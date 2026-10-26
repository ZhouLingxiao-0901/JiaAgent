"""二维码登录流程。

**只在用户显式运行 CLI 时调用**；服务启动不会自动登录。二维码内容、
验证码、bot token 都不写日志，也不落普通文件。

流程依据公开协议和参考客户端（Tencent/openclaw-weixin 的 login-qr.ts）：

    1. POST 固定地址 https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3
    2. 用户扫码；GET get_qrcode_status 轮询
    3. wait / scaned 继续；expired 刷新（最多 3 次）
       need_verifycode 让用户输入验证码；verify_code_blocked 停止
       scaned_but_redirect 换到 redirect_host 继续
       binded_redirect 表示已绑定到别处，本工具不接管
    4. confirmed 必须拿到 bot_token 和 ilink_bot_id 才保存

**兼容性限制**：这些字段来自公开协议和参考实现，不等于服务端完整契约。
真实账号尚未验证；任何缺失或类型不符都失败关闭。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from app.wechat import protocol
from app.wechat.credentials import AccountCredentials, CredentialStore
from app.wechat.errors import CredentialError, ProtocolError, WeChatError
from app.wechat.redact import safe_exception

logger = logging.getLogger(__name__)

QR_TTL_SECONDS = 300.0
MAX_REFRESH = 3
POLL_INTERVAL_SECONDS = 2.0
POLL_TIMEOUT_SECONDS = 35.0

Emit = Callable[[str], None]
AskCode = Callable[[], str | Awaitable[str]]


async def _ask(ask_code: AskCode) -> str:
    value = ask_code()
    if inspect.isawaitable(value):
        value = await value
    return str(value or "").strip()


def _safe_qr_text(qr: Any) -> str:
    """输出前再次校验，避免调用方绕过 parse_qr_response。"""
    try:
        return protocol.safe_qr_display_text(getattr(qr, "qrcode_url", ""))
    except ProtocolError:
        # 不把原始内容或解析细节带进异常/日志。
        raise CredentialError("服务端返回的二维码内容格式不安全，登录停止") from None


async def login_with_qr(
    *,
    api: Any,
    credential_store: CredentialStore,
    emit: Emit = print,
    ask_code: AskCode = input,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.time,
    max_refresh: int = MAX_REFRESH,
    qr_ttl_seconds: float = QR_TTL_SECONDS,
    poll_timeout_seconds: float = POLL_TIMEOUT_SECONDS,
    local_token_list: list[str] | None = None,
) -> AccountCredentials:
    """显示二维码并等待登录完成。成功后凭证已加密保存。"""
    qr = await api.fetch_qr_code(local_token_list=list(local_token_list or [])[:10])
    emit(
        "请用要接入 Jia 的微信扫描下面的二维码：\n"
        f"{_safe_qr_text(qr)}\n"
        "二维码只用于这次登录，请不要转发。"
    )

    current_base = protocol.FIXED_LOGIN_BASE_URL
    verify_code = ""
    refreshes = 0
    deadline = clock() + float(qr_ttl_seconds)
    last_status = ""
    verify_attempts = 0
    tokens = list(local_token_list or [])[:10]

    async def refresh_qr() -> None:
        nonlocal qr, refreshes, deadline, verify_code, last_status
        if refreshes >= max_refresh:
            raise CredentialError("二维码多次过期，登录没有完成")
        refreshes += 1
        verify_code = ""
        qr = await api.fetch_qr_code(local_token_list=tokens)
        emit(f"二维码已刷新（{refreshes}/{max_refresh}）：\n{_safe_qr_text(qr)}")
        deadline = clock() + float(qr_ttl_seconds)
        last_status = ""

    while True:
        if clock() > deadline:
            await refresh_qr()
            continue

        try:
            status = await api.poll_qr_status(
                qrcode=qr.qrcode,
                verify_code=verify_code,
                base_url=current_base,
                timeout=poll_timeout_seconds,
            )
        except WeChatError as exc:
            if isinstance(exc, CredentialError):
                raise
            # 网络抖动/暂时性错误按“继续等”处理；不打印任何 URL 或二维码。
            logger.info("二维码状态轮询暂时失败：%s", safe_exception(exc))
            await sleep(POLL_INTERVAL_SECONDS)
            continue

        if status.status != last_status:
            last_status = status.status
            if status.status == "scaned":
                emit("已扫码，正在等待手机端确认…")
            elif status.status == "wait":
                emit("等待扫码…")

        if status.status in ("wait", "scaned"):
            await sleep(POLL_INTERVAL_SECONDS)
            continue

        if status.status == "expired":
            await refresh_qr()
            continue

        if status.status == "scaned_but_redirect":
            if not status.redirect_host:
                raise CredentialError("服务端要求跳转，但没有给出 redirect_host")
            current_base = protocol.redirect_base_url(status.redirect_host)
            emit("登录区域已切换，继续等待…")
            await sleep(POLL_INTERVAL_SECONDS)
            continue

        if status.status == "need_verifycode":
            verify_attempts += 1
            if verify_attempts > 3:
                raise CredentialError("验证码尝试次数过多，登录停止")
            emit("这个账号需要输入验证码。验证码不会写入日志。")
            verify_code = await _ask(ask_code)
            if not verify_code:
                raise CredentialError("没有输入验证码，登录停止")
            continue

        if status.status == "verify_code_blocked":
            raise CredentialError("验证码错误次数过多，请稍后重新登录")

        if status.status == "binded_redirect":
            raise CredentialError(
                "这个微信账号已经绑定到别的实例。本工具不会接管已有绑定。"
            )

        if status.status == "confirmed":
            return _save_confirmed(status, credential_store, emit)

        raise CredentialError("未知的二维码状态，登录停止")


def _save_confirmed(
    status: protocol.QrStatus, store: CredentialStore, emit: Emit
) -> AccountCredentials:
    token = str(status.bot_token or "").strip()
    account_id = str(status.bot_id or "").strip()
    if not token:
        raise CredentialError("登录已确认，但服务端没有返回 bot_token，拒绝保存")
    if not account_id:
        raise CredentialError("登录已确认，但服务端没有返回 ilink_bot_id，拒绝保存")

    base_url = status.base_url or protocol.FIXED_LOGIN_BASE_URL
    if not protocol.is_allowed_base_url(base_url):
        raise CredentialError("服务端返回的 baseurl 不在允许的微信域名内，拒绝保存")

    credentials = AccountCredentials(
        account_id=account_id,
        token=token,
        base_url=protocol.normalize_base_url(base_url),
        user_id=str(status.user_id or "").strip(),
        saved_at=datetime.now().isoformat(timespec="seconds"),
    )
    store.save(credentials)
    emit("登录成功。凭证已用 Windows DPAPI 加密保存到 data/wechat/。")
    emit("注意：登录成功不等于授权。还需要在本机用 CLI 把这个微信用户加入允许名单。")
    if credentials.user_id:
        emit(f"扫码用户的 ilink_user_id 是：{credentials.user_id}")
    else:
        emit("服务端没有返回扫码用户 ID；请在电脑端用配对的用户 ID 加入允许名单。")
    logger.info("微信账号 %s 登录成功，凭证已加密保存", account_id)
    return credentials
