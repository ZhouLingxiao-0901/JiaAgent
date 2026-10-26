"""把启动器捕获的 stdout/stderr 再做一遍脱敏。

应用自己的 ``logs/jiaagent.log`` 已经由 ``app.wechat.redact`` 等模块控制；
但 ``logs/autostart.log`` 捕获的是 Python/uvicorn 的原始输出，里面可能
出现第三方库异常、请求 URL 或配置错误带出的密钥。这里按“保留诊断信息、
只替换敏感值”的原则处理，不因为怕泄露就把整行丢掉。

注意：这不是密码学意义上的安全边界。同用户进程仍然能读到环境变量和
配置文件；它只保证普通启动日志不会顺手留下 token、验证码、二维码等内容。
"""

from __future__ import annotations

import re

_REDACTED = "<redacted>"

# 常见密钥字段名。refresh_token / client_secret 以及环境变量里的
# JIA_WECHAT_BOT_TOKEN、JIA_CLIENT_SECRET 这类带前缀的名字也要覆盖。
_KEY_LABEL = (
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|bot[_-]?token|"
    r"client[_-]?secret|context[_-]?token|verify[_-]?code|"
    r"authorization[_-]?code|pairing[_-]?code|auth[_-]?token|"
    r"secret|password|passwd|token)"
)
# 只接受“分段前缀”（必须带 - 或 _），避免把 ABC123SECRET 这类普通标识
# 里的 SECRET 当成字段名；同时覆盖 JIA_WECHAT_BOT_TOKEN。
_KEY_PREFIX = r"(?:[A-Za-z0-9]+[_-])+"

# 顺序有意义：先处理 Bearer / sk- 这类有固定形态的，再处理 key=value。
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        "Bearer <redacted>",
    ),
    (
        re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b"),
        "<redacted-api-key>",
    ),
    (
        # Authorization: Basic xxx（Bearer 已在上面处理）。
        # 必须整行替换：Basic 的凭据在 scheme 后面，只删 scheme 不够。
        re.compile(r"(?i)(Authorization\s*:\s*)(?!Bearer\b)[^\r\n]+"),
        r"\1<redacted>",
    ),
    (
        # 形如 JIA_DEEPSEEK_API_KEY=... / JIA_WECHAT_BOT_TOKEN=...
        # / context_token: "..." / "access_token": "..." / {"token": "..."}
        # 键名前必须是边界，避免把 ABC123SECRET 这类普通标识当字段名；
        # 键名可以带 " 引号（JSON），也可以带分段前缀（环境变量）。
        # 未加引号的值在 &、;、,、} 处停止，避免吞掉其他字段。
        re.compile(
            r"(?i)(?<![A-Za-z0-9_])"
            r"(\"?(?:" + _KEY_PREFIX + r")?" + _KEY_LABEL + r"\"?\s*[:=]\s*)"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&}]+)"
        ),
        r"\1<redacted>",
    ),
    (
        # URL 查询参数：?token=...&code=...&refresh_token=...
        re.compile(
            r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"bot[_-]?token|client[_-]?secret|context[_-]?token|"
            r"auth[_-]?token|verify[_-]?code|auth[_-]?code|"
            r"authorization[_-]?code|pairing[_-]?code|token|code|secret|password)=)"
            r"[^&\s]+"
        ),
        r"\1<redacted>",
    ),
    (
        # 中文日志里的标签
        re.compile(
            r"((?:授权码|验证码|二维码|令牌|密钥|密码|凭证)\s*[:：=]\s*)"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
        ),
        r"\1<redacted>",
    ),
    (
        # 微信登录二维码 URL（整段替换，不保留可扫描内容）
        re.compile(
            r"(?i)https?://[^\s]*(?:qrcode|qr|login|verify)[^\s]*weixin\.qq\.com[^\s]*"
        ),
        "<redacted-qr-url>",
    ),
)


def redact_log_line(line: str) -> str:
    """返回脱敏后的单行文本；普通诊断信息保持不变。"""
    text = "" if line is None else str(line)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
