"""本机管理动作：取消配对与会话退出。

**unpair**：只移除允许名单并撤销该用户尚未使用的授权码。不删除任务归属，
也不取消正在运行的任务——任务是否继续由用户通过 `/cancel` 或电脑端显式决定。

**logout**：删除本机 DPAPI 凭证并撤销该账号未使用的授权码。它**不等于**
微信服务端解绑，也不会猜测或调用未确认的服务端接口。Jia 正在运行时拒绝
执行，避免出现“凭证已删、旧监听还活着”的不一致状态。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from app.wechat.credentials import (
    AllowListStore,
    CredentialStore,
    Protector,
)
from app.wechat.errors import CredentialError
from app.wechat.runtime_state import runtime_is_active
from app.wechat.store import WeChatStore


@dataclass(frozen=True)
class UnpairResult:
    account_id: str
    user_id: str
    removed: bool
    revoked_codes: int


@dataclass(frozen=True)
class LogoutResult:
    account_id: str
    revoked_codes: int


def _state_path(state_dir: Path | str) -> Path:
    return Path(state_dir)


def _revoke_codes(state_dir: Path, account_id: str, now: float) -> int:
    db_path = _state_path(state_dir) / "state.db"
    if not db_path.exists():
        return 0
    store = WeChatStore(db_path)
    store.init()
    return store.expire_approvals_for_account(account_id, now)


def unpair_user(
    state_dir: Path | str,
    *,
    account_id: str,
    user_id: str,
    now: float | None = None,
) -> UnpairResult:
    account = str(account_id or "").strip()
    user = str(user_id or "").strip()
    if not account or not user:
        raise CredentialError("账号 ID 和用户 ID 都不能为空")
    state = _state_path(state_dir)
    removed = AllowListStore(state).remove(account_id=account, user_id=user)
    revoked = 0
    db_path = state / "state.db"
    if db_path.exists():
        store = WeChatStore(db_path)
        store.init()
        revoked = store.expire_approvals_for_user(
            account, user, time.time() if now is None else float(now)
        )
    return UnpairResult(
        account_id=account, user_id=user, removed=removed,
        revoked_codes=revoked,
    )


def logout_account(
    state_dir: Path | str,
    *,
    account_id: str,
    protector: Protector,
    now: float | None = None,
) -> LogoutResult:
    """删除本地凭证。调用方负责把“仅本地删除”的含义明确告诉用户。"""
    account = str(account_id or "").strip()
    if not account:
        raise CredentialError("必须指定要退出的账号 ID")
    state = _state_path(state_dir)
    if runtime_is_active(state):
        raise CredentialError(
            "Jia 的微信监听仍在运行或状态未知，请先停止 Jia 再退出账号"
        )

    credentials_store = CredentialStore(state, protector)
    # load 会先校验账号字符、路径中的链接，并解密验证凭证；不会删除任何东西。
    credentials = credentials_store.load(account)
    if credentials is None:
        raise CredentialError(f"账号 {account} 没有本地登录凭证")

    revoked = _revoke_codes(state, account, time.time() if now is None else float(now))
    credentials_store.clear(account)
    return LogoutResult(account_id=account, revoked_codes=revoked)
