"""窗口信息：前台窗口是谁、矩形在哪、属于哪个进程。

全部用 ctypes 调 Windows 自带 API——和 app/tools/win_process.py 同一路数，
不为了拿几个窗口信息去装 pywin32。

几个约定：
  - 矩形一律是**物理像素**（前提：进程已设 per-monitor v2，见 dpi.py）
  - 优先用 DWM 的"真实可见边界"（不含看不见的缩放边框），拿不到再退回 GetWindowRect
  - 任何一步失败都不抛异常：拿不到就返回 None 或空串，由调用方如实展示
"""

from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path

from app.vision import dpi
from app.vision.model import (
    CHANGE_APPEARED,
    CHANGE_DISAPPEARED,
    CHANGE_MOVED,
    CHANGE_RETITLED,
    CHANGE_SWITCHED,
    CHANGE_UNCHANGED,
    WindowChange,
    WindowInfo,
)

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_TITLE = 512

if _IS_WINDOWS:
    from ctypes import wintypes

    _BOOL = wintypes.BOOL
    _HWND = wintypes.HWND
    _LPARAM = wintypes.LPARAM

    def _set_prototypes() -> None:
        """声明句柄相关函数的参数/返回类型。

        不声明时 ctypes 默认按 `c_int` 处理返回值，64 位下的 HWND 和进程
        句柄会被截断。前台窗口核对、进程身份核对和 CloseHandle 都依赖
        这些句柄，截断会让安全检查比较或关闭错误的对象。
        """
        user32 = ctypes.windll.user32
        function_types = {
            "GetForegroundWindow": ([], wintypes.HWND),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "IsZoomed": ([wintypes.HWND], wintypes.BOOL),
            "GetWindowRect": (
                [wintypes.HWND, ctypes.POINTER(_RECT)],
                wintypes.BOOL,
            ),
            "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
            "GetWindowTextW": (
                [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int],
                ctypes.c_int,
            ),
            "GetClassNameW": (
                [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int],
                ctypes.c_int,
            ),
            "GetWindowThreadProcessId": (
                [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                wintypes.DWORD,
            ),
        }
        for name, (argtypes, restype) in function_types.items():
            function = getattr(user32, name, None)
            if function is not None:
                function.argtypes = argtypes
                function.restype = restype

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
else:  # 让非 Windows 上也能 import，只是所有函数返回空
    _BOOL = ctypes.c_int
    _HWND = ctypes.c_void_p
    _LPARAM = ctypes.c_void_p

    def _set_prototypes() -> None:
        return

_ENUM_PROC = ctypes.WINFUNCTYPE(_BOOL, _HWND, _LPARAM)


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


_set_prototypes()


# ----------------------------------------------------------------------
# 内部：从 hwnd 一点点读信息
# ----------------------------------------------------------------------
def _hwnd_ptr(hwnd: int) -> ctypes.c_void_p:
    """HWND 在 64 位系统上是 64 位的，必须包成 c_void_p，否则会被截断。"""
    return ctypes.c_void_p(int(hwnd))


def _dwm_int(hwnd: int, attribute: int) -> int | None:
    value = ctypes.c_ulong()
    try:
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            _hwnd_ptr(hwnd),
            ctypes.c_ulong(attribute),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return int(value.value) if result == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _rect_of(hwnd: int) -> tuple[tuple[int, int, int, int] | None, str]:
    """拿窗口矩形。返回（矩形, 来源）。矩形无效就返回 (None, "")。"""
    rect = _RECT()

    # 一、DWM 的真实可见边界（推荐）
    try:
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            _hwnd_ptr(hwnd),
            ctypes.c_ulong(DWMWA_EXTENDED_FRAME_BOUNDS),
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if result == 0:
            value = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
            if value[2] > value[0] and value[3] > value[1]:
                return value, "dwm_frame"
    except Exception:  # noqa: BLE001
        pass

    # 二、退回 GetWindowRect（会算上不可见的缩放边框，但至少有个值）
    try:
        if ctypes.windll.user32.GetWindowRect(_hwnd_ptr(hwnd), ctypes.byref(rect)):
            value = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
            if value[2] > value[0] and value[3] > value[1]:
                return value, "window_rect"
    except Exception:  # noqa: BLE001
        pass

    return None, ""


def _title_of(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    try:
        length = int(user32.GetWindowTextLengthW(_hwnd_ptr(hwnd)))
    except Exception:  # noqa: BLE001
        return ""
    size = max(2, min(length + 1, _MAX_TITLE))
    buffer = ctypes.create_unicode_buffer(size)
    try:
        if not user32.GetWindowTextW(_hwnd_ptr(hwnd), buffer, size):
            return ""
    except Exception:  # noqa: BLE001
        return ""
    return buffer.value


def _class_of(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    try:
        if ctypes.windll.user32.GetClassNameW(_hwnd_ptr(hwnd), buffer, 256):
            return buffer.value
    except Exception:  # noqa: BLE001
        pass
    return ""


def _pid_of(hwnd: int) -> int:
    pid = ctypes.c_ulong(0)
    try:
        ctypes.windll.user32.GetWindowThreadProcessId(
            _hwnd_ptr(hwnd), ctypes.byref(pid)
        )
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return 0


def _process_info(pid: int) -> tuple[str, str]:
    """返回（进程名, 完整路径）。拿不到就退回进程快照，再拿不到就空串。"""
    if pid <= 0 or not _IS_WINDOWS:
        return "", ""

    exe_path = ""
    handle = None
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if handle:
            buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                exe_path = buffer.value
    except Exception:  # noqa: BLE001
        exe_path = ""
    finally:
        try:
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass

    name = Path(exe_path).name if exe_path else ""

    if not name:
        # 兜底：借用现有的进程快照，不重复实现同一件事。
        # 提权进程常常 OpenProcess 失败，但进程列表里还是看得到名字。
        try:
            from app.tools.win_process import list_process_names

            for candidate, pids in list_process_names().items():
                if int(pid) in pids:
                    name = candidate
                    break
        except Exception:  # noqa: BLE001
            pass

    return name, exe_path


# ----------------------------------------------------------------------
# 对外接口
# ----------------------------------------------------------------------
def get_window_info(hwnd: int) -> WindowInfo | None:
    """读一个窗口的全部信息。hwnd 无效就返回 None，绝不抛异常。"""
    if not _IS_WINDOWS or not hwnd:
        return None

    try:
        if not ctypes.windll.user32.IsWindow(_hwnd_ptr(hwnd)):
            return None
    except Exception:  # noqa: BLE001
        return None

    rect, rect_source = _rect_of(hwnd)
    if rect is None:
        # 矩形都拿不到，这个窗口没法用——最小化到托盘、或者正在销毁。
        return None

    pid = _pid_of(hwnd)
    process, exe_path = _process_info(pid)

    user32 = ctypes.windll.user32
    try:
        visible = bool(user32.IsWindowVisible(_hwnd_ptr(hwnd)))
    except Exception:  # noqa: BLE001
        visible = False
    try:
        minimized = bool(user32.IsIconic(_hwnd_ptr(hwnd)))
    except Exception:  # noqa: BLE001
        minimized = False
    try:
        maximized = bool(user32.IsZoomed(_hwnd_ptr(hwnd)))
    except Exception:  # noqa: BLE001
        maximized = False

    return WindowInfo(
        hwnd=int(hwnd),
        title=_title_of(hwnd),
        pid=pid,
        process=process,
        exe_path=exe_path,
        class_name=_class_of(hwnd),
        rect=rect,
        visible=visible,
        minimized=minimized,
        maximized=maximized,
        rect_source=rect_source,
    )


def get_foreground_window() -> WindowInfo | None:
    """当前前台窗口。锁屏、切换桌面等情况下可能返回 None——那是正常情况。"""
    if not _IS_WINDOWS:
        return None
    try:
        hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        return None
    if not hwnd:
        return None
    return get_window_info(hwnd)


def list_windows(
    limit: int = 200,
    *,
    include_untitled: bool = False,
    include_offscreen: bool = False,
) -> list[WindowInfo]:
    """枚举可见窗口。

    过滤掉：不可见的、被系统 cloaked 的（UWP 应用会挂一堆这种隐藏窗口）、
    以及（默认）没有标题的。任何单个窗口出错都只跳过它，不中断枚举。
    """
    if not _IS_WINDOWS or limit <= 0:
        return []

    results: list[WindowInfo] = []
    user32 = ctypes.windll.user32

    # 只算一次屏幕边界。最小化到托盘的窗口常常躺在一个假坐标上
    # （本机实测 FlClash 是 -31994,-32000），那不代表它在屏幕上。
    bounds = None
    if not include_offscreen:
        try:
            metrics = dpi.screen_metrics()
            bounds = (
                metrics.virtual_left,
                metrics.virtual_top,
                metrics.virtual_left + metrics.virtual_width,
                metrics.virtual_top + metrics.virtual_height,
            )
        except Exception:  # noqa: BLE001 - 拿不到就不过滤
            bounds = None

    def _callback(hwnd, _lparam):  # noqa: ANN001, ANN202
        try:
            if len(results) >= limit:
                return False        # 返回 False 让 EnumWindows 停下来
            if not user32.IsWindowVisible(hwnd):
                return True
            if _dwm_int(int(hwnd), DWMWA_CLOAKED):
                return True
            info = get_window_info(int(hwnd))
            if info is None:
                return True
            if not include_untitled and not info.title.strip():
                return True
            if bounds is not None and not dpi.rect_intersects(info.rect, bounds):
                return True
            results.append(info)
        except Exception:  # noqa: BLE001 - 一个窗口出错不能毁掉整次枚举
            logger.debug("枚举窗口时跳过了一个", exc_info=True)
        return True

    try:
        user32.EnumWindows(_ENUM_PROC(_callback), 0)
    except Exception:  # noqa: BLE001
        logger.exception("枚举窗口失败")

    results.sort(key=lambda item: (item.process.casefold(), item.title.casefold()))
    return results[:limit]


def compare_windows(
    before: WindowInfo | None, after: WindowInfo | None
) -> WindowChange:
    """比较前后两次观察到的前台窗口。

    纯逻辑，不碰系统。判断顺序是有讲究的：
    先看"还是不是同一个窗口"，再看"位置变没变"，最后才看标题——
    因为窗口一换，位置和标题的差异都没有意义了。
    """
    if before is None and after is None:
        return WindowChange(CHANGE_UNCHANGED, "前后都没有前台窗口")
    if before is None:
        return WindowChange(
            CHANGE_APPEARED,
            f"出现了前台窗口：{after.title or after.process or after.hwnd}",
        )
    if after is None:
        return WindowChange(
            CHANGE_DISAPPEARED,
            f"前台窗口没了：{before.title or before.process or before.hwnd}",
        )
    if before.hwnd != after.hwnd:
        return WindowChange(
            CHANGE_SWITCHED,
            f"前台窗口换了：{before.title or before.hwnd!r} -> "
            f"{after.title or after.hwnd!r}",
        )
    if before.rect != after.rect:
        return WindowChange(
            CHANGE_MOVED, f"还是同一个窗口，但矩形变了：{before.rect} -> {after.rect}"
        )
    if before.title != after.title:
        return WindowChange(
            CHANGE_RETITLED,
            f"还是同一个窗口，但标题变了：{before.title!r} -> {after.title!r}",
        )
    return WindowChange(CHANGE_UNCHANGED, "前台窗口没变")
