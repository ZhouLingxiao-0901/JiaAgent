"""按确定的 HWND 捕获单个应用窗口（Windows Graphics Capture）。

和 capture.py 的区别：
  capture.py 抓的是**整个虚拟桌面**，只用于用户明确要求的 desktop 范围。
  这里抓的是**一个窗口自己的合成画面**：即使它被其他窗口完全遮挡，
  WGC 返回的仍然是它自己的内容，不会混入桌面上的其他窗口。

安全约定（一条都不能少）：
  - 只接受已经确定的 HWND，不做「猜一个窗口」。
  - 捕获前检查句柄、可见性、最小化、尺寸和「窗口是否声明禁止捕获」。
  - 捕获后检查画面：全黑、可疑单色、颜色过少、尺寸异常都算失败。
  - 失败一律抛 VisionError，明确说明原因。
  - **绝不退回 PrintWindow，也绝不退回整桌截图。**
  - 图像只在内存里，不落盘、不写日志、不写数据库。

资源释放：
  capturekit 的 ScreenCapture 用 with 管理捕获会话和后台线程；
  离开 with 时一定会调用 close()，即使 grab() 抛异常。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from PIL import Image

from app.errors import VisionError
from app.vision import capture
from app.vision import dpi
from app.vision import window as window_mod
from app.vision.model import Snapshot, WindowInfo

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_MAX_BLACK_RATIO = 0.98
DEFAULT_MAX_UNIFORM_RATIO = 0.995
DEFAULT_MIN_DISTINCT_COLORS = 3
DEFAULT_SAMPLE_SIDE = 128
MIN_IMAGE_SIDE = 8
BLACK_LEVEL = 4          # 三个通道都不超过这个值，算一个「黑像素」


@dataclass(frozen=True)
class ContentStats:
    """对捕获画面的基本统计。只用于判断「这张图能不能当成功结果」。"""

    width: int
    height: int
    black_ratio: float
    uniform_ratio: float
    distinct_colors: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "black_ratio": round(self.black_ratio, 4),
            "uniform_ratio": round(self.uniform_ratio, 4),
            "distinct_colors": self.distinct_colors,
        }


@dataclass
class WindowFrame:
    """一次成功的窗口捕获结果。图像只在内存里。"""

    image: Image.Image
    preview: Image.Image
    snapshot: Snapshot
    backend: str
    content_size: tuple[int, int]
    stats: ContentStats


def _load_capturekit():
    """惰性导入 capturekit，导入失败时给出人话解释。"""
    try:
        import capturekit
    except Exception as exc:  # noqa: BLE001 - 缺依赖、DLL 问题等
        raise VisionError(
            "窗口捕获组件不可用",
            detail=f"capturekit 没有安装或加载失败：{type(exc).__name__}: {exc}",
        ) from exc
    return capturekit


def is_available() -> tuple[bool, str]:
    """窗口捕获组件能不能用。

    先确认能导入，再问一次当前系统是否支持窗口捕获。这里**不打开捕获
    会话、不截图**；能力探测失败也按不可用处理（fail closed），免得让用户
    先确认一个根本做不到的操作。
    """
    try:
        ck = _load_capturekit()
    except VisionError as exc:
        detail = f"（{exc.detail}）" if exc.detail else ""
        return False, f"{exc.message}{detail}"

    probe = getattr(ck, "get_capabilities", None)
    if callable(probe):
        try:
            capabilities = probe()
        except Exception as exc:  # noqa: BLE001 - 探测失败也不要放行
            return False, f"无法确认窗口捕获是否可用：{type(exc).__name__}: {exc}"
        capability = getattr(capabilities, "window_capture", None)
        if capability is not None and not capability:
            reason = str(getattr(capability, "reason", "") or "系统不支持")
            return False, f"这台电脑不支持窗口捕获：{reason}"

    return True, ""


def _reason_for(exc: BaseException) -> str:
    """把 capturekit 的异常翻成用户能看懂的一句话。

    只按异常类型判断，不展示画面内容。原始异常类型和消息会放在 detail 里，
    方便排查，但不含像素数据。
    """
    name = type(exc).__name__
    if name in {"TargetNotFoundError", "TargetLostError", "WindowClosedError"}:
        return "目标窗口在捕获前或捕获中关闭、失效了"
    if name == "CaptureTimeoutError":
        return "窗口没有在限定时间内返回画面"
    if name == "AccessDeniedError":
        return "系统拒绝捕获这个窗口"
    if name in {
        "CapabilityUnavailableError",
        "UnsupportedPlatformError",
        "UnsupportedDisplayServerError",
    }:
        return "这台电脑或这个窗口不支持窗口捕获"
    return "窗口捕获失败"


def _guard_info(info: WindowInfo | None) -> None:
    """窗口状态的基本检查。不通过就明确报错。"""
    if info is None or not info.hwnd:
        raise VisionError("目标窗口已失效", detail="窗口句柄不存在，或窗口已经关闭")
    if not info.visible:
        raise VisionError("目标窗口当前不可见", detail=f"HWND {info.hwnd:#x}")
    if info.minimized:
        raise VisionError(
            "目标窗口已最小化",
            detail=f"HWND {info.hwnd:#x}，请先恢复窗口再试",
        )
    if not info.is_valid:
        raise VisionError(
            "目标窗口尺寸无效", detail=f"HWND {info.hwnd:#x} 的矩形是 {info.rect}"
        )


def _frame_to_image(frame) -> Image.Image:
    """把 capturekit 的 BGRA 帧转成 PIL RGB 图。

    capturekit 的 Frame.data 是 (h, w, 4) 的 uint8 BGRA 数组。
    这里不信任形状，先检查再转换；畸形数据直接报错。
    """
    data = getattr(frame, "data", None)
    if data is None:
        raise VisionError("窗口捕获没有返回图像", detail="捕获结果里没有画面数据")

    try:
        import numpy as np

        array = np.asarray(data)
    except Exception as exc:  # noqa: BLE001
        raise VisionError(
            "窗口捕获结果无法解析", detail=f"{type(exc).__name__}: {exc}"
        ) from exc

    if array.size == 0 or array.ndim != 3 or array.shape[2] not in (3, 4):
        raise VisionError(
            "窗口捕获返回的画面格式不对",
            detail=f"shape={getattr(array, 'shape', None)}",
        )

    height, width = int(array.shape[0]), int(array.shape[1])
    if width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE:
        raise VisionError("窗口捕获返回的画面太小", detail=f"{width}x{height}")

    # BGRA / BGR -> RGB
    rgb = array[:, :, [2, 1, 0]]
    if rgb.dtype != np.uint8:
        rgb = rgb.astype(np.uint8)
    return Image.fromarray(rgb, "RGB").copy()


def measure_content(image: Image.Image, *, sample_side: int = DEFAULT_SAMPLE_SIDE) -> ContentStats:
    """统计画面：黑色比例、最常见颜色占比、颜色种类数。

    采样到最多 sample_side × sample_side，避免大窗口逐像素扫描太慢。
    统计结果只用于判断有效性，不写日志、不落盘。
    """
    width, height = image.size
    sample = image.convert("RGB")
    if max(sample.size) > max(1, int(sample_side)):
        sample = sample.resize(
            (max(1, int(sample_side)), max(1, int(sample_side))),
            Image.Resampling.NEAREST,
        )

    total = sample.width * sample.height
    if total <= 0:
        raise VisionError("窗口捕获结果无法检查", detail="采样图没有像素")

    colors = sample.getcolors(maxcolors=total + 1)
    if not colors:
        raise VisionError("窗口捕获结果无法检查", detail="像素统计失败")

    black = sum(
        count for count, (r, g, b) in colors if max(int(r), int(g), int(b)) <= BLACK_LEVEL
    )
    top = max(count for count, _ in colors)
    return ContentStats(
        width=width,
        height=height,
        black_ratio=black / total,
        uniform_ratio=top / total,
        distinct_colors=len(colors),
    )


def _check_content(
    stats: ContentStats,
    *,
    max_black_ratio: float,
    max_uniform_ratio: float,
    min_distinct_colors: int,
) -> None:
    """全黑 / 可疑单色 / 颜色过少都不算成功。"""
    if stats.black_ratio > max_black_ratio:
        raise VisionError(
            "捕获到的窗口画面几乎是全黑",
            detail=f"黑色像素占 {stats.black_ratio:.2%}，判定为无效画面",
        )
    if stats.uniform_ratio > max_uniform_ratio:
        raise VisionError(
            "捕获到的窗口画面是可疑的单色",
            detail=f"最常见颜色占 {stats.uniform_ratio:.2%}，判定为无效画面",
        )
    if stats.distinct_colors < min_distinct_colors:
        raise VisionError(
            "捕获到的窗口画面颜色过少",
            detail=f"只有 {stats.distinct_colors} 种颜色，判定为无效画面",
        )


def capture_window(
    hwnd: int,
    *,
    max_side: int = 1280,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_black_ratio: float = DEFAULT_MAX_BLACK_RATIO,
    max_uniform_ratio: float = DEFAULT_MAX_UNIFORM_RATIO,
    min_distinct_colors: int = DEFAULT_MIN_DISTINCT_COLORS,
    sample_side: int = DEFAULT_SAMPLE_SIDE,
) -> WindowFrame:
    """按 HWND 捕获一帧并做有效性检查。失败抛 VisionError，绝不降级。"""
    hwnd = int(hwnd or 0)

    before = window_mod.get_window_info(hwnd)
    _guard_info(before)

    ck = _load_capturekit()

    try:
        ck_window = ck.get_window(hwnd)
    except Exception as exc:  # noqa: BLE001
        raise VisionError(
            _reason_for(exc),
            detail=f"HWND {hwnd:#x}：{type(exc).__name__}: {exc}",
        ) from exc

    if getattr(ck_window, "capture_excluded", False):
        raise VisionError(
            "目标窗口不允许被捕获",
            detail="这个窗口声明了从屏幕捕获中排除，佳不会绕过它",
        )
    if getattr(ck_window, "is_minimized", False):
        raise VisionError("目标窗口已最小化", detail=f"HWND {hwnd:#x}")
    if not getattr(ck_window, "is_visible", True):
        raise VisionError("目标窗口当前不可见", detail=f"HWND {hwnd:#x}")

    ck_rect = getattr(ck_window, "rect", None)
    if ck_rect is not None:
        ck_width = int(getattr(ck_rect, "width", 0) or 0)
        ck_height = int(getattr(ck_rect, "height", 0) or 0)
        if ck_width <= 0 or ck_height <= 0:
            raise VisionError(
                "目标窗口尺寸无效", detail=f"HWND {hwnd:#x} 的窗口矩形是 {ck_rect}"
            )

    backend = ""
    try:
        # with 保证无论 grab() 成功还是抛异常，捕获会话和后台线程都会被释放。
        with ck.ScreenCapture(
            ck.WindowSource(ck_window, client_area_only=False, border=False)
        ) as cap:
            frame = cap.grab(timeout=timeout)
            backend = getattr(cap, "backend_name", "") or ""
    except VisionError:
        raise
    except Exception as exc:  # noqa: BLE001 - capturekit 的异常都要如实转出去
        raise VisionError(
            _reason_for(exc),
            detail=f"HWND {hwnd:#x}：{type(exc).__name__}: {exc}",
        ) from exc

    if frame is None:
        raise VisionError("窗口捕获没有返回画面", detail=f"HWND {hwnd:#x}")

    image = _frame_to_image(frame)
    stats = measure_content(image, sample_side=sample_side)
    _check_content(
        stats,
        max_black_ratio=max_black_ratio,
        max_uniform_ratio=max_uniform_ratio,
        min_distinct_colors=min_distinct_colors,
    )

    # 捕获期间窗口可能被关掉，或者句柄被系统复用给另一个进程。
    # 这两种情况下的画面都不能信。
    after = window_mod.get_window_info(hwnd)
    _guard_info(after)
    if before.pid and after.pid and int(before.pid) != int(after.pid):
        raise VisionError(
            "目标窗口在捕获期间发生了变化",
            detail="窗口变成了另一个进程的，本次画面不可信",
        )
    if (
        before.process
        and after.process
        and before.process.casefold() != after.process.casefold()
    ):
        raise VisionError(
            "目标窗口在捕获期间发生了变化",
            detail="窗口进程名变了，本次画面不可信",
        )

    preview = capture.make_preview(image, max_side)

    raw_content = getattr(frame, "content_size", None)
    try:
        content_size = (
            (int(raw_content[0]), int(raw_content[1]))
            if raw_content
            else image.size
        )
    except (TypeError, ValueError, IndexError):
        content_size = image.size
    if content_size[0] <= 0 or content_size[1] <= 0:
        content_size = image.size

    snap = Snapshot(
        image=image,
        preview=preview,
        metrics=dpi.screen_metrics(),
        window=after,
        taken_at=datetime.now().isoformat(timespec="seconds"),
        annotated=False,
        annotation_note="按窗口句柄用 Windows Graphics Capture 捕获，只包含该窗口自身内容",
        original_size=image.size,
        preview_size=preview.size,
    )

    return WindowFrame(
        image=image,
        preview=preview,
        snapshot=snap,
        backend=backend,
        content_size=content_size,
        stats=stats,
    )
