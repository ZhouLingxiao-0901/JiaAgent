"""微信 iLink 协议的最小、严格解析层。

依据（2026-10-24 阅读的公开资料）：

  * Tencent/openclaw-weixin `docs/protocol.md` / `docs/protocol_zh_CN.md`
  * 同一仓库的 `src/api/api.ts`、`src/api/types.ts`
  * `src/auth/login-qr.ts`、`src/messaging/inbound.ts`、`src/messaging/send.ts`

公开文档明确说它**不是完整的服务端契约**。所以这里只实现已核实、且
第一版真正需要的最小字段；未知字段不解释、不转发、不当作授权依据。
任何缺失、类型不对、超限或无法确定的输入都失败关闭。
"""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.wechat.errors import ProtocolError

# 单条入站消息的硬上限。微信文本消息实际远小于这些值；超限一律丢弃，
# 避免把异常大的内容送进模型或数据库。
MAX_MESSAGE_RAW_BYTES = 256 * 1024
MAX_UPDATES_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 16
MAX_TEXT_CHARS = 8_000
MAX_ID_CHARS = 256
MAX_CONTEXT_TOKEN_CHARS = 4_096
MAX_CURSOR_CHARS = 64 * 1024
MAX_QR_CONTENT_CHARS = 4_096
MAX_MESSAGES_PER_POLL = 100

ALLOWED_BASE_HOST_SUFFIXES = (".weixin.qq.com",)
FIXED_LOGIN_BASE_URL = "https://ilinkai.weixin.qq.com"

_MESSAGE_TYPE_USER = 1
_MESSAGE_ITEM_TEXT = 1

# C0 控制字符（允许换行和制表符）以及 DEL。微信正常文本不会带这些。
_FORBIDDEN_CONTROL = frozenset(
    chr(code) for code in range(0x00, 0x20) if code not in (0x09, 0x0A, 0x0D)
) | {"\x7f"}


@dataclass(frozen=True)
class InboundMessage:
    """经过解析、可以进入鉴权流程的一条微信消息。

    `text` 只来自单个 text_item；第一版遇到图片/语音/文件/视频直接拒绝，
    不会把它们转成字符串再交给模型。
    """

    message_key: str
    message_id: str
    from_user_id: str
    to_user_id: str
    text: str
    context_token: str
    create_time_ms: int
    seq: int | None

    @property
    def key_source(self) -> str:
        if self.message_id:
            return "message_id"
        if self.seq is not None:
            return "seq"
        return "content_hash"


@dataclass(frozen=True)
class UpdatesPage:
    messages: list[dict[str, Any]]
    cursor: str
    ret: int
    errcode: int | None
    errmsg: str
    longpolling_timeout_ms: int | None


@dataclass(frozen=True)
class QrCode:
    qrcode: str
    qrcode_url: str


@dataclass(frozen=True)
class QrStatus:
    status: str
    bot_token: str = ""
    bot_id: str = ""
    base_url: str = ""
    user_id: str = ""
    redirect_host: str = ""


