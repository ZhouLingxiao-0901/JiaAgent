"""本地 HTTP 服务的访问控制（只服务于本机网页）。

# 威胁模型（先写在最前面，避免高估这些措施）

能防住的：

  - 别的网站通过浏览器向本机服务发起的跨站请求（CSRF / DNS rebinding）；
  - 从局域网或公网直接访问服务（应用层再拦一次，即使意外绑定了 0.0.0.0）；
  - 过期、伪造、重放的恢复急停请求。

**防不住的：** 同一用户权限下的恶意本地进程。它可以自己请求一个会话、
读取界面拿到的令牌，甚至直接改配置或调用系统 API。普通 HTTP 令牌和
来源校验不是身份认证，不能把本机恶意进程挡在外面。

# 三层检查

  1. Host 必须是本机回环名（localhost / 127.0.0.0/8 / ::1）；
  2. 连接来源 IP 必须是回环地址；
  3. 跨站请求（Origin / Referer / Sec-Fetch-Site）一律拒绝。

修改输入状态的接口还要带 `X-Jia-Session`：这个令牌只在同源页面初始化时
签发，跨站页面拿不到（自定义请求头会触发预检，而服务没有开放 CORS）。
它挡的是"别的网页"，不是"本机别的程序"。

# 恢复急停：一次性挑战

`confirmed: true` 只是"用户想恢复"的意图，不是授权。恢复必须：

  1. 持有有效的本地会话令牌；
  2. 请求来自同源页面；
  3. 先换取一个一次性挑战（`/input/emergency-reset/prepare`）；
  4. 在很短时间内用这个挑战完成恢复，且急停状态没有在此期间变化。

挑战一次性使用、绑定会话和急停代次。急停再次触发后，旧挑战立即失效。
恢复本身不会启用真实鼠标输入，双重启用条件仍然必须满足。
"""

from __future__ import annotations

import ipaddress
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from app.errors import JiaAgentError

SESSION_HEADER = "X-Jia-Session"
SESSION_TTL_SECONDS = 30 * 60
RESET_CHALLENGE_TTL_SECONDS = 60

_MAX_SESSIONS = 64
_MAX_CHALLENGES = 64

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


# ----------------------------------------------------------------------
# 错误
# ----------------------------------------------------------------------
class InputAccessError(JiaAgentError):
    """本地访问控制拒绝。"""

    code = "input_access_denied"
    http_status = 403


class UntrustedRequest(InputAccessError):
    code = "untrusted_request"
    http_status = 403


class UntrustedOrigin(InputAccessError):
    code = "untrusted_origin"
    http_status = 403


class InputSessionRequired(InputAccessError):
    code = "input_session_required"
    http_status = 401


class InputSessionInvalid(InputAccessError):
    code = "input_session_invalid"
    http_status = 401


class ResetChallengeRequired(JiaAgentError):
    code = "reset_challenge_required"
    http_status = 400


class ResetChallengeInvalid(JiaAgentError):
    code = "reset_challenge_invalid"
    http_status = 409


class EmergencyNotActive(JiaAgentError):
    code = "emergency_not_active"
    http_status = 409


# ----------------------------------------------------------------------
# 地址判断
# ----------------------------------------------------------------------
def is_loopback_host(host: str) -> bool:
    """主机名是不是本机回环。`localhost` 和 127.0.0.0/8、::1 都算。"""
    name = str(host or "").strip().strip("[]").rstrip(".").casefold()
    if not name:
        return False
    if name == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback


