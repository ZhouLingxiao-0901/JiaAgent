"""DPI 感知与坐标空间。

# 为什么这个文件存在

Windows 上同时存在四种"坐标"，**它们不是一回事**：

  1. 屏幕物理像素（physical）
     显示器真实有多少像素。本机是 2560x1600。
     设成 per-monitor v2 之后，GetSystemMetrics、GetWindowRect、截图
     用的都是这个空间——这是我们内部**唯一**使用的坐标。

  2. 逻辑像素（logical / virtualized）
     DPI 不感知的进程看到的是 1707x1067（2560 / 1.5）。
     本机实测：不设感知时，窗口坐标是逻辑的，而 ImageGrab 截出来的
     却是 2560x1600 的物理图——两者差 1.5 倍。
     **这就是为什么必须在启动时就把感知设死。**

  3. 归一化坐标（normalized 0.0~1.0）
     以后给视觉模型用：让它说"按钮在图片宽度的 43% 处"，
     比让它猜绝对像素可靠得多。阶段 0 只实现换算，不接模型。

  4. 输入绝对坐标（input absolute 0~65535）
     SendInput 的 MOUSEEVENTF_ABSOLUTE 用的就是这个。
     **阶段 0 只做换算，不发送任何输入。**

# 一条规矩

所有换算都写成纯函数，参数里显式带 scale / 尺寸，
不读全局状态。这样测试可以随便构造"150% 缩放、双屏、负坐标"的场景。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import replace

from app.vision.model import ScreenMetrics

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# GetSystemMetrics 的索引
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CMONITORS = 80

LOGPIXELSX = 88

_DPI_AWARENESS_NAMES = {0: "unaware", 1: "system", 2: "per_monitor"}

# ensure_awareness 只真正执行一次，结果缓存在这里
_AWARENESS_CACHE: dict[str, str] | None = None


# ----------------------------------------------------------------------
# 一、DPI 感知
# ----------------------------------------------------------------------
def _read_awareness_level() -> int:
    """读当前进程的 DPI 感知级别：0=未感知，1=系统级，2=每显示器级。"""
    if not _IS_WINDOWS:
        return -1
    try:
        value = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(value))
        return int(value.value)
    except Exception:  # noqa: BLE001 - 老系统可能没有 shcore
        return -1


def ensure_awareness() -> dict[str, str]:
    """把本进程设成 per-monitor v2。幂等，可以反复调用。

    为什么非设不可：不设的话窗口坐标是逻辑像素、截图是物理像素，
    两边差一个 scale。这种错位不会报错，只会让所有坐标悄悄偏掉。

    返回 {"awareness": ..., "method": ..., "error": ...}。
    即使设置失败也不抛异常——上层应该照常跑，只是把 error 如实告诉用户。
    """
    global _AWARENESS_CACHE
    if _AWARENESS_CACHE is not None:
        return dict(_AWARENESS_CACHE)

    if not _IS_WINDOWS:
        _AWARENESS_CACHE = {
            "awareness": "unsupported",
            "method": "none",
            "error": "当前系统不是 Windows，屏幕感知不可用",
        }
        return dict(_AWARENESS_CACHE)

    result = {"awareness": "", "method": "", "error": ""}

    level = _read_awareness_level()
    if level >= 2:
        result.update(awareness="per_monitor", method="already-set")
        _AWARENESS_CACHE = result
        return dict(result)

    user32 = ctypes.windll.user32

    # 首选：Windows 10 1703+ 的 per-monitor v2
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            result["awareness"] = "per_monitor"
            result["method"] = "SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)"
        else:
            result["error"] = "SetProcessDpiAwarenessContext 返回 0"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"SetProcessDpiAwarenessContext 不可用：{exc}"

    # 退一步：Windows 8.1+ 的 shcore
    if not result["awareness"]:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            result["awareness"] = "per_monitor"
            result["method"] = "SetProcessDpiAwareness(PER_MONITOR_AWARE)"
            result["error"] = ""
        except Exception as exc:  # noqa: BLE001
            result["error"] += f"；SetProcessDpiAwareness 也失败：{exc}"

    # 再退一步：Vista+ 的系统级感知（能保证截图和窗口坐标一致，多屏不一定准）
    if not result["awareness"]:
        try:
            if user32.SetProcessDPIAware():
                result["awareness"] = "system"
                result["method"] = "SetProcessDPIAware()"
        except Exception as exc:  # noqa: BLE001
            result["error"] += f"；SetProcessDPIAware 也失败：{exc}"

    actual = _read_awareness_level()
    if actual >= 0:
        result["awareness"] = _DPI_AWARENESS_NAMES.get(
            actual, result["awareness"] or "unknown"
        )
        if actual < 2:
            note = f"最终感知级别是 {result['awareness']}，不是 per_monitor，坐标可能错位"
            result["error"] = f"{result['error']}；{note}" if result["error"] else note

    _AWARENESS_CACHE = result
    logger.info(
        "DPI 感知：%s（%s）%s",
        result["awareness"], result["method"], result["error"],
    )
    return dict(result)


def current_dpi() -> int:
    """当前系统 DPI（96 = 100%，144 = 150%）。读不到就按 96 算。"""
    if not _IS_WINDOWS:
        return 96
    try:
        return int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:  # noqa: BLE001 - Win10 1607 以前没有
        pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        try:
            value = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, LOGPIXELSX))
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)
        return value or 96
    except Exception:  # noqa: BLE001
        return 96


def screen_metrics() -> ScreenMetrics:
    """读当前的屏幕度量。**不截图**，所以 consistent 是 None。"""
    info = ensure_awareness()
    if not _IS_WINDOWS:
        return ScreenMetrics(
            awareness=info["awareness"],
            awareness_method=info["method"],
            note=info["error"],
        )

    user32 = ctypes.windll.user32
    dpi = current_dpi()
    return ScreenMetrics(
        primary_width=int(user32.GetSystemMetrics(SM_CXSCREEN)),
        primary_height=int(user32.GetSystemMetrics(SM_CYSCREEN)),
        virtual_left=int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        virtual_top=int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        virtual_width=int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
        virtual_height=int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
        monitor_count=int(user32.GetSystemMetrics(SM_CMONITORS)),
        dpi=dpi,
        scale=scale_for_dpi(dpi),
        awareness=info["awareness"],
        awareness_method=info["method"],
        consistent=None,
        note=info["error"],
    )


# ----------------------------------------------------------------------
# 二、纯换算（不碰系统状态，最好测）
# ----------------------------------------------------------------------
def scale_for_dpi(dpi: int) -> float:
    """DPI -> 缩放倍数。96->1.0，144->1.5，192->2.0。"""
    if dpi <= 0:
        return 1.0
    return round(dpi / 96.0, 4)


def round_half_up(value: float) -> int:
    """四舍五入（不是 Python round 的"银行家舍入"）。

    坐标可以是负数（多显示器时虚拟桌面左上角可能是负的），
    所以两个方向都按"远离零"处理，保证前后一致。
    """
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def clamp(value, low, high):
    if value < low:
        return low
    if value > high:
        return high
    return value


def logical_to_physical(x: float, y: float, scale: float) -> tuple[int, int]:
    """逻辑像素 -> 物理像素。未感知的进程看到的是逻辑坐标，截图是物理的。"""
    if scale <= 0:
        raise ValueError("缩放比例必须大于 0")
    return (round_half_up(x * scale), round_half_up(y * scale))


def physical_to_logical(x: float, y: float, scale: float) -> tuple[int, int]:
    """物理像素 -> 逻辑像素。只用来诊断"差了多少倍"，不参与业务判断。"""
    if scale <= 0:
        raise ValueError("缩放比例必须大于 0")
    return (round_half_up(x / scale), round_half_up(y / scale))


def normalized_to_physical(
    nx: float,
    ny: float,
    width: int,
    height: int,
    *,
    origin_x: int = 0,
    origin_y: int = 0,
) -> tuple[int, int]:
    """归一化坐标（0~1）-> 物理像素。

    用 width-1 / height-1 做映射：1.0 落在最后一个像素上，不会越界。
    视觉模型以后返回的就是这种坐标。
    """
    if width <= 0 or height <= 0:
        raise ValueError("宽高必须大于 0")
    nx = clamp(float(nx), 0.0, 1.0)
    ny = clamp(float(ny), 0.0, 1.0)
    return (
        origin_x + round_half_up(nx * (width - 1)),
        origin_y + round_half_up(ny * (height - 1)),
    )


def physical_to_normalized(
    x: float,
    y: float,
    width: int,
    height: int,
    *,
    origin_x: int = 0,
    origin_y: int = 0,
) -> tuple[float, float]:
    """物理像素 -> 归一化坐标（0~1）。"""
    if width <= 1 or height <= 1:
        return (0.0, 0.0)
    nx = clamp((x - origin_x) / (width - 1), 0.0, 1.0)
    ny = clamp((y - origin_y) / (height - 1), 0.0, 1.0)
    return (round(nx, 6), round(ny, 6))


def physical_to_input_absolute(
    x: float, y: float, metrics: ScreenMetrics
) -> tuple[int, int]:
    """物理像素 -> SendInput 的 0~65535 绝对坐标。

    **阶段 0 只提供换算函数，不发送任何输入。**
    放在这里是因为它属于坐标体系，和鼠标动作本身无关。
    """
    width = max(1, metrics.virtual_width)
    height = max(1, metrics.virtual_height)
    ax = round_half_up((x - metrics.virtual_left) * 65535 / max(1, width - 1))
    ay = round_half_up((y - metrics.virtual_top) * 65535 / max(1, height - 1))
    return (int(clamp(ax, 0, 65535)), int(clamp(ay, 0, 65535)))


def input_absolute_to_physical(
    ax: float, ay: float, metrics: ScreenMetrics
) -> tuple[int, int]:
    """0~65535 绝对坐标 -> 物理像素。用来验证换算往返是否一致。"""
    width = max(1, metrics.virtual_width)
    height = max(1, metrics.virtual_height)
    return (
        metrics.virtual_left + round_half_up(ax * max(1, width - 1) / 65535),
        metrics.virtual_top + round_half_up(ay * max(1, height - 1) / 65535),
    )


def clip_rect(
    rect: tuple[int, int, int, int], bounds: tuple[int, int, int, int]
) -> tuple[int, int, int, int] | None:
    """把一个矩形裁到 bounds 里面。完全不相交就返回 None。"""
    left, top, right, bottom = rect
    bleft, btop, bright, bbottom = bounds
    new_left = max(left, bleft)
    new_top = max(top, btop)
    new_right = min(right, bright)
    new_bottom = min(bottom, bbottom)
    if new_right <= new_left or new_bottom <= new_top:
        return None
    return (new_left, new_top, new_right, new_bottom)


def rect_intersects(
    rect: tuple[int, int, int, int], bounds: tuple[int, int, int, int]
) -> bool:
    """两个矩形有没有交集。用来判断一个窗口是不是真的在屏幕范围内。

    注意这里不要求完全在屏幕内：一个半截露在屏幕外的窗口仍然是真的。
    只要完全不相交（比如躺在 -32000 那种假坐标上），才算"不在屏幕上"。
    """
    left, top, right, bottom = rect
    bleft, btop, bright, bbottom = bounds
    return right > bleft and left < bright and bottom > btop and top < bbottom


def physical_rect_to_preview(
    rect: tuple[int, int, int, int],
    *,
    virtual_left: int,
    virtual_top: int,
    original_size: tuple[int, int],
    preview_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """物理像素矩形 -> 预览图上的矩形。

    预览图是"整块虚拟桌面"缩放后的结果，所以要先减掉虚拟桌面原点，
    再按预览/原图的比例缩放。**两个轴分开算**，别假设缩放在两轴一致。
    """
    original_width, original_height = original_size
    preview_width, preview_height = preview_size
    if original_width <= 0 or original_height <= 0:
        raise ValueError("原图尺寸必须大于 0")
    scale_x = preview_width / original_width
    scale_y = preview_height / original_height
    left, top, right, bottom = rect
    return (
        round_half_up((left - virtual_left) * scale_x),
        round_half_up((top - virtual_top) * scale_y),
        round_half_up((right - virtual_left) * scale_x),
        round_half_up((bottom - virtual_top) * scale_y),
    )


def check_capture(metrics: ScreenMetrics, size: tuple[int, int]) -> ScreenMetrics:
    """拿真实截图尺寸核对系统度量。对不上就如实标出来，绝不硬凑。

    这是防"静默错位"的最后一道闸：如果 DPI 感知没设成功，
    这里会立刻发现，而不是等到截图和窗口框对不齐才让人一头雾水。
    """
    try:
        width, height = int(size[0]), int(size[1])
    except Exception:  # noqa: BLE001
        return replace(metrics, consistent=False, note="拿不到截图尺寸，无法核对")

    expected = (metrics.virtual_width, metrics.virtual_height)
    if expected[0] <= 0 or expected[1] <= 0:
        return replace(
            metrics, consistent=None, note="系统没给出屏幕尺寸，无法核对截图"
        )
    if (width, height) == expected:
        return replace(metrics, consistent=True, note="")

    ratio = round(width / expected[0], 4) if expected[0] else 0.0
    return replace(
        metrics,
        consistent=False,
        note=(
            f"截图是 {width}x{height}，系统度量是 {expected[0]}x{expected[1]}"
            f"（比值 {ratio}）。这通常意味着 DPI 感知没设成 per-monitor v2，"
            "按窗口矩形去裁剪或标注会整体错位。"
        ),
    )


def describe_spaces(metrics: ScreenMetrics | None = None) -> dict[str, str]:
    """四种坐标空间的人话说明。接口会把它带给前端，免得以后又搞混。"""
    scale = metrics.scale if metrics else 1.0
    return {
        "physical": "屏幕物理像素。截图、窗口矩形、GetSystemMetrics 都用这个空间。",
        "logical": (
            f"逻辑像素，DPI 未感知时看到的。本机缩放 {scale}，"
            f"逻辑尺寸 = 物理 / {scale}。本项目不拿它做判断，只用来发现错位。"
        ),
        "normalized": "0.0~1.0 的相对坐标。将来给视觉模型用，比猜绝对像素可靠。",
        "input_absolute": "SendInput 的 0~65535 绝对坐标。阶段 0 只换算，不发送。",
    }
