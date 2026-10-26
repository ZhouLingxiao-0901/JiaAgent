"""真实 Windows 鼠标后端的离线测试。

**不发送任何真实输入。** 所有 Windows API 都由 `FakeApi` 替代，
坐标、返回值和异常都在测试里控制。测试覆盖：

  - 双重启用条件（配置 + 环境变量，缺一不可）
  - 未启用时构造失败 / 登记处拿不到后端
  - 安全桌面、急停、速率限制
  - 窗口关闭、移动、最小化、身份变化、前台被抢
  - 负坐标 / 多显示器虚拟桌面、越界坐标
  - 坐标处最顶层窗口的命中检查（被遮挡、无窗口、异常、适配器不支持）
  - SendInput 全部成功、一个都没提交、部分提交、抛异常
  - 部分提交后补发一次左键抬起（不重试点击）、补发失败时提示人工检查
  - 适配器级安全桌面判断、桌面句柄关闭、ctypes 句柄原型
  - 急停持久化、恢复需要显式确认
  - 只允许 windows_api.py 调用 SendInput

用法：
    .venv\\Scripts\\python.exe scripts\\test_windows_backend.py
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import ctypes
import sys
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from app.agent import gate, pending  # noqa: E402
from app.input import control  # noqa: E402
from app.input.backend import (  # noqa: E402
    CLICK_FAILED,
    CLICK_SUCCEEDED,
    CLICK_UNKNOWN,
    CODE_CLICK_FAILED,
    CODE_CLICK_UNKNOWN,
    CODE_EMERGENCY_STOP,
    CODE_FOCUS_FAILED,
    CODE_FOREGROUND_MISMATCH,
    CODE_POINT_NOT_TARGET,
    CODE_RATE_LIMITED,
    CODE_SECURE_DESKTOP,
    CODE_UNSAFE_TARGET,
    CODE_WINDOW_MINIMIZED,
    CODE_WINDOW_MOVED,
)
from app.input import windows_api  # noqa: E402
from app.input.windows_api import SendInputResult, Win32InputApi  # noqa: E402
from app.input.windows_backend import (  # noqa: E402
    ENV_REAL_MOUSE,
    RealInputDisabled,
    WindowsInputBackend,
    build_input_backend,
    real_mouse_enablement,
)
from app.settings import InputConfig  # noqa: E402
from app.tools.click_tools import ClickElementTool  # noqa: E402
from app.vision import dpi, observation  # noqa: E402
from app.vision.analyze import ScreenReading, UiElement  # noqa: E402
from app.vision.model import ScreenMetrics, Snapshot, WindowInfo  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
SESSION = "test-windows-backend"
ANSWERED: set[str] = set()


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


# ----------------------------------------------------------------------
# 假环境
# ----------------------------------------------------------------------
class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


def win_info(
    *,
    hwnd: int = 7,
    title: str = "记事本",
    process: str = "notepad.exe",
    pid: int = 42,
    class_name: str = "Notepad",
    rect: tuple[int, int, int, int] = (-1500, -100, -500, 400),
    visible: bool = True,
    minimized: bool = False,
) -> WindowInfo:
    return WindowInfo(
        hwnd=hwnd,
        title=title,
        pid=pid,
        process=process,
        class_name=class_name,
        rect=rect,
        visible=visible,
        minimized=minimized,
        rect_source="dwm_frame",
    )


def screen_metrics(
    *,
    left: int = -1920,
    top: int = -200,
    width: int = 3840,
    height: int = 1280,
) -> ScreenMetrics:
    """双屏且副屏在左边/上边：虚拟桌面原点为负。"""
    return ScreenMetrics(
        primary_width=1920,
        primary_height=1080,
        virtual_left=left,
        virtual_top=top,
        virtual_width=width,
        virtual_height=height,
        monitor_count=2,
        dpi=144,
        scale=1.5,
        awareness="per_monitor",
        awareness_method="test",
        consistent=True,
    )


class FakeApi:
    """完全离线的 Windows 适配器替身。"""

    name = "fake-win32"

    def __init__(
        self,
        window: WindowInfo,
        *,
        metrics: ScreenMetrics | None = None,
        available: bool = True,
        available_reason: str = "",
        secure: bool = False,
        secure_error: Exception | None = None,
        focus_mode: str = "ok",
        focus_foreground: WindowInfo | None = None,
        send_mode: str = "succeed",
        send_inserted: int | None = None,
        send_error: str = "",
        point_mode: str = "ok",
        point_hwnd: int | None = None,
        point_error: Exception | None = None,
        on_focus=None,
        on_send=None,
    ) -> None:
        self.window = window
        self.metrics = metrics or screen_metrics()
        self.available_ok = available
        self.available_reason = available_reason
        self.secure = secure
        self.secure_error = secure_error
        self.focus_mode = focus_mode
        self.focus_foreground = focus_foreground
        self.send_mode = send_mode
        self.send_inserted = send_inserted
        self.send_error = send_error
        self.point_mode = point_mode
        self.point_hwnd = point_hwnd
        self.point_error = point_error
        self.on_focus = on_focus
        self.on_send = on_send

        self.foreground: WindowInfo | None = window
        self.send_calls: list[dict] = []
        self.focus_calls: list[int] = []
        self.hit_tests: list[tuple[int, int]] = []
        self.secure_checks = 0
        self.available_checks = 0

    def available(self):
        self.available_checks += 1
        return self.available_ok, self.available_reason

    def get_window_info(self, hwnd: int):
        if self.window is None:
            return None
        return self.window if int(hwnd) == int(self.window.hwnd) else None

    def get_foreground_window(self):
        return self.foreground

    def screen_metrics(self):
        return self.metrics

    def focus_window(self, hwnd: int):
        self.focus_calls.append(int(hwnd))
        if self.focus_mode == "fail":
            return False, "模拟：系统拒绝聚焦"
        self.foreground = self.focus_foreground or self.window
        if self.on_focus is not None:
            self.on_focus(self)
        return True, ""

    def secure_desktop(self):
        self.secure_checks += 1
        if self.secure_error is not None:
            raise self.secure_error
        if self.secure:
            return True, "模拟：当前处于 UAC / 锁屏安全桌面"
        return False, ""

    def root_window_at_point(self, x: int, y: int):
        self.hit_tests.append((int(x), int(y)))
        if self.point_error is not None:
            raise self.point_error
        if self.point_mode == "none":
            return None
        if self.point_mode == "zero":
            return 0
        if self.point_mode == "other":
            return int(self.point_hwnd or 0x1234)
        return int(self.window.hwnd) if self.window is not None else 0

    def send_left_click(self, *, screen_x: int, screen_y: int, metrics):
        self.send_calls.append({
            "screen_x": int(screen_x),
            "screen_y": int(screen_y),
            "virtual_rect": (
                metrics.virtual_left,
                metrics.virtual_top,
                metrics.virtual_width,
                metrics.virtual_height,
            ),
            # 发送这一刻已经做过的命中检查，用来确认顺序（检查在发送之前）。
            "hit_tests": list(self.hit_tests),
        })
        if self.on_send is not None:
            self.on_send(self)
        if self.send_mode == "raise":
            raise RuntimeError("模拟：SendInput 调用异常")
        if self.send_mode == "zero":
            return SendInputResult(0, 3, self.send_error or "模拟：一个事件都没提交")
        if self.send_mode == "partial":
            return SendInputResult(
                self.send_inserted if self.send_inserted is not None else 2,
                3,
                self.send_error or "模拟：只提交了一部分事件",
            )
        return SendInputResult(3, 3, self.send_error)


class Controller:
    def __init__(self, start: float = 1000.0) -> None:
        self.clock = Clock(start)
        self.emergency = control.EmergencyStop(path=None)
        self.limiter = control.ClickRateLimiter(clock=self.clock)
        self.controller = control.RealInputController(
            emergency=self.emergency, limiter=self.limiter
        )


def make_backend(
    api: FakeApi,
    *,
    config_enabled: bool = True,
    env_enabled: bool = True,
    controller: Controller | None = None,
):
    ctrl = controller or Controller()
    cfg = InputConfig(real_mouse_enabled=config_enabled)
    env = {ENV_REAL_MOUSE: "1"} if env_enabled else {}
    backend = build_input_backend(
        input_config=cfg,
        env=env,
        api=api,
        controller=ctrl.controller,
        window_lookup=api.get_window_info,
    )
    return backend, ctrl


# ----------------------------------------------------------------------
# 一、双重启用条件
# ----------------------------------------------------------------------
def test_enablement() -> None:
    print("\n[一] 双重启用条件")

    off = InputConfig(real_mouse_enabled=False)
    on = InputConfig(real_mouse_enabled=True)

    d = real_mouse_enablement(off, env={ENV_REAL_MOUSE: "1"})
    check("只有环境变量、配置关闭时不启用", not d.allowed)
    d = real_mouse_enablement(on, env={})
    check("只有配置、环境变量缺失时不启用", not d.allowed)
    d = real_mouse_enablement(on, env={ENV_REAL_MOUSE: "0"})
    check("环境变量为 0 时不启用", not d.allowed)
    d = real_mouse_enablement(on, env={ENV_REAL_MOUSE: "true"})
    check("环境变量必须是 1，true 不算", not d.allowed)
    d = real_mouse_enablement(on, env={ENV_REAL_MOUSE: "1"})
    check("配置默认值与独立环境变量同时满足才启用", d.allowed)

    api = FakeApi(win_info())
    backend, _ = make_backend(api, config_enabled=False)
    check("条件不全时工厂返回 None", backend is None)
    backend, _ = make_backend(api, env_enabled=False)
    check("缺少环境变量时工厂返回 None", backend is None)
    check("条件不全时没有发出任何输入", not api.send_calls)

    backend, _ = make_backend(api)
    check("双重条件满足时工厂返回真实后端",
          isinstance(backend, WindowsInputBackend))
    check("真实后端不标记为模拟",
          backend is not None and backend.simulated is False)

    try:
        WindowsInputBackend(input_config=off)
    except RealInputDisabled:
        blocked = True
    else:
        blocked = False
    check("直接构造真实后端也会检查双重条件", blocked)

    from app.tools.registry import registry

    tool = registry.get("click_element")
    check("默认登记处没有真实后端",
          tool is not None and getattr(tool, "_backend", None) is None)


# ----------------------------------------------------------------------
# 二、急停
# ----------------------------------------------------------------------
def test_emergency_stop() -> None:
    print("\n[二] 急停")

    emergency = control.EmergencyStop(path=None)
    check("默认没有急停", emergency.block_reason() == "")

    state = emergency.trigger("测试按下急停")
    check("触发后进入停止状态", state.stopped is True)
    check("急停原因可读", "测试按下急停" in emergency.block_reason())
    check("急停提示说明模型不能清除",
          "模型和工作流不能清除" in emergency.block_reason())

    ok, message = emergency.reset(confirmed=False)
    check("没有显式确认不能恢复", not ok and emergency.stopped)

    ok, _ = emergency.reset(confirmed=True)
    check("显式确认后可以恢复", ok and not emergency.stopped)

    # 持久化：新实例要读回停止状态。
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "stop.json"
        first = control.EmergencyStop(path=path)
        first.trigger("持久化测试")
        second = control.EmergencyStop(path=path)
        check("重启后仍然保持急停", second.stopped is True)
        check("重启后原因仍然存在", "持久化测试" in second.block_reason())
        second.reset(confirmed=True)
        third = control.EmergencyStop(path=path)
        check("显式恢复后重启不再停止", third.stopped is False)

        # 状态文件坏了必须失败关闭，不能因为读不出来就当没急停。
        path.write_text("{ 这不是合法 JSON", encoding="utf-8")
        broken = control.EmergencyStop(path=path)
        check("急停状态文件损坏时按已急停处理", broken.stopped is True)
        check("损坏状态的原因明确", "无法读取" in broken.block_reason(),
              broken.block_reason()[:60])

    # 后端 / 工具层面的拒绝。
    api = FakeApi(win_info())
    backend, ctrl = make_backend(api)
    ctrl.emergency.trigger("测试")
    status = backend.status()
    check("急停时后端报告不可用", not status.available)
    check("急停原因出现在后端状态里", "急停" in status.reason)

    focus = backend.focus_window(
        window=api.window, window_rect=api.window.rect
    )
    check("急停时不聚焦", not focus.ok and focus.code == CODE_EMERGENCY_STOP)
    outcome = backend.click(window=api.window, screen_x=-100, screen_y=300)
    check("急停时不点击", outcome.status == CLICK_FAILED
          and outcome.code == CODE_EMERGENCY_STOP)
    check("急停时没有发出任何输入", not api.send_calls)
    check("急停时连聚焦都没有做", not api.focus_calls)


# ----------------------------------------------------------------------
# 三、速率限制
# ----------------------------------------------------------------------
def test_rate_limit() -> None:
    print("\n[三] 速率限制")

    clock = Clock(0.0)
    limiter = control.ClickRateLimiter(clock=clock)
    check("第一次可以点击", limiter.try_acquire()[0])
    clock.advance(0.5)
    ok, reason = limiter.try_acquire()
    check("不到 1 秒的第二次被拒绝", not ok and "间隔" in reason)
    clock.advance(0.5)
    check("满 1 秒后可以再次点击", limiter.try_acquire()[0])

    clock = Clock(0.0)
    limiter = control.ClickRateLimiter(clock=clock)
    accepted = 0
    for _ in range(5):
        if limiter.try_acquire()[0]:
            accepted += 1
        clock.advance(1.0)
    ok, reason = limiter.try_acquire()
    check("一分钟内最多 5 次", accepted == 5 and not ok)
    check("超限原因说明不会排队", "不会自动排队" in reason)
    clock.advance(60.0)
    check("窗口滑过后恢复额度", limiter.try_acquire()[0])

    # 配置不能把上限放宽。
    loose = control.ClickRateLimiter(
        min_interval=0.1, max_per_minute=100, clock=Clock(0.0)
    )
    check("最小间隔被夹到 1 秒", loose.min_interval >= 1.0)
    check("每分钟上限被夹到 5 次", loose.max_per_minute <= 5)

    # 后端层面：超限时不发送输入。
    api = FakeApi(win_info())
    backend, ctrl = make_backend(api, controller=Controller(0.0))
    statuses = []
    for index in range(6):
        if index:
            ctrl.clock.advance(1.0)
        statuses.append(
            backend.click(window=api.window, screen_x=-100, screen_y=300)
        )
    check("前 5 次真实点击成功",
          all(item.status == CLICK_SUCCEEDED for item in statuses[:5]))
    check("第 6 次被速率限制拒绝",
          statuses[5].status == CLICK_FAILED
          and statuses[5].code == CODE_RATE_LIMITED)
    check("被拒绝的第 6 次没有调用输入接口", len(api.send_calls) == 5)
    check("超限后没有自动重试", len(api.send_calls) == 5)


# ----------------------------------------------------------------------
# 四、安全桌面与窗口核对
# ----------------------------------------------------------------------
def test_security_and_windows() -> None:
    print("\n[四] 安全桌面与窗口核对")

    api = FakeApi(win_info(), secure=True)
    backend, _ = make_backend(api)
    focus = backend.focus_window(
        window=api.window, window_rect=api.window.rect
    )
    check("安全桌面时拒绝聚焦",
          not focus.ok and focus.code == CODE_SECURE_DESKTOP)
    outcome = backend.click(window=api.window, screen_x=-100, screen_y=300)
    check("安全桌面时拒绝点击",
          outcome.status == CLICK_FAILED
          and outcome.code == CODE_SECURE_DESKTOP)
    check("安全桌面时没有发出输入", not api.send_calls)

    api = FakeApi(win_info(), secure_error=RuntimeError("检测失败"))
    backend, _ = make_backend(api)
    blocked, reason = backend.secure_desktop()
    check("安全桌面检测异常时失败关闭", blocked and "异常" in reason)

    api = FakeApi(win_info(), available=False, available_reason="没有权限")
    backend, _ = make_backend(api)
    check("适配器不可用时 status 失败关闭",
          not backend.status().available)
    outcome = backend.click(window=api.window, screen_x=-100, screen_y=300)
    check("适配器不可用时绝不点击", not api.send_calls)

    # 窗口最小化 / 移动 / 身份变化：聚焦前就停下。
    for name, changed, expected in (
        ("最小化", replace(win_info(), minimized=True), CODE_WINDOW_MINIMIZED),
        ("移动", replace(win_info(), rect=(-1400, -100, -400, 400)), CODE_WINDOW_MOVED),
        ("标题变化", replace(win_info(), title="别的窗口"), "window_changed"),
        ("窗口关闭", None, "window_gone"),
    ):
        api = FakeApi(win_info())
        if changed is None:
            api.window = None
        else:
            api.window = changed
        backend, _ = make_backend(api)
        focus = backend.focus_window(
            window=win_info(), window_rect=win_info().rect
        )
        check(f"窗口{name}时拒绝聚焦", not focus.ok and focus.code == expected)
        check(f"窗口{name}时没有发出输入", not api.send_calls)

    # 防御性检查：即使工具层漏了，后端自己也不操作 UAC / 锁屏窗口。
    api = FakeApi(win_info(process="consent.exe", title="用户账户控制"))
    backend, _ = make_backend(api)
    focus = backend.focus_window(
        window=api.window, window_rect=api.window.rect
    )
    check("目标是 UAC 进程时后端拒绝聚焦",
          not focus.ok and focus.code == CODE_UNSAFE_TARGET)
    outcome = backend.click(window=api.window, screen_x=-100, screen_y=300)
    check("目标是 UAC 进程时后端拒绝点击",
          outcome.status == CLICK_FAILED
          and outcome.code == CODE_UNSAFE_TARGET)
    check("UAC 目标没有发出输入", not api.send_calls)

    api = FakeApi(win_info(), focus_mode="fail")
    backend, _ = make_backend(api)
    focus = backend.focus_window(
        window=api.window, window_rect=api.window.rect
    )
    check("聚焦失败时明确失败",
          not focus.ok and focus.code == CODE_FOCUS_FAILED)

    other = replace(win_info(), hwnd=8, title="别的窗口")
    api = FakeApi(win_info(), focus_foreground=other)
    backend, _ = make_backend(api)
    focus = backend.focus_window(
        window=api.window, window_rect=api.window.rect
    )
    check("聚焦后前台不是目标时失败",
          not focus.ok and focus.code == CODE_FOREGROUND_MISMATCH)

    # 聚焦成功，但点击前后台被抢走。
    api = FakeApi(win_info())
    backend, _ = make_backend(api)
    focus = backend.focus_window(
        window=api.window, window_rect=api.window.rect
    )
    api.foreground = other
    baseline = len(api.focus_calls)
    outcome = backend.click(window=api.window, screen_x=-100, screen_y=300)
    check("聚焦成功后前台被抢走，点击前发现",
          focus.ok and outcome.status == CLICK_FAILED
          and outcome.code == CODE_FOREGROUND_MISMATCH)
    check("前台不匹配时没有发出输入", not api.send_calls)
    check("前台核对没有重新聚焦", len(api.focus_calls) == baseline)


# ----------------------------------------------------------------------
# 五、负坐标、多显示器与结果语义
# ----------------------------------------------------------------------
def test_click_and_results() -> None:
    print("\n[五] 负坐标、多显示器与结果语义")

    metrics = screen_metrics()
    window = win_info()
    api = FakeApi(window, metrics=metrics)
    backend, _ = make_backend(api)
    focus = backend.focus_window(window=window, window_rect=window.rect)
    outcome = backend.click(window=window, screen_x=-100, screen_y=300)

    check("双屏负原点下可以聚焦", focus.ok)
    check("双屏负原点下点击成功", outcome.status == CLICK_SUCCEEDED)
    check("真实后端结果标记为非模拟", outcome.simulated is False)
    check("坐标原样交给适配器（物理像素）",
          len(api.send_calls) == 1
          and api.send_calls[0]["screen_x"] == -100
          and api.send_calls[0]["screen_y"] == 300)
    check("适配器拿到的是整个虚拟桌面范围",
          api.send_calls[0]["virtual_rect"] == (-1920, -200, 3840, 1280))
    check("发送前对落点做了命中检查",
          api.send_calls[0]["hit_tests"] == [(-100, 300)])

    ax, ay = dpi.physical_to_input_absolute(-1920, -200, metrics)
    check("虚拟桌面左上角映射为 (0,0)", (ax, ay) == (0, 0))
    ax, ay = dpi.physical_to_input_absolute(-100, 300, metrics)
    check("负坐标映射到 0~65535 绝对坐标",
          ax > 0 and ay > 0 and ax <= 65535 and ay <= 65535)

    # 越界坐标：在发送前拒绝。
    api = FakeApi(window, metrics=metrics)
    backend, _ = make_backend(api)
    focus = backend.focus_window(window=window, window_rect=window.rect)
    outcome = backend.click(window=window, screen_x=5000, screen_y=300)
    check("越界坐标被拒绝",
          focus.ok and outcome.status == CLICK_FAILED
          and outcome.code == CODE_CLICK_FAILED)
    check("越界坐标没有发出输入", not api.send_calls)

    # 结果三分。
    for mode, expected, inserted in (
        ("succeed", CLICK_SUCCEEDED, None),
        ("zero", CLICK_FAILED, None),
        ("partial", CLICK_UNKNOWN, 2),
    ):
        api = FakeApi(window, metrics=metrics, send_mode=mode,
                      send_inserted=inserted)
        backend, _ = make_backend(api)
        backend.focus_window(window=window, window_rect=window.rect)
        outcome = backend.click(window=window, screen_x=-100, screen_y=300)
        check(f"SendInput {mode} 映射为 {expected}",
              outcome.status == expected)

    api = FakeApi(window, metrics=metrics, send_mode="raise")
    backend, _ = make_backend(api)
    backend.focus_window(window=window, window_rect=window.rect)
    outcome = backend.click(window=window, screen_x=-100, screen_y=300)
    check("SendInput 抛异常时结果是 unknown", outcome.status == CLICK_UNKNOWN)
    check("unknown 不会被说成确定失败",
          outcome.code == CODE_CLICK_UNKNOWN and "无法确认" in outcome.reason)

    # 点击前窗口被换掉：后端必须在真正发送前再查一次。
    api = FakeApi(window, metrics=metrics)
    backend, _ = make_backend(api)
    backend.focus_window(window=window, window_rect=window.rect)
    api.window = replace(win_info(), title="被换掉的窗口")
    outcome = backend.click(window=window, screen_x=-100, screen_y=300)
    check("发送前窗口身份变化时拒绝",
          outcome.status == CLICK_FAILED and not api.send_calls)

    # 坐标处最顶层的窗口必须是目标窗口：挡住落点的窗口一律拒绝。
    for kwargs, note in (
        ({"point_mode": "other"}, "坐标处是别的窗口"),
        ({"point_mode": "zero"}, "坐标处没有任何窗口"),
        ({"point_mode": "none"}, "无法确认坐标处属于哪个窗口"),
        ({"point_error": RuntimeError("命中检查失败")}, "命中检查抛异常"),
    ):
        api = FakeApi(window, metrics=metrics, **kwargs)
        backend, _ = make_backend(api)
        backend.focus_window(window=window, window_rect=window.rect)
        outcome = backend.click(window=window, screen_x=-100, screen_y=300)
        check(f"{note}时拒绝点击",
              outcome.status == CLICK_FAILED
              and outcome.code == CODE_POINT_NOT_TARGET)
        check(f"{note}时没有发出输入", not api.send_calls)
        check(f"{note}时确实做了命中检查", api.hit_tests == [(-100, 300)])

    # 适配器不支持命中检查时也必须失败关闭，而不是跳过。
    class LegacyApi(FakeApi):
        root_window_at_point = None

    api = LegacyApi(window, metrics=metrics)
    backend, _ = make_backend(api)
    backend.focus_window(window=window, window_rect=window.rect)
    outcome = backend.click(window=window, screen_x=-100, screen_y=300)
    check("适配器不支持命中检查时失败关闭",
          outcome.status == CLICK_FAILED
          and outcome.code == CODE_POINT_NOT_TARGET
          and not api.send_calls)


# ----------------------------------------------------------------------
# 六、工具集成：确认真实输入 + 急停
# ----------------------------------------------------------------------
def metrics() -> ScreenMetrics:
    return screen_metrics(left=0, top=0, width=1920, height=1080)


def make_observation():
    window = win_info(rect=(100, 50, 900, 650))
    desktop_metrics = metrics()
    store = observation.ObservationStore(
        ttl_seconds=30,
        wall_clock=lambda: datetime(2026, 10, 21, 12, 0, 0),
        window_lookup=lambda _hwnd: window,
        metrics_lookup=lambda: desktop_metrics,
    )
    image = Image.new("RGB", (800, 600), (20, 20, 20))
    snapshot = Snapshot(
        image=image,
        preview=image.resize((400, 300)),
        metrics=desktop_metrics,
        window=window,
        taken_at="2026-10-21T12:00:00",
        original_size=(800, 600),
        preview_size=(400, 300),
    )
    reading = ScreenReading(
        description="测试界面",
        elements=[UiElement(
            name="登录", kind="button", x=0.5, y=0.5,
            width=0.2, height=0.1, confidence=0.9,
        )],
        provider="fake",
        model="fake-vision",
        question="登录按钮在哪",
        scope="window",
    )
    return store, window, store.create_from_capture(
        snapshot, reading, scope="window"
    )


async def gate_and_run(tool, args, *, before_run=None):
    outcome = gate.GateOutcome()
    events: list[dict] = []

    async def answer_when_asked():
        while True:
            await asyncio.sleep(0.005)
            for action in pending.store.pending_for(SESSION):
                if action.id in ANSWERED:
                    continue
                ANSWERED.add(action.id)
                pending.store.answer(action.id, True)
                return

    answerer = asyncio.create_task(answer_when_asked())
    try:
        async for event in gate.authorize_tool(
            tool, args, session_id=SESSION, outcome=outcome, timeout=1.0
        ):
            events.append(event)
    finally:
        answerer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await answerer

    result = None
    if outcome.proceed:
        if before_run is not None:
            before_run()
        result = await tool.execute(args, injected=outcome.injected)
    return outcome, events, result


async def test_tool_integration() -> None:
    print("\n[六] 工具集成：真实输入确认与急停")

    store, window, record = make_observation()
    api = FakeApi(window, metrics=metrics())
    backend, ctrl = make_backend(api)
    tool = ClickElementTool(
        store=store, backend=backend, emergency=ctrl.emergency
    )
    args = {"observation_id": record.observation_id, "element_id": "e1"}

    outcome, events, result = await gate_and_run(tool, args)
    confirm = [
        e for e in events if e.get("type") == "need_confirm"
    ]
    check("真实后端仍会弹出确认卡片", len(confirm) == 1)
    check("确认卡片明确说明是真实输入",
          "真实鼠标左键单击" in outcome.summary
          and "不是模拟" in outcome.summary)
    check("确认卡片包含目标应用和窗口",
          "notepad.exe" in outcome.summary and "记事本" in outcome.summary)
    check("确认后只发送一次点击", len(api.send_calls) == 1)
    check("工具报告成功", result is not None and result.ok)
    check("工具结果显示真实后端",
          result is not None and "windows-sendinput" in result.output)

    # 急停发生在用户确认之后、执行之前：run() 必须再次拦住。
    store2, window2, record2 = make_observation()
    api2 = FakeApi(window2, metrics=metrics())
    backend2, ctrl2 = make_backend(api2)
    tool2 = ClickElementTool(
        store=store2, backend=backend2, emergency=ctrl2.emergency
    )
    args2 = {"observation_id": record2.observation_id, "element_id": "e1"}
    outcome2, events2, result2 = await gate_and_run(
        tool2, args2, before_run=lambda: ctrl2.emergency.trigger("确认后急停")
    )
    check("确认后急停仍然阻止执行",
          result2 is not None and not result2.ok
          and "急停" in result2.error)
    check("确认后急停没有发出输入", not api2.send_calls)

    # 已经急停时，连确认卡片都不该出现。
    store3, window3, record3 = make_observation()
    api3 = FakeApi(window3, metrics=metrics())
    backend3, ctrl3 = make_backend(api3)
    ctrl3.emergency.trigger("提前急停")
    tool3 = ClickElementTool(
        store=store3, backend=backend3, emergency=ctrl3.emergency
    )
    outcome3, events3, result3 = await gate_and_run(
        tool3,
        {"observation_id": record3.observation_id, "element_id": "e1"},
    )
    check("急停时不会创建确认卡片",
          not [e for e in events3 if e.get("type") == "need_confirm"])
    check("急停时工具没有执行", result3 is None and not outcome3.proceed)
    check("急停时没有发出输入", not api3.send_calls)


# ----------------------------------------------------------------------
# 七、适配器级检查：安全桌面、句柄、部分提交后的按键释放
# ----------------------------------------------------------------------
class FakeUser32:
    """假 user32，用来验证适配器自己的判断逻辑（不发真实输入）。"""

    def __init__(
        self,
        *,
        desktop_name: str = "Default",
        open_ok: bool = True,
        info_ok: bool = True,
        send_results: list | None = None,
        point_hwnd: int = 0,
        ancestor_hwnd: int | None = None,
        point_error: Exception | None = None,
    ) -> None:
        self.desktop_name = desktop_name
        self.open_ok = open_ok
        self.info_ok = info_ok
        self._send_results = list(send_results or [])
        self.point_hwnd = point_hwnd
        self.ancestor_hwnd = ancestor_hwnd
        self.point_error = point_error

        self.open_args: tuple | None = None
        self.close_calls: list = []
        self.send_flags: list[list[int]] = []
        self.point_calls: list[tuple[int, int]] = []
        self.ancestor_args: tuple | None = None

    def OpenInputDesktop(self, flags, inherit, access):
        self.open_args = (flags, inherit, access)
        return 0x1234 if self.open_ok else 0

    def GetUserObjectInformationW(self, handle, index, buffer, size, needed):
        if not self.info_ok:
            return 0
        buffer.value = self.desktop_name
        return 1

    def CloseDesktop(self, handle):
        self.close_calls.append(handle)
        return 1

    def GetForegroundWindow(self):
        return 0

    def SendInput(self, count, events, size):
        self.send_flags.append(
            [int(events[index].mi.dwFlags) for index in range(count)]
        )
        if not self._send_results:
            return count
        result = self._send_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def WindowFromPoint(self, point):
        if self.point_error is not None:
            raise self.point_error
        self.point_calls.append((int(point.x), int(point.y)))
        return self.point_hwnd

    def GetAncestor(self, hwnd, flag):
        handle = int(getattr(hwnd, "value", hwnd) or 0)
        self.ancestor_args = (handle, int(flag))
        return self.ancestor_hwnd if self.ancestor_hwnd is not None else handle


def make_win32_api(fake: FakeUser32) -> Win32InputApi:
    api = Win32InputApi()
    api._user32 = fake
    return api


def test_adapter() -> None:
    print("\n[七] 适配器级检查（假 user32，不发送真实输入）")

    if not windows_api._IS_WINDOWS:
        check("非 Windows 跳过适配器级测试", True, "skipped")
        return

    # ---- 安全桌面 ----
    def with_foreground(window, fn):
        original = windows_api.window_mod.get_foreground_window
        windows_api.window_mod.get_foreground_window = lambda: window
        try:
            return fn()
        finally:
            windows_api.window_mod.get_foreground_window = original

    fake = FakeUser32(desktop_name="Default")
    api = make_win32_api(fake)
    blocked, _ = with_foreground(
        win_info(), lambda: api.secure_desktop()
    )
    check("普通 Default 桌面被判定为安全", not blocked)
    check("打开输入桌面时请求只读和切换权限",
          fake.open_args == (
              0,
              False,
              windows_api.DESKTOP_READOBJECTS
              | windows_api.DESKTOP_SWITCHDESKTOP,
          ))
    check("检测结束后关闭桌面句柄", fake.close_calls == [0x1234])

    fake = FakeUser32(desktop_name="Winlogon")
    api = make_win32_api(fake)
    blocked, reason = with_foreground(
        win_info(), lambda: api.secure_desktop()
    )
    check("非 Default 桌面（如 Winlogon）被拒绝",
          blocked and "Winlogon" in reason)
    check("拒绝时也关闭桌面句柄", fake.close_calls == [0x1234])

    fake = FakeUser32(open_ok=False)
    api = make_win32_api(fake)
    blocked, reason = with_foreground(
        win_info(), lambda: api.secure_desktop()
    )
    check("OpenInputDesktop 失败时失败关闭", blocked)
    check("打不开桌面时原因明确", "无法打开输入桌面" in reason)
    check("打不开桌面时没有句柄可关", not fake.close_calls)

    fake = FakeUser32(info_ok=False)
    api = make_win32_api(fake)
    blocked, reason = with_foreground(
        win_info(), lambda: api.secure_desktop()
    )
    check("读不到桌面名称时失败关闭", blocked)
    check("读不到名称时原因明确", "无法读取输入桌面名称" in reason)

    fake = FakeUser32(desktop_name="Default")
    api = make_win32_api(fake)
    blocked, reason = with_foreground(
        win_info(process="consent.exe", title="用户账户控制"),
        lambda: api.secure_desktop(),
    )
    check("Default 桌面上弹出 UAC 前台时仍然拒绝",
          blocked and "系统安全界面" in reason)

    fake = FakeUser32(desktop_name="Default")
    api = make_win32_api(fake)

    def raise_foreground():
        raise RuntimeError("读取前台失败")

    original = windows_api.window_mod.get_foreground_window
    windows_api.window_mod.get_foreground_window = raise_foreground
    try:
        blocked, reason = api.secure_desktop()
    finally:
        windows_api.window_mod.get_foreground_window = original
    check("核对前台异常时失败关闭", blocked and "无法核对前台" in reason)

    # ---- 部分提交后的左键释放 ----
    click_metrics = screen_metrics()

    fake = FakeUser32(send_results=[3])
    api = make_win32_api(fake)
    result = api.send_left_click(
        screen_x=-100, screen_y=300, metrics=click_metrics
    )
    check("三个事件全部提交时报告成功回执",
          result.inserted == 3 and not result.raised)
    check("成功时不补发抬起", len(fake.send_flags) == 1
          and not result.release_attempted)

    fake = FakeUser32(send_results=[0])
    api = make_win32_api(fake)
    result = api.send_left_click(
        screen_x=-100, screen_y=300, metrics=click_metrics
    )
    check("一个都没提交时明确是 0", result.inserted == 0)
    check("0 个事件时不补发抬起", len(fake.send_flags) == 1
          and not result.release_attempted)

    fake = FakeUser32(send_results=[1])
    api = make_win32_api(fake)
    result = api.send_left_click(
        screen_x=-100, screen_y=300, metrics=click_metrics
    )
    check("只提交移动事件时不补发抬起",
          result.inserted == 1 and not result.release_attempted
          and len(fake.send_flags) == 1)
    check("只提交移动时说明左键没有按下",
          "左键没有按下" in result.error)

    fake = FakeUser32(send_results=[2, 1])
    api = make_win32_api(fake)
    result = api.send_left_click(
        screen_x=-100, screen_y=300, metrics=click_metrics
    )
    check("部分提交（按下已提交、抬起没有）时补发一次抬起",
          result.inserted == 2 and result.release_attempted
          and result.release_ok)
    check("补发抬起不是第二次点击：只有 1 个事件", len(fake.send_flags) == 2
          and len(fake.send_flags[1]) == 1)
    release_flags = fake.send_flags[1][0]
    check("补发事件只有 LEFTUP，没有 LEFTDOWN",
          release_flags & windows_api.MOUSEEVENTF_LEFTUP
          and not release_flags & windows_api.MOUSEEVENTF_LEFTDOWN)
    check("部分提交仍然报告为无法确认",
          "无法确认点击是否完整" in result.error)

    fake = FakeUser32(send_results=[2, 0])
    api = make_win32_api(fake)
    result = api.send_left_click(
        screen_x=-100, screen_y=300, metrics=click_metrics
    )
    check("补发抬起失败时明确标记", result.release_attempted
          and not result.release_ok)
    check("补发失败时提示立即人工检查",
          "人工检查" in result.error and "仍处于按下状态" in result.error)
    check("补发失败时不会重试按下", len(fake.send_flags) == 2
          and not any(
              flag & windows_api.MOUSEEVENTF_LEFTDOWN
              for flags in fake.send_flags[1:]
              for flag in flags
          ))

    fake = FakeUser32(send_results=[RuntimeError("SendInput 崩了"), 1])
    api = make_win32_api(fake)
    result = api.send_left_click(
        screen_x=-100, screen_y=300, metrics=click_metrics
    )
    check("SendInput 抛异常时标记 raised", result.raised)
    check("抛异常时也尝试补发一次抬起",
          result.release_attempted and result.release_ok
          and len(fake.send_flags) == 2)

    fake = FakeUser32(send_results=[RuntimeError("崩了"), 0])
    api = make_win32_api(fake)
    result = api.send_left_click(
        screen_x=-100, screen_y=300, metrics=click_metrics
    )
    check("抛异常且补发也失败时提示人工检查",
          result.raised and result.release_attempted
          and not result.release_ok and "人工检查" in result.error)

    # ---- 坐标命中检查 ----
    fake = FakeUser32(point_hwnd=0x55, ancestor_hwnd=0x77)
    api = make_win32_api(fake)
    check("命中检查返回根窗口句柄", api.root_window_at_point(10, 20) == 0x77)
    check("命中检查使用了物理坐标",
          fake.point_calls == [(10, 20)]
          and fake.ancestor_args == (0x55, windows_api.GA_ROOT))

    fake = FakeUser32(point_hwnd=0)
    api = make_win32_api(fake)
    check("坐标处没有窗口时返回 0",
          api.root_window_at_point(10, 20) == 0)

    fake = FakeUser32(point_error=RuntimeError("命中失败"))
    api = make_win32_api(fake)
    check("命中检查异常时返回 None（调用方失败关闭）",
          api.root_window_at_point(10, 20) is None)

    # ---- ctypes 原型：64 位句柄不能被截断 ----
    real = Win32InputApi()
    try:
        user32 = real._u32()
        check("GetForegroundWindow 返回类型声明为句柄",
              user32.GetForegroundWindow.restype is ctypes.c_void_p)
        check("SendInput 参数与返回类型已声明",
              len(user32.SendInput.argtypes or []) == 3
              and user32.SendInput.restype is ctypes.c_uint)
        check("CloseDesktop 参数声明为句柄",
              user32.CloseDesktop.argtypes
              and user32.CloseDesktop.argtypes[0] is ctypes.c_void_p)
    except Exception as exc:  # noqa: BLE001
        check("读取 user32 原型", False, f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------
# 八、静态检查：真实输入入口只有一个
# ----------------------------------------------------------------------
def test_source_surface() -> None:
    print("\n[八] 真实输入入口检查")

    root = Path(__file__).resolve().parents[1]
    forbidden_calls = {
        "SendInput", "mouse_event", "keybd_event", "SetCursorPos",
        "MoveCursor", "keybd_event",
    }
    forbidden_modules = {
        "pyautogui", "pydirectinput", "pynput", "win32api", "win32con",
    }
    violations: list[str] = []
    found_in_adapter: set[str] = set()
    adapter_calls: set[str] = set()
    found_modules: set[str] = set()

    for path in (root / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    calls.add(func.id)
                elif isinstance(func, ast.Attribute):
                    calls.add(func.attr)
            elif isinstance(node, ast.Import):
                found_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found_modules.add(node.module)
        if path.name == "windows_api.py":
            adapter_calls = calls
            found_in_adapter |= calls & forbidden_calls
        elif calls & forbidden_calls:
            violations.append(f"{path.relative_to(root)}: {sorted(calls & forbidden_calls)}")

    check("只有 windows_api.py 调用 SendInput 等底层输入接口",
          not violations and "SendInput" in found_in_adapter,
          str(violations))
    check("没有引入自动化输入库",
          not (found_modules & forbidden_modules),
          str(sorted(found_modules & forbidden_modules)))

    # 只看真实调用，不看文档字符串里的说明。
    check("适配器没有使用 mouse_event / keybd_event / SetCursorPos",
          not (adapter_calls & {"mouse_event", "keybd_event", "SetCursorPos"}),
          str(sorted(adapter_calls & {"mouse_event", "keybd_event", "SetCursorPos"})))

    backend_source = (root / "app/input/windows_backend.py").read_text(
        encoding="utf-8"
    )
    check("后端不直接调用 SendInput", "SendInput(" not in backend_source)

    # 每次工具调用只有一处 click。
    tool_source = (root / "app/tools/click_tools.py").read_text(
        encoding="utf-8"
    )
    check("点击工具只有一次 backend.click 调用",
          tool_source.count("backend.click(") == 1)


# ----------------------------------------------------------------------
def main() -> int:
    print("=" * 72)
    print("Windows 真实鼠标后端测试（全部离线，不发送真实输入）")
    print("=" * 72)

    test_enablement()
    test_emergency_stop()
    test_rate_limit()
    test_security_and_windows()
    test_click_and_results()
    test_adapter()
    asyncio.run(test_tool_integration())
    test_source_surface()

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 72)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for note, _, detail in failed:
        print(f"  [失败] {note}" + (f"　{detail}" if detail else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
