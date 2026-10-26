"""真实输入的控制接口：查看状态、急停、恢复。

安全边界：
  - 所有接口只接受本机回环请求，并校验 Host 与来源（见 `app.api.security`）；
  - `emergency-stop` 是安全动作，只需要可信来源，不要求会话令牌：即使
    会话过期，用户也必须能立刻停下真实输入；
  - 查看状态、恢复急停需要本地会话令牌；
  - 恢复还需要一次性挑战。`confirmed: true` 只表达意图，不能单独授权；
  - 恢复急停不等于启用真实输入——双重启用条件仍然要满足，后端也只在
    启动时按条件构造；
  - 这些接口都不能修改配置或环境变量，模型和前端也无法借此绕开授权。

这里的任何接口都不会执行点击。

威胁模型：这些措施挡的是**别的网站**（CSRF、DNS rebinding）和局域网访问。
同一用户权限下的恶意本地进程可以自己申请会话令牌，普通 HTTP 令牌和
来源校验挡不住它；不要把这套机制当成操作系统的身份认证。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.agent import pending
from app.api import security
from app.errors import JiaAgentError
from app.input import control
from app.input.windows_backend import real_mouse_enablement

logger = logging.getLogger(__name__)
router = APIRouter()


class InputControlError(JiaAgentError):
    code = "input_control_rejected"
    http_status = 400


class ResetRequest(BaseModel):
    # confirmed 只表达"用户想恢复"的意图，不是充分授权。还必须提供
    # 由 /prepare 换取、短时有效、一次性的 nonce。
    confirmed: bool = Field(
        default=False, description="必须显式传 true 才能恢复真实输入"
    )
    nonce: str = Field(
        default="", description="恢复前换取的一次性挑战，不能重复使用"
    )


def _backend_state() -> dict[str, Any]:
    """当前登记处里的点击工具用的是哪种后端。"""
    from app.tools.registry import registry

    tool = registry.get("click_element")
    backend = getattr(tool, "_backend", None) if tool is not None else None
    simulated = (
        bool(getattr(backend, "simulated", False)) if backend is not None else None
    )
    return {
        "registered": tool is not None,
        "backend": getattr(backend, "name", None) if backend is not None else None,
        "simulated": simulated,
        "real_input_active": backend is not None and simulated is False,
    }


def _status_dict() -> dict[str, Any]:
    decision = real_mouse_enablement()
    return {
        "real_mouse": {
            **decision.to_dict(),
            **_backend_state(),
        },
        "emergency": control.emergency.snapshot().to_dict(),
        "rate_limit": control.limiter.snapshot(),
        "hint": (
            "急停只影响真实输入；模拟后端和只读工具不受影响。"
            "恢复急停不会自动启用真实输入。"
        ),
    }


@router.post("/input/session")
async def open_input_session(request: Request) -> dict:
    """给同源网页签发一个短期本地会话令牌。

    这不是用户身份认证：任何能访问本机服务、并绕过浏览器来源校验的
    本地进程都可以申请。它只用于把"网页发起的输入控制请求"和"别的
    网站伪造的跨站请求"区分开。
    """
    security.require_trusted_request(request)
    session = security.input_access.new_session()
    return {
        "token": session.token,
        "expires_in_seconds": security.input_access.session_remaining(session),
        "header": security.SESSION_HEADER,
    }


@router.get("/input/status")
async def input_status(
    session: security.InputSession = Depends(security.require_input_session),
) -> dict:
    """真实输入是否启用、急停状态、速率限制额度。

    只返回状态，不包含坐标、密钥或任何可执行能力。仍然要求本地会话，
    避免被跨站页面或 DNS rebinding 读取。
    """
    return _status_dict()


@router.post("/input/emergency-stop")
async def emergency_stop(request: Request) -> dict:
    """立即停掉真实输入。

    急停是"减少能力"的动作，**故意不要求会话令牌**：令牌过期、页面
    还没初始化完，或者用户直接调用本机接口时，都应该还能立刻停下。
    来源仍必须是同源页面或非浏览器本机请求；别的网站发来的跨站请求
    会被 Origin / Sec-Fetch-Site 拒绝。

    同时拒绝所有等待中的确认，避免用户在急停后继续批准某个点击。
    """
    security.require_trusted_request(request)
    state = control.emergency.trigger(
        "用户从网页按下了急停", source="web-ui"
    )
    cancelled = pending.store.cancel_all()
    logger.warning(
        "用户触发急停；已取消 %d 个等待中的确认", cancelled
    )
    return {
        "ok": True,
        "emergency": state.to_dict(),
        "cancelled_pending": cancelled,
        "message": "急停已生效。所有尚未执行的真实输入都会被拒绝。",
    }


@router.post("/input/emergency-reset/prepare")
async def emergency_reset_prepare(
    session: security.InputSession = Depends(security.require_input_session),
) -> dict:
    """为一次恢复操作换取短时、一次性的挑战。

    只有在急停已经生效时才签发。挑战绑定当前会话和急停代次；如果
    期间又发生了一次急停，或者在别处恢复过，旧挑战会失效。
    """
    emergency = control.emergency
    if not emergency.stopped:
        raise security.EmergencyNotActive(
            "当前没有处于急停状态，不需要恢复。"
        )
    nonce, ttl = security.input_access.new_reset_challenge(
        session.token, emergency.generation
    )
    logger.info("已签发一次恢复急停挑战（会话 %s…）", session.token[:8])
    return {
        "nonce": nonce,
        "expires_in_seconds": ttl,
        "message": "挑战仅可使用一次；恢复不会启用真实输入。",
    }


@router.post("/input/emergency-reset")
async def emergency_reset(
    request: Request,
    body: ResetRequest,
    session: security.InputSession = Depends(security.require_input_session),
) -> dict:
    """恢复急停。

    仅当以下条件同时成立时才恢复：

      - 请求来自本机同源页面，且带有有效本地会话；
      - `confirmed` 为 true（用户意图）；
      - 提供了未过期、未使用、属于本会话的一次性挑战；
      - 急停代次与签发挑战时一致（期间没有新的急停）。

    任何一条不满足都拒绝，且不改变急停状态。恢复后真实输入仍然要满足
    配置 + 环境变量的双重启用条件，本接口不会修改任何开关。
    """
    security.require_trusted_request(request)

    if body.confirmed is not True:
        raise InputControlError(
            "恢复真实输入需要用户显式确认，confirmed 必须为 true。"
        )

    nonce = str(body.nonce or "").strip()
    if not nonce:
        raise security.ResetChallengeRequired(
            "仅凭 confirmed=true 不能恢复急停。请在本机页面上重新确认，"
            "取得一次性的恢复凭证后再试。",
            detail="先调用 POST /api/input/emergency-reset/prepare。",
        )

    emergency = control.emergency
    if not emergency.stopped:
        raise security.EmergencyNotActive(
            "当前没有处于急停状态，不需要恢复。"
        )

    generation = emergency.generation
    ok, reason = security.input_access.consume_reset_challenge(
        nonce, session.token, generation
    )
    if not ok:
        raise security.ResetChallengeInvalid(reason)

    # 校验和清除在同一把锁里完成：期间若又有急停，会因代次变化而失败。
    ok, message = emergency.reset_if_generation(
        generation, confirmed=True, source="web-ui"
    )
    if not ok:
        raise security.ResetChallengeInvalid(message)

    logger.warning("用户从网页恢复了急停")
    return {
        "ok": True,
        "message": message,
        **_status_dict(),
    }