def is_loopback_address(host: str) -> bool:
    """连接来源 IP 是不是本机回环（兼容 IPv4-mapped IPv6）。"""
    try:
        address = ipaddress.ip_address(str(host or "").strip())
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _hostname_from_header(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return ""
        rest = text[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return ""
        return text[1:end]
    if text.count(":") == 1:
        name, port = text.rsplit(":", 1)
        return name if port.isdigit() else ""
    return text


def host_header_allowed(value: str) -> bool:
    """Host 头必须是本机回环名。缺失、域名、局域网 IP 一律拒绝。"""
    return is_loopback_host(_hostname_from_header(value))


def _port_from_host_header(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return None
        rest = text[end + 1 :]
        if not rest:
            return 80
        if rest.startswith(":") and rest[1:].isdigit():
            return int(rest[1:])
        return None
    if text.count(":") == 1:
        _, port = text.rsplit(":", 1)
        return int(port) if port.isdigit() else None
    return 80


def _request_port(request: Request) -> int | None:
    port = _port_from_host_header(request.headers.get("host", ""))
    if port is not None:
        return port
    try:
        return request.url.port
    except Exception:  # noqa: BLE001
        return None


def origin_allowed(origin: str, request: Request) -> bool:
    """Origin/Referer 必须与本机页面的协议、主机、端口一致。"""
    text = str(origin or "").strip()
    if not text or text.casefold() == "null":
        return False
    try:
        parsed = urlsplit(text)
        host = parsed.hostname or ""
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme != request.url.scheme:
        return False
    if not is_loopback_host(host):
        return False
    port = _request_port(request)
    return port is not None and origin_port == port


def request_client_is_loopback(request: Request) -> bool:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    if host is None and isinstance(client, (tuple, list)) and client:
        host = client[0]
    return is_loopback_address(str(host or ""))


# ----------------------------------------------------------------------
# 会话与一次性恢复挑战
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class InputSession:
    token: str
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class _ResetChallenge:
    token: str
    generation: int
    expires_at: float


class InputAccessControl:
    """内存里的本地会话和一次性恢复挑战。

    重启服务会清空所有会话和挑战；已经生效的急停状态仍然从磁盘读回。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        session_ttl: float = SESSION_TTL_SECONDS,
        challenge_ttl: float = RESET_CHALLENGE_TTL_SECONDS,
    ) -> None:
        self._clock = clock or time.monotonic
        self.session_ttl = float(session_ttl)
        self.challenge_ttl = float(challenge_ttl)
        self._lock = threading.RLock()
        self._sessions: dict[str, InputSession] = {}
        self._challenges: dict[str, _ResetChallenge] = {}

    # ---- 内部 ----
    def _prune_locked(self, now: float) -> None:
        expired = [
            token for token, item in self._sessions.items()
            if now >= item.expires_at
        ]
        for token in expired:
            self._sessions.pop(token, None)
        expired = [
            nonce for nonce, item in self._challenges.items()
            if now >= item.expires_at
        ]
        for nonce in expired:
            self._challenges.pop(nonce, None)

    # ---- 会话 ----
    def new_session(self) -> InputSession:
        now = float(self._clock())
        with self._lock:
            self._prune_locked(now)
            if len(self._sessions) >= _MAX_SESSIONS:
                oldest = min(
                    self._sessions,
                    key=lambda token: self._sessions[token].created_at,
                )
                self._sessions.pop(oldest, None)
            session = InputSession(
                token=secrets.token_urlsafe(32),
                created_at=now,
                expires_at=now + self.session_ttl,
            )
            self._sessions[session.token] = session
            return session

    def validate_session(self, token: str) -> InputSession | None:
        text = str(token or "").strip()
        if not text:
            return None
        now = float(self._clock())
        with self._lock:
            self._prune_locked(now)
            session = self._sessions.get(text)
            if session is None or now >= session.expires_at:
                return None
            return session

    def session_remaining(self, session: InputSession) -> int:
        return max(0, int(round(session.expires_at - float(self._clock()))))

    # ---- 恢复挑战 ----
    def new_reset_challenge(
        self, session_token: str, generation: int
    ) -> tuple[str, int]:
        now = float(self._clock())
        with self._lock:
            self._prune_locked(now)
            if len(self._challenges) >= _MAX_CHALLENGES:
                oldest = min(
                    self._challenges,
                    key=lambda nonce: self._challenges[nonce].expires_at,
                )
                self._challenges.pop(oldest, None)
            nonce = secrets.token_urlsafe(32)
            self._challenges[nonce] = _ResetChallenge(
                token=str(session_token),
                generation=int(generation),
                expires_at=now + self.challenge_ttl,
            )
            return nonce, self.challenge_ttl_seconds

    @property
    def challenge_ttl_seconds(self) -> int:
        return max(0, int(round(self.challenge_ttl)))

    def consume_reset_challenge(
        self, nonce: str, session_token: str, generation: int
    ) -> tuple[bool, str]:
        """验证并消耗一次性挑战。失败时不改变急停状态。"""
        text = str(nonce or "").strip()
        now = float(self._clock())
        with self._lock:
            self._prune_locked(now)
            challenge = self._challenges.get(text)
            if challenge is None:
                return False, "恢复凭证不存在、已经使用或已经过期。请重新确认。"
            if challenge.token != str(session_token):
                # 不删除：避免别人拿一个偷来的 nonce 让合法用户的挑战失效。
                return False, "恢复凭证不属于当前会话。"
            if now >= challenge.expires_at:
                self._challenges.pop(text, None)
                return False, "恢复凭证已经过期，请重新确认。"
            if int(generation) != challenge.generation:
                self._challenges.pop(text, None)
                return False, "急停状态在确认后发生了变化，本次恢复请求已失效。"
            self._challenges.pop(text, None)
            return True, ""


# 进程级单例。测试可以替换成隔离的实例。
input_access = InputAccessControl()


# ----------------------------------------------------------------------
# 请求校验
# ----------------------------------------------------------------------
def require_trusted_request(request: Request) -> None:
    """Host、来源 IP、Origin/Referer、Sec-Fetch-Site 的联合校验。"""
    if not host_header_allowed(request.headers.get("host", "")):
        raise UntrustedRequest(
            "请求的 Host 不是本机地址，拒绝访问。",
            detail="只允许 localhost、127.0.0.0/8 或 ::1。",
        )
    if not request_client_is_loopback(request):
        raise UntrustedRequest(
            "请求不是来自本机回环地址，拒绝访问。",
            detail="JiaAgent 只服务本机浏览器。",
        )

    site = str(request.headers.get("sec-fetch-site", "")).strip().casefold()
    if site and site not in {"same-origin", "none"}:
        raise UntrustedOrigin(
            "跨站请求被拒绝。",
            detail=f"Sec-Fetch-Site={site!r}，只允许同源请求。",
        )

    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    check_value = origin or referer
    if check_value and not origin_allowed(check_value, request):
        raise UntrustedOrigin(
            "请求来源不是本机页面，拒绝访问。",
            detail="Origin/Referer 与当前服务地址不一致。",
        )


async def require_input_session(request: Request) -> InputSession:
    """输入控制接口的依赖：可信来源 + 有效本地会话。"""
    require_trusted_request(request)
    token = request.headers.get(SESSION_HEADER, "")
    if not str(token or "").strip():
        raise InputSessionRequired(
            "缺少本地会话令牌，拒绝访问。",
            detail="请从本机网页重新初始化会话。",
        )
    session = input_access.validate_session(token)
    if session is None:
        raise InputSessionInvalid(
            "本地会话无效或已经过期，拒绝访问。",
            detail="请刷新页面重新初始化会话。",
        )
    return session


# ----------------------------------------------------------------------
# 全局中间件
# ----------------------------------------------------------------------
def _error_response(exc: JiaAgentError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status, content={"error": exc.to_dict()}
    )


async def local_only_middleware(request: Request, call_next):
    """所有请求先过本机边界；/api 的写操作再过来源校验。"""
    try:
        if not host_header_allowed(request.headers.get("host", "")):
            return _error_response(UntrustedRequest(
                "请求的 Host 不是本机地址，拒绝访问。",
                detail="只允许 localhost、127.0.0.0/8 或 ::1。",
            ))
        if not request_client_is_loopback(request):
            return _error_response(UntrustedRequest(
                "请求不是来自本机回环地址，拒绝访问。",
                detail="JiaAgent 只服务本机浏览器。",
            ))
        if (
            request.method.upper() not in _SAFE_METHODS
            and request.url.path.startswith("/api/")
        ):
            require_trusted_request(request)
    except JiaAgentError as exc:
        return _error_response(exc)
    return await call_next(request)