def _as_text(value: Any, field: str, *, max_chars: int, allow_empty: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ProtocolError(f"{field} 不是字符串")
    if len(value) > max_chars:
        raise ProtocolError(f"{field} 太长")
    if not allow_empty and not value.strip():
        raise ProtocolError(f"{field} 为空")
    return value


def _as_optional_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{field} 不是整数")
    return int(value)


def _as_id(value: Any, field: str, *, required: bool) -> str:
    """ID 在 JSON 里可能是数字或字符串；统一转成十进制字符串。"""
    if value is None:
        if required:
            raise ProtocolError(f"缺少 {field}")
        return ""
    if isinstance(value, bool):
        raise ProtocolError(f"{field} 类型不对")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise ProtocolError(f"{field} 类型不对")
    if len(text) > MAX_ID_CHARS:
        raise ProtocolError(f"{field} 太长")
    if not text.strip():
        if required:
            raise ProtocolError(f"{field} 为空")
        return ""
    return text


def _check_text_safe(text: str) -> str:
    clean = text.strip()
    if not clean:
        raise ProtocolError("消息正文为空")
    if len(clean) > MAX_TEXT_CHARS:
        raise ProtocolError("消息正文超过上限")
    if any(ch in _FORBIDDEN_CONTROL for ch in clean):
        raise ProtocolError("消息正文包含控制字符")
    # 孤立代理项无法安全地写入日志/数据库。
    try:
        clean.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProtocolError("消息正文编码无效") from exc
    return clean


def _content_hash(from_user_id: str, create_time_ms: int, text: str) -> str:
    digest = hashlib.sha256(
        f"{from_user_id}\0{create_time_ms}\0{text}".encode("utf-8")
    ).hexdigest()
    return "content:" + digest


def parse_inbound_message(raw: Any) -> InboundMessage:
    """把 `getUpdates` 里的一个 msgs 元素转成 InboundMessage。

    只接受：
      * 一对一消息（`group_id` 为空）；
      * `message_type` 是用户（1）或未提供；
      * `message_state` 是 new（0）/ finished（2）或未提供；
      * 恰好一个 text item。
    """
    if not isinstance(raw, dict):
        raise ProtocolError("消息不是对象")

    group_id = _as_id(raw.get("group_id"), "group_id", required=False)
    if group_id:
        raise ProtocolError("第一版不处理群聊消息")

    message_type = _as_optional_int(raw.get("message_type"), "message_type")
    if message_type not in (None, _MESSAGE_TYPE_USER):
        # 包括 message_type=2（机器人自己），一律不进入后续流程。
        raise ProtocolError("不是用户消息")

    message_state = _as_optional_int(raw.get("message_state"), "message_state")
    if message_state not in (None, 0, 2):
        # 1 = generating，表示这条消息还没生成完，不能提前执行。
        raise ProtocolError("消息还没有完成")

    from_user_id = _as_id(raw.get("from_user_id"), "from_user_id", required=True)
    to_user_id = _as_id(raw.get("to_user_id"), "to_user_id", required=False)
    message_id = _as_id(raw.get("message_id"), "message_id", required=False)
    seq = _as_optional_int(raw.get("seq"), "seq")
    create_time_ms = _as_optional_int(raw.get("create_time_ms"), "create_time_ms") or 0
    if create_time_ms < 0:
        raise ProtocolError("create_time_ms 无效")

    raw_context = raw.get("context_token")
    context_token = _as_text(
        raw_context,
        "context_token",
        max_chars=MAX_CONTEXT_TOKEN_CHARS,
    ).strip()

    items = raw.get("item_list")
    if not isinstance(items, list) or not items:
        raise ProtocolError("消息没有 item_list")
    if len(items) != 1:
        raise ProtocolError("第一版只接受单条文字消息")
    item = items[0]
    if not isinstance(item, dict):
        raise ProtocolError("消息条目不是对象")
    if item.get("type") != _MESSAGE_ITEM_TEXT:
        raise ProtocolError("第一版只接受文字消息")
    text_item = item.get("text_item")
    if not isinstance(text_item, dict):
        raise ProtocolError("文字条目格式不对")
    text = _check_text_safe(_as_text(text_item.get("text"), "text", max_chars=MAX_TEXT_CHARS))

    if message_id:
        key = "mid:" + message_id
    elif seq is not None and seq >= 0:
        key = f"seq:{from_user_id}:{seq}"
    else:
        # 没有稳定 ID 时用内容指纹。它只用于去重，不作为“已读”证明。
        key = _content_hash(from_user_id, create_time_ms, text)

    return InboundMessage(
        message_key=key,
        message_id=message_id,
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        text=text,
        context_token=context_token,
        create_time_ms=create_time_ms,
        seq=seq,
    )


def parse_updates_response(raw: Any) -> UpdatesPage:
    if not isinstance(raw, dict):
        raise ProtocolError("getUpdates 响应不是对象")
    ret = _as_optional_int(raw.get("ret"), "ret") or 0
    errcode = _as_optional_int(raw.get("errcode"), "errcode")
    errmsg = _as_text(raw.get("errmsg"), "errmsg", max_chars=512)
    messages = raw.get("msgs")
    if messages is None:
        messages = []
    if not isinstance(messages, list):
        raise ProtocolError("msgs 不是数组")
    if len(messages) > MAX_MESSAGES_PER_POLL:
        raise ProtocolError("单次返回的消息过多")
    cursor = _as_text(
        raw.get("get_updates_buf"),
        "get_updates_buf",
        max_chars=MAX_CURSOR_CHARS,
    )
    timeout_ms = _as_optional_int(raw.get("longpolling_timeout_ms"), "longpolling_timeout_ms")
    if timeout_ms is not None and not (1_000 <= timeout_ms <= 120_000):
        timeout_ms = None
    return UpdatesPage(
        messages=messages,
        cursor=cursor,
        ret=ret,
        errcode=errcode,
        errmsg=errmsg,
        longpolling_timeout_ms=timeout_ms,
    )


def _validate_qr_display_text(value: Any) -> str:
    """二维码内容会直接打印到终端，必须只包含可打印 ASCII 的 HTTPS URL。

    这不是“美化”校验：服务端返回的字符串若含 ESC/换行/制表符，可能操纵
    终端显示。这里不返回、也不记录原始内容，避免异常信息再次泄露它。
    """
    if not isinstance(value, str):
        raise ProtocolError("二维码内容不是字符串")
    text = value
    if not text:
        raise ProtocolError("二维码内容为空")
    if len(text) > MAX_QR_CONTENT_CHARS:
        raise ProtocolError("二维码内容太长")
    if not text.isascii():
        raise ProtocolError("二维码内容包含非 ASCII 字符")
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in text):
        raise ProtocolError("二维码内容包含控制字符或空白")
    if "\\" in text:
        raise ProtocolError("二维码内容格式不安全")
    try:
        parsed = urlsplit(text)
    except ValueError:
        raise ProtocolError("二维码内容不是合法 URL") from None
    if parsed.scheme.casefold() != "https":
        raise ProtocolError("二维码内容必须是 HTTPS URL")
    if parsed.username or parsed.password:
        raise ProtocolError("二维码 URL 不能包含用户信息")
    try:
        raw_host = parsed.hostname
    except ValueError:
        raise ProtocolError("二维码 URL 的主机名不合法") from None
    host = str(raw_host or "").strip().rstrip(".").casefold()
    if not host or len(host) > 253 or "." not in host:
        raise ProtocolError("二维码 URL 的主机名不合法")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ProtocolError("二维码 URL 不能使用 IP 地址")
    for label in host.split("."):
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(not (ch.isalnum() or ch == "-") for ch in label)
        ):
            raise ProtocolError("二维码 URL 的主机名不合法")
    try:
        port = parsed.port
    except ValueError:
        raise ProtocolError("二维码 URL 的端口不合法") from None
    if port is not None and not (1 <= port <= 65535):
        raise ProtocolError("二维码 URL 的端口不合法")
    return text


