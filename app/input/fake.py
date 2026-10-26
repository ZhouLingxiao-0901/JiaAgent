"""假输入后端：只记录，不碰操作系统。

它存在的意义是让"授权 -> 校验 -> 聚焦 -> 复核 -> 点击 -> 报告"整条链路
可以被离线测试。它**不会**调用 SendInput、mouse_event、keybd_event 或
任何其它真实输入接口，也不会移动真实鼠标。

测试可以控制：
  - 后端是否可用（available）
  - 聚焦成功 / 失败 / 前台被抢走（focus_mode）
  - 点击成功 / 失败 / 结果未知（click_mode）
  - 当前是否处于安全桌面（secure_desktop_mode）
  - 聚焦后、点击前故意改变窗口状态（on_focus / on_click 钩子）

所有"做过的事"都记在 focus_calls / clicks / foreground_checks 里，
断言不用去猜。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from app.input.backend import (
    CLICK_FAILED,
    CLICK_SUCCEEDED,
    CLICK_UNKNOWN,
    CODE_BACKEND_UNAVAILABLE,
    CODE_CLICK_FAILED,
    CODE_CLICK_UNKNOWN,
    CODE_FOCUS_FAILED,
    CODE_FOREGROUND_MISMATCH,
    CODE_SECURE_DESKTOP,
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
)
from app.vision.model import WindowInfo

# 聚焦模式
FOCUS_SUCCEED = "succeed"          # 正常带到前台
FOCUS_FAIL = "fail"                # 系统拒绝聚焦
FOCUS_MISMATCH = "mismatch"        # 聚焦后前台变成了别的窗口
FOCUS_GONE = "gone"                # 聚焦前窗口已经关闭
FOCUS_CHANGED = "changed"          # 聚焦前窗口身份已经变化

# 点击模式
CLICK_MODE_SUCCEED = "succeed"
CLICK_MODE_FAIL = "fail"
CLICK_MODE_UNKNOWN = "unknown"


class FakeInputBackend(InputBackend):
    """只记录模拟操作的后端。"""

    name = "fake"
    simulated = True

    def __init__(
        self,
        window_lookup: Callable[[int], WindowInfo | None],
        *,
        available: bool = True,
        available_reason: str = "",
        focus_mode: str = FOCUS_SUCCEED,
        click_mode: str | Callable[..., ClickOutcome] = CLICK_MODE_SUCCEED,
        secure_desktop_mode: bool = False,
        secure_reason: str = "当前处于 UAC / 安全桌面，不能操作普通窗口。",
        focus_reason: str = "系统拒绝把目标窗口带到前台。",
        click_reason: str = "模拟输入后端报告点击失败。",
        unknown_reason: str = "输入后端超时，无法确认点击是否生效。",
        other_foreground: WindowInfo | None = None,
        on_focus: Callable[["FakeInputBackend"], None] | None = None,
        on_click: Callable[["FakeInputBackend"], None] | None = None,
    ) -> None:
        # window_lookup 必须由测试提供。没有它就无法核对目标身份，
        # 这种情况按"聚焦失败"处理，绝不假装成功。
        self._lookup = window_lookup
        self.available = bool(available)
        self.available_reason = available_reason
        self.focus_mode = focus_mode
        self.click_mode = click_mode
        self.secure_desktop_mode = bool(secure_desktop_mode)
        self.secure_reason = secure_reason
        self.focus_reason = focus_reason
        self.click_reason = click_reason
        self.unknown_reason = unknown_reason
        self.other_foreground = other_foreground
        self.on_focus = on_focus
        self.on_click = on_click

        self.foreground: WindowInfo | None = None
        self.focus_calls: list[dict[str, object]] = []
        self.clicks: list[dict[str, object]] = []
        self.foreground_checks: list[int] = []
        self.secure_checks = 0

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def status(self) -> BackendStatus:
        if not self.available:
            return BackendStatus(
                available=False,
                reason=self.available_reason or "没有配置可用的输入后端。",
                simulated=True,
            )
        return BackendStatus(available=True, simulated=True)

    def secure_desktop(self) -> tuple[bool, str]:
        self.secure_checks += 1
        if self.secure_desktop_mode:
            return True, self.secure_reason
        return False, ""

    # ------------------------------------------------------------------
    # 聚焦
    # ------------------------------------------------------------------
    def focus_window(
        self,
        *,
        window: WindowInfo,
        window_rect: tuple[int, int, int, int],
    ) -> FocusResult:
        self.focus_calls.append({
            "hwnd": int(window.hwnd),
            "window_rect": tuple(int(v) for v in window_rect),
        })

        status = self.status()
        if not status.available:
            return FocusResult(
                False, CODE_BACKEND_UNAVAILABLE, status.reason
            )
        blocked, reason = self.secure_desktop()
        if blocked:
            return FocusResult(False, CODE_SECURE_DESKTOP, reason)

        if self.focus_mode == FOCUS_GONE:
            return FocusResult(
                False, CODE_WINDOW_GONE,
                "模拟：聚焦前目标窗口已经关闭。",
            )
        if self.focus_mode == FOCUS_CHANGED:
            return FocusResult(
                False, CODE_WINDOW_CHANGED,
                "模拟：聚焦前目标窗口的身份已经变化。",
            )

        actual = self._lookup(int(window.hwnd))
        if actual is None:
            return FocusResult(
                False, CODE_WINDOW_GONE,
                "目标窗口已关闭或句柄失效，不能聚焦。",
            )
        problem = identity_problem(window, actual)
        if problem:
            return FocusResult(False, CODE_WINDOW_CHANGED, problem)
        if not actual.visible:
            return FocusResult(
                False, CODE_WINDOW_HIDDEN, "目标窗口当前不可见，不能聚焦。"
            )
        if actual.minimized:
            return FocusResult(
                False, CODE_WINDOW_MINIMIZED, "目标窗口已最小化，不能聚焦。"
            )
        if tuple(int(v) for v in actual.rect) != tuple(
            int(v) for v in window_rect
        ):
            return FocusResult(
                False, CODE_WINDOW_MOVED,
                "目标窗口的位置或尺寸已经变化，不能按旧记录聚焦。",
            )

        if self.focus_mode == FOCUS_FAIL:
            return FocusResult(False, CODE_FOCUS_FAILED, self.focus_reason)

        if self.focus_mode == FOCUS_MISMATCH:
            # 模拟"前台在聚焦后被别的窗口抢走"。工具必须在真正点击前
            # 再查一次前台，不能只信 focus_window 说自己成功。
            self.foreground = self.other_foreground or replace(
                actual, hwnd=actual.hwnd + 1, title="（别的窗口）"
            )
            return FocusResult(
                True, "", "模拟：已尝试聚焦，但前台被其他窗口占用。",
                foreground=self.foreground,
            )

        self.foreground = actual
        if self.on_focus is not None:
            self.on_focus(self)
        return FocusResult(
            True, "", "模拟：已把目标窗口带到前台。", foreground=self.foreground
        )

    def foreground_matches(self, window: WindowInfo) -> tuple[bool, str]:
        self.foreground_checks.append(int(window.hwnd))
        if self.foreground is None:
            return False, "没有前台窗口，不能确认目标仍然在前台。"
        problem = identity_problem(window, self.foreground)
        if problem:
            return False, f"前台不是预期窗口：{problem}"
        return True, ""

    # ------------------------------------------------------------------
    # 点击
    # ------------------------------------------------------------------
    def click(
        self,
        *,
        window: WindowInfo,
        screen_x: int,
        screen_y: int,
    ) -> ClickOutcome:
        if self.on_click is not None:
            self.on_click(self)

        record = {
            "hwnd": int(window.hwnd),
            "screen_x": int(screen_x),
            "screen_y": int(screen_y),
            "window_rect": tuple(int(v) for v in window.rect),
            "simulated": True,
            "status": "",
            "code": "",
            "reason": "",
        }
        self.clicks.append(record)

        status = self.status()
        if not status.available:
            return self._finish(
                record, CLICK_FAILED, CODE_BACKEND_UNAVAILABLE, status.reason
            )
        blocked, reason = self.secure_desktop()
        if blocked:
            return self._finish(
                record, CLICK_FAILED, CODE_SECURE_DESKTOP, reason
            )

        # 真正"按下去"之前最后再核对一次窗口身份和前台。
        # 这不是形式主义：窗口可能在聚焦之后、点击之前被关掉或换掉。
        actual = self._lookup(int(window.hwnd))
        if actual is None:
            return self._finish(
                record, CLICK_FAILED, CODE_WINDOW_GONE,
                "点击前目标窗口已经关闭，未发送点击。",
            )
        problem = identity_problem(window, actual)
        if problem:
            return self._finish(
                record, CLICK_FAILED, CODE_WINDOW_CHANGED,
                f"点击前目标窗口身份变化，未发送点击：{problem}",
            )
        fg_ok, fg_reason = self.foreground_matches(window)
        if not fg_ok:
            return self._finish(
                record, CLICK_FAILED, CODE_FOREGROUND_MISMATCH,
                f"点击前前台窗口不匹配，未发送点击：{fg_reason}",
            )

        if callable(self.click_mode):
            outcome = self.click_mode(window=window, screen_x=screen_x, screen_y=screen_y)
            return self._finish(
                record, outcome.status, outcome.code,
                outcome.reason, outcome=outcome,
            )
        if self.click_mode == CLICK_MODE_FAIL:
            return self._finish(
                record, CLICK_FAILED, CODE_CLICK_FAILED, self.click_reason
            )
        if self.click_mode == CLICK_MODE_UNKNOWN:
            return self._finish(
                record, CLICK_UNKNOWN, CODE_CLICK_UNKNOWN, self.unknown_reason
            )
        return self._finish(
            record, CLICK_SUCCEEDED, "",
            "模拟点击完成（没有发送真实鼠标输入）。",
        )

    @staticmethod
    def _finish(
        record: dict[str, object],
        status: str,
        code: str,
        reason: str,
        *,
        outcome: ClickOutcome | None = None,
    ) -> ClickOutcome:
        record["status"] = status
        record["code"] = code
        record["reason"] = reason
        if outcome is not None:
            record["screen_x"] = outcome.screen_x if outcome.screen_x is not None else record["screen_x"]
            record["screen_y"] = outcome.screen_y if outcome.screen_y is not None else record["screen_y"]
        return ClickOutcome(
            status=status,
            code=code,
            reason=reason,
            hwnd=int(record["hwnd"]),
            screen_x=int(record["screen_x"]),
            screen_y=int(record["screen_y"]),
            simulated=True,
        )
