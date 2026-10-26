"""真实 Windows 鼠标后端（只做单次左键单击）。

# 两道启用条件

真实输入默认关闭。只有下面两条**同时**满足，`build_input_backend()` 才会
构造这个后端：

  1. `config/tools.yaml` 里 `input.real_mouse_enabled: true`
  2. 环境变量 `JIA_ENABLE_REAL_MOUSE=1`（必须来自真实的进程环境，
     不是 .env 文件；服务启动后也不会被任何请求修改）

任何一条不满足，工具登记处拿到的就是 `None`，`click_element` 会在 prepare
阶段失败关闭，连确认卡片都不会弹。没有"自动退回另一种真实输入"的路径。

# 执行前的最后核对

工具层已经做过授权、观察记录校验、聚焦和前台核对。后端在真正
`SendInput` 之前还会**再查一遍**：后端可用、急停未触发、不是安全桌面、
目标不是 UAC/锁屏、窗口身份/可见性/位置没变、前台仍是它、坐标处最顶层
的窗口仍是它。随后才占用速率额度并发送。这些检查只能缩短竞态窗口，
不能证明竞态不存在；因此结果必须如实区分 `failed`（确定没执行）和
`unknown`（无法确认）。

部分事件提交（例如"抬起"没提交）时，适配层会尝试补发一次左键抬起，
避免按键卡在按下状态；这只是清理，不是重试点击。清理也失败时，结果仍是
`unknown`，并明确提示需要人工检查鼠标按键。

真实输入只支持 Windows；环境不满足、读不到必要信息、无法可靠检测安全
桌面时，一律失败关闭。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.input import control
from app.input.backend import (
    CLICK_FAILED,
    CLICK_SUCCEEDED,
    CLICK_UNKNOWN,
    CODE_BACKEND_UNAVAILABLE,
    CODE_CLICK_FAILED,
    CODE_CLICK_UNKNOWN,
    CODE_EMERGENCY_STOP,
    CODE_FOCUS_FAILED,
    CODE_FOREGROUND_MISMATCH,
    CODE_POINT_NOT_TARGET,
    CODE_RATE_LIMITED,
    CODE_SECURE_DESKTOP,
    CODE_UNSAFE_TARGET,
    CODE_WINDOW_CHANGED,
    CODE_WINDOW_GONE,
    CODE_WINDOW_HIDDEN,
    CODE_WINDOW_MINIMIZED,
    CODE_WINDOW_MOVED,
    BackendStatus,
    ClickOutcome,
    FocusResult,
    InputBackend,
    identity_problem,
    unsafe_target_reason,
)
from app.input.windows_api import Win32InputApi
from app.vision.model import ScreenMetrics, WindowInfo

logger = logging.getLogger(__name__)

ENV_REAL_MOUSE = "JIA_ENABLE_REAL_MOUSE"
_CONFIG_KEY = "input.real_mouse_enabled"


class RealInputDisabled(RuntimeError):
    """双重启用条件没有同时满足。"""


@dataclass(frozen=True)
class Enablement:
    """真实输入是否被允许构造。两个条件分别记录，方便界面解释。"""

    config_enabled: bool
    env_enabled: bool
    allowed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_key": _CONFIG_KEY,
            "config_enabled": self.config_enabled,
            "env_name": ENV_REAL_MOUSE,
            "env_enabled": self.env_enabled,
            "allowed": self.allowed,
            "reason": self.reason,
        }


def _input_config(input_config: Any = None) -> Any:
    if input_config is not None:
        return input_config
    from app.settings import get_tools_config

    return get_tools_config().input


def real_mouse_enablement(
    input_config: Any = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Enablement:
    """检查双重启用条件。纯函数，测试可以直接传配置和 env。"""
    config_enabled = bool(
        getattr(_input_config(input_config), "real_mouse_enabled", False)
    )
    source = os.environ if env is None else env
    env_enabled = str(source.get(ENV_REAL_MOUSE, "")).strip() == "1"

    missing: list[str] = []
    if not config_enabled:
        missing.append(f"配置 {_CONFIG_KEY} 不是 true")
    if not env_enabled:
        missing.append(f"环境变量 {ENV_REAL_MOUSE} 不是 1")
    allowed = not missing
    reason = (
        "双重启用条件已满足。"
        if allowed
        else "真实输入未启用：" + "；".join(missing) + "。"
    )
    return Enablement(
        config_enabled=config_enabled,
        env_enabled=env_enabled,
        allowed=allowed,
        reason=reason,
    )


class WindowsInputBackend(InputBackend):
    """用 Windows `SendInput` 执行单次左键单击。"""

    name = "windows-sendinput"
    simulated = False

    def __init__(
        self,
        *,
        api: Any = None,
        controller: control.RealInputController | None = None,
        input_config: Any = None,
        window_lookup: Callable[[int], WindowInfo | None] | None = None,
    ) -> None:
        if api is None:
            # 生产路径：构造真实适配器之前必须再验一次双重条件。
            # 注入 api 是测试用的替换点，不会走到这里。
            decision = real_mouse_enablement(input_config)
            if not decision.allowed:
                raise RealInputDisabled(decision.reason)
            api = Win32InputApi()

        self._api = api
        self._controller = controller or control.controller
        self._lookup = window_lookup or api.get_window_info
        self.input_config = _input_config(input_config)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def status(self) -> BackendStatus:
        available, reason = self._api.available()
        if not available:
            return BackendStatus(available=False, reason=reason, simulated=False)
        stop = self._controller.block_reason()
        if stop:
            return BackendStatus(available=False, reason=stop, simulated=False)
        return BackendStatus(available=True, simulated=False)

    @property
    def emergency(self) -> control.EmergencyStop:
        return self._controller.emergency

    def secure_desktop(self) -> tuple[bool, str]:
        try:
            return self._api.secure_desktop()
        except Exception as exc:  # noqa: BLE001 - 检测失败必须失败关闭
            return (
                True,
                f"安全桌面检测抛出异常：{type(exc).__name__}: {exc}。"
                "不能确认安全，拒绝执行。",
            )

    # ------------------------------------------------------------------
    # 聚焦
    # ------------------------------------------------------------------
    def _window_problem(
        self,
        expected: WindowInfo,
        actual: WindowInfo | None,
        expected_rect: tuple[int, int, int, int],
        *,
        check_rect: bool,
    ) -> tuple[str, str]:
        if actual is None:
            return CODE_WINDOW_GONE, "目标窗口已关闭或句柄失效。"
        problem = identity_problem(expected, actual)
        if problem:
            return CODE_WINDOW_CHANGED, problem
        # 防御性再查一次：即使工具层漏了，后端也不会操作 UAC/锁屏窗口。
        unsafe = unsafe_target_reason(actual)
        if unsafe:
            return CODE_UNSAFE_TARGET, unsafe
        if not actual.visible:
            return CODE_WINDOW_HIDDEN, "目标窗口当前不可见。"
        if actual.minimized:
            return CODE_WINDOW_MINIMIZED, "目标窗口已最小化。"
        if check_rect and tuple(int(v) for v in actual.rect) != tuple(
            int(v) for v in expected_rect
        ):
            return CODE_WINDOW_MOVED, "目标窗口的位置或尺寸已经变化。"
        return "", ""

    def focus_window(
        self,
        *,
        window: WindowInfo,
        window_rect: tuple[int, int, int, int],
    ) -> FocusResult:
        stop = self._controller.block_reason()
        if stop:
            return FocusResult(False, CODE_EMERGENCY_STOP, stop)

        status = self.status()
        if not status.available:
            return FocusResult(
                False, CODE_BACKEND_UNAVAILABLE, status.reason
            )

        blocked, reason = self.secure_desktop()
        if blocked:
            return FocusResult(False, CODE_SECURE_DESKTOP, reason)

        actual = self._lookup(int(window.hwnd))
        code, reason = self._window_problem(
            window, actual, window_rect, check_rect=True
        )
        if code:
            return FocusResult(False, code, reason)

        try:
            ok, focus_reason = self._api.focus_window(int(window.hwnd))
        except Exception as exc:  # noqa: BLE001
            return FocusResult(
                False,
                CODE_FOCUS_FAILED,
                f"聚焦时异常：{type(exc).__name__}: {exc}",
            )
        if not ok:
            return FocusResult(False, CODE_FOCUS_FAILED, focus_reason)

        # 聚焦之后前台必须是目标窗口。系统可能接受了调用却没真的切过去。
        try:
            foreground = self._api.get_foreground_window()
        except Exception as exc:  # noqa: BLE001
            return FocusResult(
                False,
                CODE_FOREGROUND_MISMATCH,
                f"读取前台窗口失败：{type(exc).__name__}: {exc}",
            )
        problem = identity_problem(window, foreground)
        if problem:
            return FocusResult(
                False,
                CODE_FOREGROUND_MISMATCH,
                f"聚焦后前台不是目标窗口：{problem}",
            )
        return FocusResult(True, "", "", foreground=foreground)

    def foreground_matches(self, window: WindowInfo) -> tuple[bool, str]:
        stop = self._controller.block_reason()
        if stop:
            return False, stop
        try:
            foreground = self._api.get_foreground_window()
        except Exception as exc:  # noqa: BLE001
            return False, f"读取前台窗口失败：{type(exc).__name__}: {exc}"
        problem = identity_problem(window, foreground)
        if problem:
            return False, f"前台不是预期窗口：{problem}"
        return True, ""

    # ------------------------------------------------------------------
    # 点击
    # ------------------------------------------------------------------
    def _fail(
        self,
        code: str,
        reason: str,
        *,
        screen_x: int,
        screen_y: int,
        hwnd: int,
    ) -> ClickOutcome:
        return ClickOutcome(
            status=CLICK_FAILED,
            code=code,
            reason=reason,
            hwnd=hwnd,
            screen_x=screen_x,
            screen_y=screen_y,
            simulated=False,
        )

    def click(
        self,
        *,
        window: WindowInfo,
        screen_x: int,
        screen_y: int,
    ) -> ClickOutcome:
        hwnd = int(window.hwnd)
        x = int(screen_x)
        y = int(screen_y)

        # ---- 1. 后端可用、急停、安全桌面 ----
        available, reason = self._api.available()
        if not available:
            return self._fail(
                CODE_BACKEND_UNAVAILABLE, reason, screen_x=x, screen_y=y, hwnd=hwnd
            )
        stop = self._controller.block_reason()
        if stop:
            return self._fail(
                CODE_EMERGENCY_STOP, stop, screen_x=x, screen_y=y, hwnd=hwnd
            )
        blocked, reason = self.secure_desktop()
        if blocked:
            return self._fail(
                CODE_SECURE_DESKTOP, reason, screen_x=x, screen_y=y, hwnd=hwnd
            )

        # ---- 2. 窗口身份、可见性、位置 ----
        actual = self._lookup(hwnd)
        code, reason = self._window_problem(
            window, actual, window.rect, check_rect=True
        )
        if code:
            return self._fail(
                code, reason, screen_x=x, screen_y=y, hwnd=hwnd
            )

        # ---- 3. 前台窗口 ----
        try:
            foreground = self._api.get_foreground_window()
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                CODE_FOREGROUND_MISMATCH,
                f"读取前台窗口失败：{type(exc).__name__}: {exc}",
                screen_x=x,
                screen_y=y,
                hwnd=hwnd,
            )
        problem = identity_problem(window, foreground)
        if problem:
            return self._fail(
                CODE_FOREGROUND_MISMATCH,
                f"点击前前台不是目标窗口：{problem}",
                screen_x=x,
                screen_y=y,
                hwnd=hwnd,
            )

        # ---- 4. 当前的虚拟桌面度量与坐标范围 ----
        metrics = self._api.screen_metrics()
        if metrics is None:
            return self._fail(
                CODE_BACKEND_UNAVAILABLE,
                "无法读取当前屏幕度量，拒绝发送输入。",
                screen_x=x,
                screen_y=y,
                hwnd=hwnd,
            )
        bounds_reason = self._bounds_problem(x, y, metrics)
        if bounds_reason:
            return self._fail(
                CODE_CLICK_FAILED, bounds_reason, screen_x=x, screen_y=y, hwnd=hwnd
            )

        # ---- 5. 占用一次速率额度（急停也在这里再查一次）----
        allowed, reason = self._controller.acquire_click()
        if not allowed:
            code = (
                CODE_EMERGENCY_STOP
                if self._controller.emergency.stopped
                else CODE_RATE_LIMITED
            )
            return self._fail(
                code, reason, screen_x=x, screen_y=y, hwnd=hwnd
            )

        # ---- 6. 发送前最后一次核对：急停、安全桌面、窗口、前台 ----
        stop = self._controller.block_reason()
        if stop:
            return self._fail(
                CODE_EMERGENCY_STOP, stop, screen_x=x, screen_y=y, hwnd=hwnd
            )
        blocked, reason = self.secure_desktop()
        if blocked:
            return self._fail(
                CODE_SECURE_DESKTOP, reason, screen_x=x, screen_y=y, hwnd=hwnd
            )
        actual = self._lookup(hwnd)
        code, reason = self._window_problem(
            window, actual, window.rect, check_rect=True
        )
        if code:
            return self._fail(
                code, reason, screen_x=x, screen_y=y, hwnd=hwnd
            )
        try:
            foreground = self._api.get_foreground_window()
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                CODE_FOREGROUND_MISMATCH,
                f"发送前读取前台窗口失败：{type(exc).__name__}: {exc}",
                screen_x=x,
                screen_y=y,
                hwnd=hwnd,
            )
        problem = identity_problem(window, foreground)
        if problem:
            return self._fail(
                CODE_FOREGROUND_MISMATCH,
                f"发送前前台不是目标窗口：{problem}",
                screen_x=x,
                screen_y=y,
                hwnd=hwnd,
            )

        # ---- 7. 坐标处最顶层的窗口必须就是目标窗口 ----
        # 前台是目标窗口，不代表 (x, y) 没被别的置顶窗口/弹窗遮挡。
        # 适配器不支持这项检查时也失败关闭，而不是跳过。
        hit_test = getattr(self._api, "root_window_at_point", None)
        if hit_test is None:
            return self._fail(
                CODE_POINT_NOT_TARGET,
                "输入适配器不支持坐标命中检查，无法确认点击落点，拒绝发送输入。",
                screen_x=x,
                screen_y=y,
                hwnd=hwnd,
            )
        try:
            at_point = hit_test(x, y)
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                CODE_POINT_NOT_TARGET,
                f"读取坐标处窗口失败：{type(exc).__name__}: {exc}。"
                "无法确认点击落点，拒绝发送输入。",
                screen_x=x,
                screen_y=y,
                hwnd=hwnd,
            )
        if at_point is None:
            return self._fail(
                CODE_POINT_NOT_TARGET,
                "无法确认坐标处属于哪个窗口，拒绝发送输入。",
                screen_x=x,
                screen_y=y,
                hwnd=hwnd,
            )
        if int(at_point) != hwnd:
            return self._fail(
                CODE_POINT_NOT_TARGET,
                f"坐标 ({x}, {y}) 处最顶层的窗口是 {int(at_point):#x}，"
                f"不是目标窗口 {hwnd:#x}。可能被其他窗口遮挡或窗口已经变化，"
                "拒绝发送输入。",
                screen_x=x,
                screen_y=y,
                hwnd=hwnd,
            )

        # ---- 8. 真正发送。之后无论结果如何都不能自动重试 ----
        try:
            result = self._api.send_left_click(
                screen_x=x, screen_y=y, metrics=metrics
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("真实点击时适配层抛出异常")
            return ClickOutcome(
                status=CLICK_UNKNOWN,
                code=CODE_CLICK_UNKNOWN,
                reason=(
                    f"真实点击适配层抛出异常（{type(exc).__name__}: {exc}），"
                    "无法确认是否已经执行。"
                ),
                hwnd=hwnd,
                screen_x=x,
                screen_y=y,
                simulated=False,
            )

        if result.raised:
            return ClickOutcome(
                status=CLICK_UNKNOWN,
                code=CODE_CLICK_UNKNOWN,
                reason=(
                    f"{result.error} 无法确认是否已经提交了输入事件。"
                ),
                hwnd=hwnd,
                screen_x=x,
                screen_y=y,
                simulated=False,
            )
        if result.inserted >= result.expected and result.expected > 0:
            return ClickOutcome(
                status=CLICK_SUCCEEDED,
                code="",
                reason=(
                    f"Windows SendInput 已提交全部 {result.expected} 个"
                    "鼠标事件（移动、左键按下、左键抬起）。"
                ),
                hwnd=hwnd,
                screen_x=x,
                screen_y=y,
                simulated=False,
            )
        if result.inserted <= 0:
            return self._fail(
                CODE_CLICK_FAILED,
                result.error or "SendInput 没有提交任何事件，未执行点击。",
                screen_x=x,
                screen_y=y,
                hwnd=hwnd,
            )
        return ClickOutcome(
            status=CLICK_UNKNOWN,
            code=CODE_CLICK_UNKNOWN,
            reason=(
                result.error
                or f"SendInput 只提交了 {result.inserted}/{result.expected} 个事件。"
            ),
            hwnd=hwnd,
            screen_x=x,
            screen_y=y,
            simulated=False,
        )

    @staticmethod
    def _bounds_problem(
        x: int, y: int, metrics: ScreenMetrics
    ) -> str:
        left = int(metrics.virtual_left)
        top = int(metrics.virtual_top)
        right = left + int(metrics.virtual_width)
        bottom = top + int(metrics.virtual_height)
        if not (left <= x < right and top <= y < bottom):
            return (
                f"服务端坐标 ({x}, {y}) 不在当前虚拟桌面 "
                f"[{left},{top} - {right},{bottom}) 内，拒绝发送输入。"
            )
        return ""


def build_input_backend(
    *,
    input_config: Any = None,
    env: Mapping[str, str] | None = None,
    api: Any = None,
    controller: control.RealInputController | None = None,
    window_lookup: Callable[[int], WindowInfo | None] | None = None,
) -> InputBackend | None:
    """按双重条件构造真实后端；不满足就返回 None（失败关闭）。

    这是生产代码里**唯一**构造真实后端的地方。`api` / `controller` 只供
    测试注入假适配器和隔离的时钟；正常调用不传。
    """
    try:
        decision = real_mouse_enablement(input_config, env=env)
    except Exception:  # noqa: BLE001 - 读不到启用条件也必须失败关闭
        logger.exception("读取真实输入启用条件失败，本次不启用真实输入")
        return None

    if not decision.allowed:
        logger.info("%s", decision.reason)
        return None

    try:
        return WindowsInputBackend(
            api=api,
            controller=controller,
            input_config=input_config,
            window_lookup=window_lookup,
        )
    except RealInputDisabled as exc:
        logger.warning("真实输入没有启用：%s", exc)
        return None
    except Exception:  # noqa: BLE001 - 构造失败也要失败关闭
        logger.exception("真实输入后端构造失败，本次不启用真实输入")
        return None