def safe_qr_display_text(value: Any) -> str:
    """供登录层在输出前再次校验，防止调用方绕过协议解析。"""
    return _validate_qr_display_text(value)


def parse_qr_response(raw: Any) -> QrCode:
    if not isinstance(raw, dict):
        raise ProtocolError("二维码响应不是对象")
    qrcode = _as_text(raw.get("qrcode"), "qrcode", max_chars=MAX_CONTEXT_TOKEN_CHARS, allow_empty=False)
    url = _validate_qr_display_text(raw.get("qrcode_img_content"))
    return QrCode(qrcode=qrcode, qrcode_url=url)


def parse_qr_status(raw: Any) -> QrStatus:
    if not isinstance(raw, dict):
        raise ProtocolError("二维码状态响应不是对象")
    status = _as_text(raw.get("status"), "status", max_chars=32, allow_empty=False)
    allowed = {
        "wait", "scaned", "confirmed", "expired", "scaned_but_redirect",
        "need_verifycode", "verify_code_blocked", "binded_redirect",
    }
    if status not in allowed:
        raise ProtocolError("未知的二维码状态")

    base_url = _as_text(raw.get("baseurl"), "baseurl", max_chars=2_048).strip()
    if base_url and not is_allowed_base_url(base_url):
        raise ProtocolError("服务端返回的 baseurl 不在允许的微信域名内")
    redirect_host = _as_text(raw.get("redirect_host"), "redirect_host", max_chars=256).strip()
    if redirect_host and not _is_allowed_host(redirect_host):
        raise ProtocolError("redirect_host 不在允许的微信域名内")

    return QrStatus(
        status=status,
        bot_token=_as_text(raw.get("bot_token"), "bot_token", max_chars=8_192).strip(),
        bot_id=_as_id(raw.get("ilink_bot_id"), "ilink_bot_id", required=False),
        base_url=base_url,
        user_id=_as_id(raw.get("ilink_user_id"), "ilink_user_id", required=False),
        redirect_host=redirect_host,
    )


def parse_send_response(raw: Any) -> str:
    if not isinstance(raw, dict):
        raise ProtocolError("sendMessage 响应不是对象")
    ret = _as_optional_int(raw.get("ret"), "ret") or 0
    if ret != 0:
        from app.wechat.errors import ApiBusinessError

        raise ApiBusinessError("sendMessage", ret)
    return _as_id(raw.get("message_id"), "message_id", required=False)


def parse_get_config_response(raw: Any) -> str:
    if not isinstance(raw, dict):
        raise ProtocolError("getConfig 响应不是对象")
    ret = _as_optional_int(raw.get("ret"), "ret") or 0
    if ret != 0:
        return ""
    return _as_text(raw.get("typing_ticket"), "typing_ticket", max_chars=8_192).strip()


def _is_allowed_host(host: str) -> bool:
    name = str(host or "").strip().strip("[]").rstrip(".").casefold()
    if not name:
        return False
    # 只允许域名，不允许 IP 字面量，避免把 Bearer token 发到任意地址。
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return False
    return any(name == suffix.lstrip(".") or name.endswith(suffix) for suffix in ALLOWED_BASE_HOST_SUFFIXES)


def is_allowed_base_url(value: str) -> bool:
    """校验服务端给的 API 地址：必须 https，且是微信域名。"""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    if parsed.scheme.casefold() != "https":
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.port not in (None, 443):
        return False
    if parsed.path not in ("", "/"):
        return False
    if parsed.query or parsed.fragment:
        return False
    return _is_allowed_host(parsed.hostname or "")


def normalize_base_url(value: str) -> str:
    if not is_allowed_base_url(value):
        raise ProtocolError("baseurl 不在允许的微信域名内")
    parsed = urlsplit(value.strip())
    return f"https://{parsed.hostname}"


def redirect_base_url(host: str) -> str:
    name = str(host or "").strip().strip("[]").rstrip(".")
    if not _is_allowed_host(name):
        raise ProtocolError("redirect_host 不在允许的微信域名内")
    return f"https://{name}"


def short_task_id(task_id: str) -> str:
    text = str(task_id or "").strip()
    return text[:8] if text else ""
