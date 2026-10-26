"""Windows 输入 API 的适配层。

**所有真实的系统输入调用都只在这里。** 其余代码（包括测试）只依赖这个
适配器的接口，可以注入假实现，从而在完全不碰真实鼠标的情况下验证：
坐标怎么换算、事件怎么构造、返回值怎么解释。

实现的是**单次鼠标左键单击**：一次 `SendInput` 提交三个事件
（移动到绝对坐标、左键按下、左键抬起）。不使用 `mouse_event`、
`keybd_event`、`SetCursorPos`，也不发送任何键盘事件。

坐标使用虚拟桌面的物理像素：左上角可能是负数（副屏在主屏左边或上边），
绝对坐标通过 `MOUSEEVENTF_VIRTUALDESK` 映射到整个虚拟桌面，而不是
假设所有显示器都从 (0, 0) 开始。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass

from app.input.backend import unsafe_target_reason
from app.vision import dpi
from app.vision import window as window_mod
from app.vision.model import ScreenMetrics, WindowInfo

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# SendInput / 鼠标事件常量
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

SW_SHOW = 5
SW_RESTORE = 9

DESKTOP_READOBJECTS = 0x0001
DESKTOP_SWITCHDESKTOP = 0x0100
UOI_NAME = 2
GA_ROOT = 2

_EXPECTED_EVENTS = 3

# 部分提交时可能已经把左键按下去了。收尾只发一个"抬起"，不是重试点击。
_STUCK_BUTTON_WARNING = (
    "鼠标左键可能仍处于按下状态，请立即人工检查（移动鼠标或再按一次左键）。"
)


@dataclass(frozen=True)
class SendInputResult:
    """`SendInput` 的原始回执。

    inserted == expected  → 所有事件都提交了，可以报告 succeeded。
    inserted == 0         → 系统一个都没接收，可以确认"没有执行"。
    0 < inserted < expected 或 raised=True → 结果未知，绝不能当成失败重试。

    `SendInput` 按顺序插入事件：inserted >= 2 意味着"左键按下"已经提交，
    但"左键抬起"可能没有。适配层会尝试补发一次**抬起**（`release_*`），
    这只是清理按键状态，不是重试点击，也绝不会再发一次按下。
    """

    inserted: int
    expected: int
    error: str = ""
    raised: bool = False
    release_attempted: bool = False
    release_ok: bool = False
    release_error: str = ""


# ----------------------------------------------------------------------
# ctypes 结构（只在真正调用 Windows API 时用到）
# ----------------------------------------------------------------------
if _IS_WINDOWS:
    from ctypes import wintypes

    _ULONG_PTR = (
        ctypes.c_ulonglong
        if ctypes.sizeof(ctypes.c_void_p) == 8
        else ctypes.c_ulong
    )

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [
            ("type", wintypes.DWORD),
            ("u", _INPUTUNION),
        ]

    def _configure_user32(user32) -> None:
        """声明参数/返回类型。

        ctypes 默认把返回值当成 `c_int`，64 位下的 HWND/HDESK 句柄会被
        截断，导致核对前台、关闭桌面或命中测试拿到错误的句柄。这里逐个
        声明，避免"安全相关检查默默比较了错误的值"。
        """
        user32.OpenInputDesktop.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        user32.OpenInputDesktop.restype = wintypes.HANDLE

        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.CloseDesktop.restype = wintypes.BOOL

        user32.GetUserObjectInformationW.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetUserObjectInformationW.restype = wintypes.BOOL

        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND

        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL

        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL

        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL

        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL

        user32.SendInput.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        ]
        user32.SendInput.restype = wintypes.UINT

        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND

        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
else:  # pragma: no cover - 非 Windows 只用于导入，不会真的发送输入
    _INPUT = None  # type: ignore[assignment]

    def _configure_user32(user32) -> None:  # noqa: ARG001
        return


def _error_text(code: int) -> str:
    if code <= 0:
        return ""
    try:
        return ctypes.FormatError(code).strip()
    except Exception:  # noqa: BLE001
        return f"Windows 错误码 {code}"


class Win32InputApi:
    """真实的 Windows 输入适配器。"""

    name = "win32-sendinput"

    def __init__(self) -> None:
        self._user32 = None

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def _u32(self):
        if self._user32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            _configure_user32(user32)
            self._user32 = user32
        return self._user32

    def available(self) -> tuple[bool, str]:
        """当前环境能不能安全地执行真实输入。拿不准就返回 False。"""
        if not _IS_WINDOWS:
            return False, "真实鼠标输入只支持 Windows，当前系统不支持。"

        try:
            awareness = dpi.ensure_awareness()
        except Exception as exc:  # noqa: BLE001
            return False, f"无法确认 DPI 感知：{type(exc).__name__}: {exc}"
        if awareness.get("error"):
            return False, f"DPI 感知不可靠：{awareness['error']}"
        if awareness.get("awareness") != "per_monitor":
            return (
                False,
                "进程不是 per-monitor DPI 感知，物理坐标可能错位，"
                "拒绝启用真实输入。",
            )

        try:
            metrics = dpi.screen_metrics()
        except Exception as exc:  # noqa: BLE001
            return False, f"无法读取屏幕度量：{type(exc).__name__}: {exc}"
        if metrics.virtual_width <= 0 or metrics.virtual_height <= 0:
            return False, "无法读取有效的虚拟桌面尺寸，拒绝启用真实输入。"
        return True, ""

    def get_window_info(self, hwnd: int) -> WindowInfo | None:
        return window_mod.get_window_info(int(hwnd))

    def get_foreground_window(self) -> WindowInfo | None:
        return window_mod.get_foreground_window()

    def screen_metrics(self) -> ScreenMetrics | None:
        try:
            metrics = dpi.screen_metrics()
        except Exception:  # noqa: BLE001
            logger.warning("读取屏幕度量失败", exc_info=True)
            return None
        if metrics.virtual_width <= 0 or metrics.virtual_height <= 0:
            return None
        return metrics

    def root_window_at_point(self, x: int, y: int) -> int | None:
        """坐标处最顶层的根窗口句柄。

        用 `WindowFromPoint` 找到落点上的窗口，再 `GetAncestor(GA_ROOT)`
        找到它所属的顶层窗口。这样点击目标窗口里某个子控件时，返回的
        仍然是目标窗口本身。返回 None 表示无法确认（调用方必须失败关闭），
        返回 0 表示该坐标处没有任何窗口。
        """
        if not _IS_WINDOWS:
            return None
        try:
            user32 = self._u32()
            point = wintypes.POINT(int(x), int(y))
            found = user32.WindowFromPoint(point)
            if not found:
                return 0
            root = user32.GetAncestor(wintypes.HWND(found), GA_ROOT)
            return int(root or found)
        except Exception:  # noqa: BLE001 - 拿不准就返回 None，由调用方拒绝
            logger.warning("读取坐标处窗口失败", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # 安全桌面
    # ------------------------------------------------------------------
    def secure_desktop(self) -> tuple[bool, str]:
        """是不是 UAC / 锁屏等安全桌面。任何不确定都按"是"处理。"""
        if not _IS_WINDOWS:
            return True, "当前不是 Windows，无法确认桌面安全。"

        user32 = self._u32()
        handle = None
        try:
            handle = user32.OpenInputDesktop(
                0, False, DESKTOP_READOBJECTS | DESKTOP_SWITCHDESKTOP
            )
            if not handle:
                code = ctypes.get_last_error()
                return (
                    True,
                    "无法打开输入桌面，可能正处于 UAC、锁屏或权限受限状态"
                    + (f"（{_error_text(code)}）" if _error_text(code) else "")
                    + "。拒绝执行真实输入。",
                )

            buffer = ctypes.create_unicode_buffer(256)
            needed = ctypes.c_ulong(0)
            ok = user32.GetUserObjectInformationW(
                handle,
                UOI_NAME,
                buffer,
                ctypes.sizeof(buffer),
                ctypes.byref(needed),
            )
            if not ok:
                return True, "无法读取输入桌面名称，不能确认桌面安全。"
            name = (buffer.value or "").strip()
            if name.casefold() != "default":
                return (
                    True,
                    f"当前输入桌面是 {name!r}，不是普通桌面（可能是 UAC/锁屏）。",
                )
        except Exception as exc:  # noqa: BLE001
            return (
                True,
                f"安全桌面检测失败：{type(exc).__name__}: {exc}。"
                "不能确认安全，拒绝执行。",
            )
        finally:
            if handle:
                try:
                    user32.CloseDesktop(handle)
                except Exception:  # noqa: BLE001
                    pass

        # 桌面名是 Default 时再核对前台，拦住仍在默认桌面上弹出的安全界面。
        try:
            foreground = window_mod.get_foreground_window()
            unsafe = unsafe_target_reason(foreground)
            if unsafe:
                return True, f"前台是系统安全界面：{unsafe}"
        except Exception:  # noqa: BLE001
            return True, "无法核对前台窗口，不能确认桌面安全。"

        return False, ""

    # ------------------------------------------------------------------
    # 聚焦
    # ------------------------------------------------------------------
    def focus_window(self, hwnd: int) -> tuple[bool, str]:
        """把窗口带到前台，并核对前台确实是它。失败就明确失败。"""
        if not _IS_WINDOWS:
            return False, "当前不是 Windows，不能聚焦窗口。"
        try:
            user32 = self._u32()
            ptr = ctypes.c_void_p(int(hwnd))
            if not user32.IsWindow(ptr):
                return False, "目标窗口已经不存在。"
            if user32.IsIconic(ptr):
                user32.ShowWindow(ptr, SW_RESTORE)
            else:
                user32.ShowWindow(ptr, SW_SHOW)
            user32.SetForegroundWindow(ptr)
            foreground = int(user32.GetForegroundWindow() or 0)
            if foreground != int(hwnd):
                return (
                    False,
                    f"聚焦后前台窗口是 {foreground:#x}，不是目标 "
                    f"{int(hwnd):#x}。系统可能拒绝了我们切换前台。",
                )
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"聚焦失败：{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # 单击
    # ------------------------------------------------------------------
    def send_left_click(
        self,
        *,
        screen_x: int,
        screen_y: int,
        metrics: ScreenMetrics,
    ) -> SendInputResult:
        """发送一次左键单击（移动 + 按下 + 抬起）。

        这是全项目唯一真正调用 `SendInput` 的地方。调用方必须先完成所有
        授权、观察记录、窗口和前台核对；这里只负责把坐标变成事件。
        """
        if not _IS_WINDOWS or _INPUT is None:
            return SendInputResult(
                0, _EXPECTED_EVENTS, "当前系统不支持 SendInput。", raised=False
            )

        try:
            absolute_x, absolute_y = dpi.physical_to_input_absolute(
                screen_x, screen_y, metrics
            )
        except Exception as exc:  # noqa: BLE001
            return SendInputResult(
                0,
                _EXPECTED_EVENTS,
                f"坐标换算失败：{type(exc).__name__}: {exc}",
                raised=False,
            )

        flags = (
            MOUSEEVENTF_MOVE
            | MOUSEEVENTF_ABSOLUTE
            | MOUSEEVENTF_VIRTUALDESK
        )
        try:
            events = (_INPUT * _EXPECTED_EVENTS)()
            events[0].type = INPUT_MOUSE
            events[0].mi.dx = absolute_x
            events[0].mi.dy = absolute_y
            events[0].mi.dwFlags = flags

            events[1].type = INPUT_MOUSE
            events[1].mi.dx = absolute_x
            events[1].mi.dy = absolute_y
            events[1].mi.dwFlags = flags | MOUSEEVENTF_LEFTDOWN

            events[2].type = INPUT_MOUSE
            events[2].mi.dx = absolute_x
            events[2].mi.dy = absolute_y
            events[2].mi.dwFlags = flags | MOUSEEVENTF_LEFTUP

            user32 = self._u32()
            ctypes.set_last_error(0)
            inserted = int(
                user32.SendInput(
                    _EXPECTED_EVENTS, events, ctypes.sizeof(_INPUT)
                )
            )
            if inserted >= _EXPECTED_EVENTS:
                return SendInputResult(inserted, _EXPECTED_EVENTS)

            code = ctypes.get_last_error()
            detail = _error_text(code)
            if inserted == 0:
                return SendInputResult(
                    0,
                    _EXPECTED_EVENTS,
                    "SendInput 没有提交任何事件（可能被系统拒绝或权限不足）"
                    + (f"：{detail}" if detail else ""),
                    raised=False,
                )

            # SendInput 按顺序插入事件：
            #   inserted == 1 只提交了"移动"，左键没有按下；
            #   inserted >= 2 则"按下"已经提交，但"抬起"可能没提交。
            prefix = (
                "只提交了移动事件，左键没有按下。"
                if inserted == 1
                else "左键按下可能已经提交，但抬起没有。"
            )
            release_attempted = False
            release_ok = False
            release_error = ""
            if inserted >= 2:
                release_attempted, release_ok, release_error = (
                    self._release_left_button(absolute_x, absolute_y)
                )
            if not release_attempted:
                tail = ""
            elif release_ok:
                tail = " 已补发一次左键抬起以释放可能仍按下的按键（不是重试点击）。"
            else:
                tail = f" {_STUCK_BUTTON_WARNING}"
                if release_error:
                    tail += f"（{release_error}）"
            return SendInputResult(
                inserted,
                _EXPECTED_EVENTS,
                f"SendInput 只提交了 {inserted}/{_EXPECTED_EVENTS} 个事件"
                + (f"（{detail}）" if detail else "")
                + f"，无法确认点击是否完整。{prefix}{tail}",
                raised=False,
                release_attempted=release_attempted,
                release_ok=release_ok,
                release_error=release_error,
            )
        except Exception as exc:  # noqa: BLE001
            # 异常发生时可能已经提交了一部分事件，不能当成"确定没执行"。
            # 状态未知时也补一次抬起：按键若本来就未按下，抬起是无害的。
            release_attempted, release_ok, release_error = (
                self._release_left_button(absolute_x, absolute_y)
            )
            if not release_attempted:
                tail = ""
            elif release_ok:
                tail = " 已补发一次左键抬起（不是重试点击）。"
            else:
                tail = f" {_STUCK_BUTTON_WARNING}"
                if release_error:
                    tail += f"（{release_error}）"
            return SendInputResult(
                0,
                _EXPECTED_EVENTS,
                f"SendInput 抛出异常：{type(exc).__name__}: {exc}。{tail}",
                raised=True,
                release_attempted=release_attempted,
                release_ok=release_ok,
                release_error=release_error,
            )

    def _release_left_button(
        self, absolute_x: int, absolute_y: int
    ) -> tuple[bool, bool, str]:
        """补发一次左键抬起，返回 (是否尝试, 是否确认提交, 错误说明)。

        这不是重试点击：只发一个 `LEFTUP` 事件，没有新的按下，也没有
        第二次点击。目的是避免部分提交后左键长期停留在按下状态。
        """
        if not _IS_WINDOWS or _INPUT is None:
            return False, False, "当前系统不支持 SendInput，无法释放左键。"
        try:
            events = (_INPUT * 1)()
            events[0].type = INPUT_MOUSE
            events[0].mi.dx = int(absolute_x)
            events[0].mi.dy = int(absolute_y)
            events[0].mi.dwFlags = (
                MOUSEEVENTF_MOVE
                | MOUSEEVENTF_ABSOLUTE
                | MOUSEEVENTF_VIRTUALDESK
                | MOUSEEVENTF_LEFTUP
            )
            user32 = self._u32()
            ctypes.set_last_error(0)
            inserted = int(
                user32.SendInput(1, events, ctypes.sizeof(_INPUT))
            )
            if inserted == 1:
                return True, True, ""
            detail = _error_text(ctypes.get_last_error())
            return (
                True,
                False,
                f"左键抬起只提交了 {inserted}/1 个事件"
                + (f"（{detail}）" if detail else ""),
            )
        except Exception as exc:  # noqa: BLE001
            return (
                True,
                False,
                f"补发左键抬起时异常：{type(exc).__name__}: {exc}",
            )
