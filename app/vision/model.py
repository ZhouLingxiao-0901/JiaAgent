"""屏幕感知的数据结构。

这里**只放数据**：不调用任何 Windows API，也不 import Pillow。
这样坐标换算、窗口比较这些纯逻辑可以脱离系统单独测试。

坐标空间说明见 dpi.py 开头的注释——四种坐标不是一回事，别混用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 窗口"变没变"的几种情况。用字符串而不是枚举，方便直接写进 JSON。
CHANGE_UNCHANGED = "unchanged"
CHANGE_APPEARED = "appeared"
CHANGE_DISAPPEARED = "disappeared"
CHANGE_SWITCHED = "switched"
CHANGE_MOVED = "moved"
CHANGE_RETITLED = "retitled"


@dataclass(frozen=True)
class ScreenMetrics:
    """屏幕度量。

    除 note/awareness 这几项外，所有尺寸**都是物理像素**——前提是进程
    已经设成 per-monitor v2（见 dpi.ensure_awareness）。

    这个前提不能假定：本机实测"未感知"状态下 GetSystemMetrics 返回
    1707x1067（逻辑），而截图是 2560x1600（物理）。所以 capture 会拿
    真实截图尺寸再核对一遍，对不上就把 consistent 标成 False，绝不硬凑。
    """

    primary_width: int = 0
    primary_height: int = 0
    virtual_left: int = 0
    virtual_top: int = 0
    virtual_width: int = 0
    virtual_height: int = 0
    monitor_count: int = 0
    dpi: int = 96
    scale: float = 1.0
    awareness: str = "unknown"
    awareness_method: str = ""
    # None = 还没截图，无法核对；True/False = 截图尺寸和上面的度量是否一致
    consistent: bool | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": [self.primary_width, self.primary_height],
            "virtual": [
                self.virtual_left,
                self.virtual_top,
                self.virtual_width,
                self.virtual_height,
            ],
            "monitor_count": self.monitor_count,
            "dpi": self.dpi,
            "scale": self.scale,
            "awareness": self.awareness,
            "awareness_method": self.awareness_method,
            "capture_matches_metrics": self.consistent,
            "note": self.note,
        }


@dataclass(frozen=True)
class WindowInfo:
    """一个窗口的全部已知信息。rect 是 (left, top, right, bottom)，物理像素。"""

    hwnd: int
    title: str = ""
    pid: int = 0
    process: str = ""
    exe_path: str = ""
    class_name: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    visible: bool = False
    minimized: bool = False
    maximized: bool = False
    # 矩形是从哪儿来的：dwm_frame（真实的可见边界）或 window_rect（含不可见边框）
    rect_source: str = ""

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    @property
    def is_valid(self) -> bool:
        """矩形是不是一个真正有面积的框。最小化的窗口常常是负数或零面积。"""
        return self.width > 0 and self.height > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "pid": self.pid,
            "process": self.process,
            "exe_path": self.exe_path,
            "class_name": self.class_name,
            "rect": list(self.rect),
            "size": [self.width, self.height],
            "visible": self.visible,
            "minimized": self.minimized,
            "maximized": self.maximized,
            "rect_source": self.rect_source,
        }


@dataclass(frozen=True)
class WindowChange:
    """前后两次观察之间，前台窗口有没有变、怎么变的。

    这是给阶段 2 的"执行前后核对同一个窗口"打地基的纯逻辑：
    真正要动手之前，必须先确认目标窗口还是原来那个、还在原来的位置。
    """

    kind: str = CHANGE_UNCHANGED
    detail: str = ""

    @property
    def changed(self) -> bool:
        return self.kind != CHANGE_UNCHANGED

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "changed": self.changed}


@dataclass
class Snapshot:
    """一次截图的结果。

    image / preview 是 Pillow 对象，**不序列化、不落盘**：
    接口层只把 preview 编码成 base64 放进 JSON，用完就丢。
    """

    image: Any
    preview: Any
    metrics: ScreenMetrics
    window: WindowInfo | None
    taken_at: str
    annotated: bool = False
    annotation_note: str = ""
    original_size: tuple[int, int] = (0, 0)
    preview_size: tuple[int, int] = (0, 0)
    window_in_preview: tuple[int, int, int, int] | None = None

    @property
    def preview_scale(self) -> float:
        """预览图相对原图的缩放倍数（<1 表示缩小了）。"""
        if not self.original_size or self.original_size[0] <= 0:
            return 0.0
        return round(self.preview_size[0] / self.original_size[0], 4)
