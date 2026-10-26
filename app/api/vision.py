"""屏幕感知的 HTTP 接口（阶段 0：只读）。

三个接口：

  GET /api/vision/context    屏幕度量 + 前台窗口（**不截图**）
  GET /api/vision/windows    当前可见窗口列表
  GET /api/vision/snapshot   截一张图，返回预览 + 前台窗口 + 坐标信息

# 安全边界（阶段 0 的核心约束）

这里**没有、也不会有**任何"让模型操作电脑"的入口：
  - 截图只因为你本人在网页上点了按钮才发生；
  - 这个模块没有注册任何工具到 app/tools/registry.py，
    所以模型连"提出截图请求"的途径都没有；
  - 不发送鼠标、键盘、快捷键，一个都没有。

截图也**不落盘**：只在内存里生成，编码成 data URL 返回给浏览器后就丢。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.errors import VisionError
from app.settings import get_tools_config
from app.vision import capture, dpi
from app.vision import window as window_mod

router = APIRouter()


def _config():
    return get_tools_config().vision


def _require_enabled() -> None:
    """功能被关掉时，明确拒绝，而不是悄悄返回空数据。"""
    if not _config().enabled:
        raise VisionError(
            "屏幕感知功能没有打开",
            detail="把 config/tools.yaml 里 vision.enabled 改成 true，然后重启服务。",
        )


# ----------------------------------------------------------------------
# 只读：屏幕度量 + 前台窗口
# ----------------------------------------------------------------------
@router.get("/vision/context")
def vision_context() -> dict:
    """不截图，只报当前屏幕和前台窗口的状态。

    用来快速确认 DPI 感知有没有设对：如果 virtual 是 1707x1067
    而截图是 2560x1600，就说明感知没生效。
    """
    _require_enabled()
    metrics = dpi.screen_metrics()
    foreground = window_mod.get_foreground_window()
    return {
        "enabled": True,
        "metrics": metrics.to_dict(),
        "foreground": foreground.to_dict() if foreground else None,
        "coordinate_spaces": dpi.describe_spaces(metrics),
        "persisted": False,
    }


@router.get("/vision/windows")
def vision_windows(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    """当前可见窗口列表。屏幕外的（最小化到托盘的假坐标）默认不列。"""
    _require_enabled()
    limit = min(limit, max(1, _config().max_windows))
    items = window_mod.list_windows(limit=limit)
    return {
        "count": len(items),
        "limit": limit,
        "windows": [item.to_dict() for item in items],
    }


# ----------------------------------------------------------------------
# 截图预览
# ----------------------------------------------------------------------
@router.get("/vision/snapshot")
def vision_snapshot(
    annotate: bool = Query(default=True, description="是否在前台窗口上画框"),
    max_side: int | None = Query(
        default=None, ge=320, le=4096, description="预览图最长边，不传就用配置值"
    ),
) -> dict:
    """截一张图，返回预览和坐标信息。

    返回里的 original_size / preview_size / scale 三个字段是分开给的，
    前端别把预览图的像素当成屏幕物理像素用。
    """
    _require_enabled()
    side = max_side if max_side is not None else _config().preview_max_side
    side = max(320, min(int(side), 4096))

    snap = capture.snapshot(annotate=annotate, max_side=side)

    return {
        "taken_at": snap.taken_at,
        "metrics": snap.metrics.to_dict(),
        "foreground": snap.window.to_dict() if snap.window else None,
        "image": capture.data_url(snap.preview),
        "preview": {
            "size": list(snap.preview_size),
            "original_size": list(snap.original_size),
            "scale": snap.preview_scale,
            "annotated": snap.annotated,
            "note": snap.annotation_note,
        },
        "foreground_in_preview": (
            list(snap.window_in_preview) if snap.window_in_preview else None
        ),
        "coordinate_spaces": dpi.describe_spaces(snap.metrics),
        # 明确写出来，方便你和测试直接核对"确实没有落盘"
        "persisted": False,
    }
