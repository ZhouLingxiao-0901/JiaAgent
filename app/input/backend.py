"""输入后端的公共接口（不实现任何真实输入）。

为什么要有这一层：
  点击不是一个"调一下 API"就完了的动作。它至少分成：把目标窗口带到前台、
  确认前台确实是那个窗口、把服务端算好的物理坐标交给系统、如实报告结果。
  把这些放进接口里，点击工具只依赖"能力"，不依赖某一种具体实现；
  测试用假后端就能跑完整条链路，不会碰真实鼠标。

**这一层不做安全判断**。授权、观察记录校验、坐标映射都在工具层和
`app.vision.observation` 里完成。后端只负责执行和报告，不能替调用方
决定"这个坐标可以点"。

目前有 `FakeInputBackend`（见 fake.py，只记录不执行）和
`WindowsInputBackend`（见 windows_backend.py，真实 `SendInput`）。真实后端：
  - 使用物理像素坐标（不要在这里做逻辑坐标换算）；
  - 聚焦后用 `GetForegroundWindow` 核对是不是同一个窗口；
  - 能在安全桌面/UAC 提示下拒绝执行；
  - 发送前核对坐标处最顶层的窗口确实是目标窗口；
  - 失败和结果未知必须分开报告，未知绝不能被当成"没点上"。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.vision.model import WindowInfo

# 点击结果。unknown 是关键：超时、句柄中途消失、系统没给回执，
# 都不能算失败——失败意味着"可以再试一次"，未知则绝对不能自动重试。
CLICK_SUCCEEDED = "succeeded"
CLICK_FAILED = "failed"
CLICK_UNKNOWN = "unknown"

# 常见原因码。后端和工具都用字符串，方便落库和测试断言。
CODE_BACKEND_UNAVAILABLE = "backend_unavailable"
CODE_SECURE_DESKTOP = "secure_desktop"
CODE_WINDOW_GONE = "window_gone"
CODE_WINDOW_CHANGED = "window_changed"
CODE_WINDOW_MOVED = "window_moved"
CODE_WINDOW_HIDDEN = "window_hidden"
CODE_WINDOW_MINIMIZED = "window_minimized"
CODE_FOCUS_FAILED = "focus_failed"
CODE_FOREGROUND_MISMATCH = "foreground_mismatch"
CODE_POINT_NOT_TARGET = "point_not_target"
CODE_UNSAFE_TARGET = "unsafe_target"
CODE_CLICK_FAILED = "click_failed"
CODE_CLICK_UNKNOWN = "click_unknown"
CODE_EMERGENCY_STOP = "emergency_stop"
CODE_RATE_LIMITED = "rate_limited"

# UAC / 安全桌面相关进程。这些进程的窗口不可操作：要么是提权确认框，
# 要么是锁屏。命中就拒绝，绝不尝试聚焦或点击。
SECURE_PROCESS_NAMES = frozenset({
    "consent.exe",       # UAC 确认框
    "logonui.exe",       # 锁屏 / 登录界面
    "winlogon.exe",      # 安全桌面宿主
})

# 这些窗口类属于系统安全界面，即使进程名没认出来也不碰。
SECURE_CLASS_NAMES = frozenset({
    "credential dialog xaml host",
    "logonui",
    "sas window class",
})

# 标题只在同时命中系统对话框类时才调用，避免把浏览器里
# 一篇叫 "User Account Control" 的文章当成 UAC。
_SECURE_TITLE_MARKERS = (
    "用户账户控制",
    "user account control",
    "安全桌面",
    "secure desktop",
)
_DIALOG_CLASS_NAMES = frozenset({"#32770", "credential dialog xaml host"})


@dataclass(frozen=True)
class BackendStatus:
    """输入后端现在能不能用。

    available=False 时调用方必须失败关闭，**不能**退回任何真实输入方式。
    """

    available: bool
    reason: str = ""
    simulated: bool = False


@dataclass(frozen=True)
class FocusResult:
    """把目标窗口带到前台的结果。"""

    ok: bool
    code: str = ""
    reason: str = ""
    foreground: WindowInfo | None = None


@dataclass(frozen=True)
class ClickOutcome:
    """一次点击的真实结果。

    status 只能是 succeeded / failed / unknown。unknown 表示"不知道点没点上"，
    调用方必须停止并询问用户，不能自动重试。
    """

    status: str
    code: str = ""
    reason: str = ""
    hwnd: int = 0
    screen_x: int | None = None
    screen_y: int | None = None
    simulated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status == CLICK_SUCCEEDED

    @property
    def unknown(self) -> bool:
        return self.status == CLICK_UNKNOWN


def _normalized_process(name: str) -> str:
    text = str(name or "").strip().casefold().replace("/", "\\")
    return text.rsplit("\\", 1)[-1]


def unsafe_target_reason(window: WindowInfo | None) -> str:
    """目标是不是 UAC / 安全桌面这类不可操作的安全界面。

    返回空串表示没命中已知规则。注意：这只拦得住"目标本身属于安全界面"，
    拦不住"执行时安全桌面突然弹出来"——后者必须由后端在点击前用
    系统 API 再查一次（见 `InputBackend.secure_desktop`）。
    """
    if window is None:
        return "没有目标窗口，不能操作。"

    process = _normalized_process(window.process)
    if process in SECURE_PROCESS_NAMES:
        return (
            f"目标窗口属于系统安全界面（{window.process or process}），"
            "UAC 或锁屏不能通过这种方式操作。"
        )

    class_name = str(window.class_name or "").strip().casefold()
    if class_name in SECURE_CLASS_NAMES:
        return (
            f"目标窗口的窗口类 {window.class_name!r} 属于系统安全界面，"
            "拒绝操作。"
        )

    title = str(window.title or "").strip().casefold()
    if class_name in _DIALOG_CLASS_NAMES and any(
        marker in title for marker in _SECURE_TITLE_MARKERS
    ):
        return (
            f"目标窗口看起来是系统安全确认框（{window.title or '无标题'}），"
            "拒绝操作。"
        )
    return ""


def identity_problem(expected: WindowInfo, actual: WindowInfo | None) -> str:
    """expected 是不是仍然是 actual 那个窗口。

    只比较身份，不比较矩形——位置/尺寸变化由观察记录校验单独报告，
    这样调用方能给出更准确的原因（移动还是缩放）。
    """
    if actual is None:
        return "目标窗口已关闭或句柄失效。"
    if int(actual.hwnd) != int(expected.hwnd):
        return "目标窗口句柄已经变化。"
    if expected.pid > 0 and int(actual.pid or 0) != int(expected.pid):
        return "目标窗口已经属于另一个进程。"
    if (
        expected.process
        and _normalized_process(actual.process)
        != _normalized_process(expected.process)
    ):
        return "目标窗口的进程名已经变化。"
    if (actual.title or "") != (expected.title or ""):
        return "目标窗口的标题已经变化。"
    if (
        expected.class_name
        and actual.class_name
        and actual.class_name != expected.class_name
    ):
        return "目标窗口的窗口类已经变化。"
    return ""


class InputBackend(ABC):
    """一次点击所需的全部能力。

    方法都是同步的：真实的 Windows 输入调用本身就是同步的，放进线程池
    反而会拉长"最后一次核对"和"真正点击"之间的间隔。调用方（工具）在
    async 里直接调用即可，这些调用很短。
    """

    name: str = "input"
    simulated: bool = False

    @abstractmethod
    def status(self) -> BackendStatus:
        """后端是否可用。不可用时调用方必须失败关闭。"""

    def secure_desktop(self) -> tuple[bool, str]:
        """当前是不是在 UAC / 锁屏等安全桌面。

        返回 (True, 原因) 时禁止执行。默认实现**故意返回"是"**：
        真实后端如果忘了实现这项检测，点击会被失败关闭，而不是悄悄
        在安全桌面上乱点。`FakeInputBackend` 会覆盖它，由测试显式控制。
        """
        return (
            True,
            "输入后端没有实现安全桌面检测，不能确认当前桌面安全。",
        )

    @abstractmethod
    def focus_window(
        self,
        *,
        window: WindowInfo,
        window_rect: tuple[int, int, int, int],
    ) -> FocusResult:
        """把目标窗口带到前台，并确认前台确实是它。"""

    @abstractmethod
    def foreground_matches(self, window: WindowInfo) -> tuple[bool, str]:
        """前台窗口还是不是目标窗口。点击前最后一道竞态检查。"""

    @abstractmethod
    def click(
        self,
        *,
        window: WindowInfo,
        screen_x: int,
        screen_y: int,
    ) -> ClickOutcome:
        """在已经校验过的物理坐标上执行一次点击，并如实报告结果。"""
