"""截图与预览。

只做三件事：抓屏、缩放、把前台窗口的框画上去。
**不落盘、不发送任何输入、不调用模型。**

为什么截图不落盘：阶段 0 只是让用户确认"看不看得见、坐标对不对"。
落盘会带来一堆额外问题（磁盘上留一堆截图、路径权限、隐私），
而预览只需要在内存里活到接口把它编码返回为止。

坐标约定：这里拿到的一切尺寸都是**物理像素**（前提见 dpi.py）。
如果实际截图尺寸和系统度量对不上，我们会把 consistent 标成 False，
并且**跳过窗口标框**——宁可少画一个框，也不画一个错位的框。
"""

from __future__ import annotations

import base64
import io
import logging
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageGrab

from app.errors import VisionError
from app.vision import dpi
from app.vision import window as window_mod
from app.vision.model import Snapshot

logger = logging.getLogger(__name__)

_FOREGROUND_COLOR = (0, 200, 255)
_LABEL = "FOREGROUND"


# ----------------------------------------------------------------------
# 抓屏
# ----------------------------------------------------------------------
def grab_screen() -> Image.Image:
    """抓整块虚拟桌面（所有显示器）。失败时抛 VisionError，不返回空图。"""
    try:
        image = ImageGrab.grab(all_screens=True)
    except Exception as exc:  # noqa: BLE001 - PIL 内部可能抛各种异常
        raise VisionError(
            "截屏失败", detail=f"{type(exc).__name__}: {exc}"
        ) from exc

    if image is None:
        raise VisionError("截屏失败", detail="系统返回了空截图")

    try:
        width, height = image.size
    except Exception as exc:  # noqa: BLE001
        raise VisionError("截屏失败", detail=f"截图对象没有尺寸：{exc}") from exc

    if width <= 0 or height <= 0:
        raise VisionError("截屏失败", detail=f"截图尺寸异常：{width}x{height}")

    if image.mode != "RGB":
        image = image.convert("RGB")
    return image


# ----------------------------------------------------------------------
# 预览
# ----------------------------------------------------------------------
def make_preview(image: Image.Image, max_side: int) -> Image.Image:
    """按最长边缩到 max_side 以内。比例保持不变，只缩不放。"""
    if max_side <= 0:
        return image.copy()

    width, height = image.size
    longest = max(width, height)
    if longest <= max_side:
        return image.copy()

    ratio = max_side / longest
    size = (max(1, int(round(width * ratio))), max(1, int(round(height * ratio))))
    return image.resize(size, Image.Resampling.LANCZOS)


def annotate_rect(
    image: Image.Image,
    rect: tuple[int, int, int, int],
    *,
    label: str = _LABEL,
) -> Image.Image:
    """在图上画一个框 + 一个标签。返回新图，不改原图。

    标签用英文：Pillow 自带的默认字体没有中文字形，写中文会变成方块。
    """
    out = image.copy()
    draw = ImageDraw.Draw(out)

    left, top, right, bottom = rect
    # 框线粗细跟着图的大小走，小图上别糊成一团
    line_width = max(2, min(6, out.width // 400 + 2))
    draw.rectangle(
        [left, top, max(left, right - 1), max(top, bottom - 1)],
        outline=_FOREGROUND_COLOR,
        width=line_width,
    )

    if not label:
        return out

    try:
        font = ImageFont.load_default()
    except Exception:  # noqa: BLE001 - 连默认字体都没有就别写字了
        return out

    try:
        box = draw.textbbox((0, 0), label, font=font)
        text_width, text_height = box[2] - box[0], box[3] - box[1]
    except Exception:  # noqa: BLE001
        text_width, text_height = len(label) * 6, 11

    # 标签默认贴在框的上方；上面没地方就贴到框里面，并保证不越界
    text_x = max(0, min(left, out.width - text_width - 6))
    text_y = top - text_height - 6
    if text_y < 0:
        text_y = min(max(0, top + 4), max(0, out.height - text_height - 6))

    draw.rectangle(
        [text_x, text_y, text_x + text_width + 4, text_y + text_height + 4],
        fill=_FOREGROUND_COLOR,
    )
    draw.text((text_x + 2, text_y + 2), label, fill=(0, 0, 0), font=font)
    return out


# ----------------------------------------------------------------------
# 一次完整观察
# ----------------------------------------------------------------------
def snapshot(*, annotate: bool = True, max_side: int = 1600) -> Snapshot:
    """截一张图 + 读前台窗口 + 生成预览。

    顺序是有讲究的：**先读度量、再截图、再用真实截图尺寸核对度量**。
    核对不通过就不画框，并把原因写进 annotation_note。
    """
    metrics = dpi.screen_metrics()
    image = grab_screen()
    metrics = dpi.check_capture(metrics, image.size)

    foreground = window_mod.get_foreground_window()
    preview = make_preview(image, max_side)

    annotated = False
    note = ""
    window_in_preview: tuple[int, int, int, int] | None = None

    if foreground is not None:
        if metrics.consistent is False:
            note = "截图尺寸和系统度量对不上，已跳过窗口标框（不硬凑坐标）"
        else:
            try:
                raw_rect = dpi.physical_rect_to_preview(
                    foreground.rect,
                    virtual_left=metrics.virtual_left,
                    virtual_top=metrics.virtual_top,
                    original_size=image.size,
                    preview_size=preview.size,
                )
            except ValueError as exc:
                note = f"预览坐标换算失败：{exc}"
            else:
                clipped = dpi.clip_rect(
                    raw_rect, (0, 0, preview.size[0], preview.size[1])
                )
                if clipped is None:
                    note = "前台窗口不在截图范围内（可能最小化或移到了屏幕外），没有标框"
                else:
                    window_in_preview = clipped
                    if annotate:
                        preview = annotate_rect(preview, clipped)
                        annotated = True

    return Snapshot(
        image=image,
        preview=preview,
        metrics=metrics,
        window=foreground,
        taken_at=datetime.now().isoformat(timespec="seconds"),
        annotated=annotated,
        annotation_note=note,
        original_size=image.size,
        preview_size=preview.size,
        window_in_preview=window_in_preview,
    )


# ----------------------------------------------------------------------
# 编码
# ----------------------------------------------------------------------
def encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def data_url(image: Image.Image) -> str:
    """转成浏览器能直接放进 <img src> 的 data URL。"""
    encoded = base64.b64encode(encode_png(image)).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def encode_jpeg(image: Image.Image, quality: int = 85) -> bytes:
    """编码成 JPEG。给视觉模型用：体积比 PNG 小很多，上传更快。"""
    quality = max(1, min(int(quality), 95))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def image_data_url(image: Image.Image, *, fmt: str = "jpeg", quality: int = 85) -> str:
    """转成 data URL。fmt 只认 jpeg / png，其它一律按 jpeg 处理。"""
    if (fmt or "").strip().lower() == "png":
        encoded = base64.b64encode(encode_png(image)).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    encoded = base64.b64encode(encode_jpeg(image, quality)).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"
