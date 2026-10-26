"""微信模块的日志脱敏工具。

原则：日志里不出现 Bot token、context_token、二维码值、验证码、远程授权码。
用户 ID 是 PII，只保留可关联的短哈希，不写完整值。
"""

from __future__ import annotations

import hashlib


def redact_token(value: str | None) -> str:
    if not value:
        return "(none)"
    return f"<redacted len={len(value)}>"


def redact_id(value: str | None) -> str:
    """把发送者 ID 变成不可逆的短哈希，便于排查但不直接暴露 ID。"""
    text = str(value or "")
    if not text:
        return "(none)"
    return "u-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def redact_code(value: str | None) -> str:
    if not value:
        return "(none)"
    return "<code>"


def safe_exception(exc: BaseException) -> str:
    """异常类型 + 经过筛选的消息。

    网络异常有时会把 URL（含 qrcode/verify_code）带进字符串，所以默认只
    记录类型；只有我们自己定义的、明确安全的异常才记录消息。
    """
    from app.wechat.errors import WeChatError

    if isinstance(exc, WeChatError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__
