"""可信观察登记与坐标校验（只读，不发送任何输入事件）。

# 这个模块解决什么问题

视觉模型看到一张截图后，会给出「登录按钮在 x=0.43, y=0.61」这样的
归一化坐标。可是从"模型说完"到"佳真的去点"之间，可能已经过去了十几秒：
窗口被拖动、程序换了页面、显示器缩放变了、甚至窗口已经关掉。此时如果
直接拿旧坐标去点，点到的可能是完全不同的东西。

所以这里做两件事：

  1. **登记**：每次真实截图 + 视觉分析成功后，由服务端建立一条观察记录，
     把画面尺寸、窗口矩形、DPI 度量、元素框和有效期绑定在一起。
  2. **校验**：将来真正要操作之前，拿 `observation_id + element_id`
     来问这里「这个坐标现在还能信吗」。只要有一项对不上，就明确拒绝，
     **绝不猜一个新坐标，也绝不悄悄沿用旧截图**。

# 信任边界

- `observation_id` 和元素编号（e1、e2……）都是**服务端生成**的。
  模型既不能创建记录，也不能指定编号。它只能引用自己看到过的编号；
  拿别的编号来查，会得到「不存在」。
- 模型自己给的坐标不直接作为操作依据。调用方必须提交元素编号；
  如果同时提交 x/y，它们必须落在该元素的可信边界框内，否则拒绝。
- 记录只保存几何信息和一张截图的指纹（SHA-256），**不保存图像本身**。
  截图仍然只在内存里活到分析结束。
- 本模块没有任何 Windows 输入 API 调用。它只读窗口信息、屏幕度量，
  返回「能不能点、点哪里」的结论；真正发送输入由 `app/input` 的后端负责，
  本阶段那里只有假后端，没有真实鼠标键盘实现。

# 为什么坐标要经过两级映射

视觉模型看的是 `snapshot.preview`（可能被缩小过），它给的是相对这张图的
0~1 坐标。截图本身对应窗口的物理像素矩形。记录同时保存截图尺寸和窗口矩形，
校验时按比例映射到屏幕物理像素，不假设"预览图 = 屏幕"。
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from app.vision import dpi
from app.vision import window as window_mod
from app.vision.analyze import ScreenReading
from app.vision.model import ScreenMetrics, Snapshot, WindowInfo

logger = logging.getLogger(__name__)

# 记录状态
STATUS_VALID = "valid"
STATUS_EXPIRED = "expired"
STATUS_INVALIDATED = "invalidated"

DEFAULT_TTL_SECONDS = 30.0
DEFAULT_MAX_ENTRIES = 64

# 模型提交的坐标允许落在元素边界框外这么多（归一化比例），
# 用来吸收四舍五入误差。它只是容差，不是"猜坐标"。
DEFAULT_COORD_TOLERANCE = 0.01

# 校验失败时返回给调用方的机器可读原因码。
CODE_INVALID_ARGUMENTS = "invalid_arguments"
CODE_NOT_FOUND = "observation_not_found"
CODE_INVALIDATED = "observation_invalidated"
CODE_EXPIRED = "observation_expired"
CODE_SCOPE = "scope_not_operable"
CODE_MAPPING = "mapping_not_trustworthy"
CODE_ELEMENT_NOT_FOUND = "element_not_found"
CODE_COORDINATES = "invalid_coordinates"
CODE_COORDINATES_OUT_OF_BOUNDS = "coordinates_out_of_bounds"
CODE_COORDINATES_MISMATCH = "coordinates_mismatch"
CODE_WINDOW_GONE = "window_gone"
CODE_WINDOW_CHANGED = "window_changed"
CODE_WINDOW_MOVED = "window_moved"
CODE_WINDOW_RESIZED = "window_resized"
CODE_WINDOW_HIDDEN = "window_not_visible"
CODE_WINDOW_MINIMIZED = "window_minimized"
CODE_ENVIRONMENT_CHANGED = "environment_changed"
CODE_POINT_OUT_OF_BOUNDS = "point_out_of_bounds"
CODE_INTERNAL = "internal_error"


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ObservedElement:
    """观察记录里一个可信元素。

    x / y 是中心点，0~1，相对视觉模型看到的那张截图。
    width / height 是边界框比例，模型没给就是 None。
    """

    element_id: str
    name: str
    kind: str
    x: float
    y: float
    width: float | None = None
    height: float | None = None
    confidence: float = 0.0

    @property
    def bbox(self) -> tuple[float, float, float, float] | None:
        """(left, top, right, bottom)，没有完整宽高时返回 None。"""
        if self.width is None or self.height is None:
            return None
        if self.width <= 0 or self.height <= 0:
            return None
        return (
            self.x - self.width / 2.0,
            self.y - self.height / 2.0,
            self.x + self.width / 2.0,
            self.y + self.height / 2.0,
        )

    def contains(self, nx: float, ny: float, tolerance: float) -> bool:
        """一个归一化点是否落在这个元素的可信范围内。"""
        tol = max(0.0, float(tolerance))
        box = self.bbox
        if box is None:
            # 模型没给边框：只认元素中心附近一个很小的半径。
            return abs(nx - self.x) <= tol and abs(ny - self.y) <= tol
        left, top, right, bottom = box
        return (
            left - tol <= nx <= right + tol
            and top - tol <= ny <= bottom + tol
        )

    def to_dict(self) -> dict[str, Any]:
        box = self.bbox
        return {
            "element_id": self.element_id,
            "name": self.name,
            "kind": self.kind,
            "x": round(self.x, 6),
            "y": round(self.y, 6),
            "width": round(self.width, 6) if self.width is not None else None,
            "height": round(self.height, 6) if self.height is not None else None,
            "bbox": (
                [round(max(0.0, box[0]), 6), round(max(0.0, box[1]), 6),
                 round(min(1.0, box[2]), 6), round(min(1.0, box[3]), 6)]
                if box is not None else None
            ),
            "confidence": round(self.confidence, 4),
        }


@dataclass
class ObservationRecord:
    """一次真实截图对应的可信观察记录。

    除 `status` / `status_reason` 外，其余字段在创建后不再改变。
    """

    observation_id: str
    scope: str                       # window = 单窗口；desktop = 整个桌面
    window: WindowInfo | None
    window_rect: tuple[int, int, int, int]
    captured_at: str
    expires_at: str
    ttl_seconds: float
    captured_monotonic: float
    expires_monotonic: float
    image_size: tuple[int, int]
    preview_size: tuple[int, int]
    content_size: tuple[int, int]
    image_digest: str
    metrics: ScreenMetrics
    elements: list[ObservedElement] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    question: str = ""
    operable: bool = False
    mapping_problem: str = ""
    element_problems: list[str] = field(default_factory=list)
    status: str = STATUS_VALID
    status_reason: str = ""

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def element(self, element_id: str) -> ObservedElement | None:
        for item in self.elements:
            if item.element_id == element_id:
                return item
        return None

    def screen_point(self, nx: float, ny: float) -> tuple[int, int] | None:
        """把归一化坐标映射成屏幕物理像素。

        映射失败返回 None，绝不返回一个"大概对"的坐标。
        """
        image_width, image_height = self.image_size
        if image_width <= 1 or image_height <= 1:
            return None

        pixel_x = nx * (image_width - 1)
        pixel_y = ny * (image_height - 1)

        if self.scope == "window" and self.window is not None:
            left, top, right, bottom = self.window_rect
            width = right - left
            height = bottom - top
            if width <= 0 or height <= 0:
                return None
            return (
                left + dpi.round_half_up(pixel_x * (width - 1) / (image_width - 1)),
                top + dpi.round_half_up(pixel_y * (height - 1) / (image_height - 1)),
            )

        # desktop：相对整块虚拟桌面
        width = int(self.metrics.virtual_width)
        height = int(self.metrics.virtual_height)
        if width <= 1 or height <= 1:
            return None
        return (
            int(self.metrics.virtual_left) + dpi.round_half_up(nx * (width - 1)),
            int(self.metrics.virtual_top) + dpi.round_half_up(ny * (height - 1)),
        )

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "scope": self.scope,
            "status": self.status,
            "status_reason": self.status_reason,
            "operable": self.operable,
            "mapping_problem": self.mapping_problem,
            "window": self.window.to_dict() if self.window is not None else None,
            "window_rect": list(self.window_rect),
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
            "ttl_seconds": self.ttl_seconds,
            "image_size": list(self.image_size),
            "preview_size": list(self.preview_size),
            "content_size": list(self.content_size),
            "image_digest": self.image_digest,
            "metrics": self.metrics.to_dict(),
            "analysis": {
                "provider": self.provider,
                "model": self.model,
                "question": self.question,
            },
            "elements": [item.to_dict() for item in self.elements],
            "element_problems": list(self.element_problems),
        }

    def describe_for_model(self) -> str:
        """给模型看的观察摘要。只包含几何和编号，不包含图像。"""
        state = {
            STATUS_VALID: "有效",
            STATUS_EXPIRED: "已过期",
            STATUS_INVALIDATED: "已失效",
        }.get(self.status, self.status)

        lines = [
            f"可信观察记录 observation_id={self.observation_id}"
            f"（{state}，范围 {self.scope}）"
        ]
        if self.window is not None:
            title = self.window.title or "（无标题窗口）"
            process = self.window.process or "进程未知"
            lines.append(
                f"目标窗口：{title}（{process}，PID {self.window.pid}，"
                f"HWND {self.window.hwnd:#x}）"
            )
            lines.append(
                f"窗口矩形：{list(self.window_rect)}；"
                f"截图 {self.image_size[0]}x{self.image_size[1]}；"
                f"预览 {self.preview_size[0]}x{self.preview_size[1]}"
            )
        else:
            lines.append(
                f"范围：整个桌面；截图 {self.image_size[0]}x{self.image_size[1]}；"
                f"预览 {self.preview_size[0]}x{self.preview_size[1]}"
            )

        lines.append(
            f"记录时间：{self.captured_at}；有效期至：{self.expires_at}"
            f"（{self.ttl_seconds:g} 秒）"
        )

        m = self.metrics
        lines.append(
            f"屏幕度量：DPI {m.dpi}（缩放 {m.scale:g}），"
            f"感知级别 {m.awareness or 'unknown'}"
        )

        if not self.operable:
            reason = self.mapping_problem or "只有单个应用窗口的观察才能用于定位操作"
            lines.append(f"这条记录不能用于定位点击：{reason}")

        if self.elements:
            lines.append(f"可信元素（编号由服务端生成，共 {len(self.elements)} 个）：")
            for item in self.elements:
                size = ""
                if item.bbox is not None:
                    left, top, right, bottom = item.bbox
                    size = (
                        f" 边界 x={left:.3f}~{right:.3f} y={top:.3f}~{bottom:.3f}"
                    )
                lines.append(
                    f"  {item.element_id} [{item.kind}] {item.name}"
                    f" 中心 x={item.x:.3f} y={item.y:.3f}{size}"
                    f" 置信度 {item.confidence:.2f}"
                )
        else:
            lines.append("可信元素：没有识别出可定位的元素。")

        lines.append(
            "引用规则：后续操作必须同时给出上面的 observation_id 和元素编号；"
            "服务端会用记录中的几何信息重新校验。模型自己编造的 observation_id、"
            "元素编号或与记录不符的坐标都会被拒绝。"
        )
        lines.append(
            "查看屏幕本身不会产生任何鼠标或键盘操作。后续如果点击，必须由"
            "用户逐次确认；点击坐标只能由服务端根据这条记录计算。"
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class ValidationResult:
    """坐标校验结论。失败时 screen_x / screen_y 一定是 None。"""

    ok: bool
    code: str = ""
    reason: str = ""
    observation_id: str = ""
    element_id: str = ""
    x: float | None = None
    y: float | None = None
    screen_x: int | None = None
    screen_y: int | None = None
    window_rect: tuple[int, int, int, int] | None = None
    scope: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "reason": self.reason,
            "observation_id": self.observation_id,
            "element_id": self.element_id,
            "x": self.x,
            "y": self.y,
            "screen_x": self.screen_x,
            "screen_y": self.screen_y,
            "window_rect": list(self.window_rect) if self.window_rect else None,
            "scope": self.scope,
        }


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clean_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _clean_text(value: Any, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def _digest(image: Any) -> str:
    """对真正送给模型的预览图算指纹。算不了就返回空串，不阻断登记。"""
    try:
        return hashlib.sha256(image.tobytes()).hexdigest()
    except Exception:  # noqa: BLE001 - 图像对象异常不应让登记崩掉
        return ""


def _mapping_problem(snapshot: Snapshot, scope: str) -> str:
    """判断这条观察的坐标映射是否可信。返回空串表示可信。"""
    metrics = snapshot.metrics
    if metrics is None:
        return "缺少屏幕度量"
    awareness = str(getattr(metrics, "awareness", "") or "unknown")
    if awareness != "per_monitor":
        return f"进程的 DPI 感知是 {awareness}，不是 per_monitor"
    if float(getattr(metrics, "scale", 0) or 0) <= 0:
        return "屏幕缩放比例无效"
    if getattr(metrics, "consistent", None) is False:
        return "截图尺寸和系统度量对不上"

    image_width, image_height = snapshot.original_size
    if image_width <= 0 or image_height <= 0:
        return "截图尺寸无效"

    if scope == "desktop":
        if int(metrics.virtual_width) <= 0 or int(metrics.virtual_height) <= 0:
            return "系统没有给出有效的虚拟桌面尺寸"
        return ""

    window_info = snapshot.window
    if window_info is None or int(window_info.hwnd) <= 0:
        return "没有可用的目标窗口"
    if not window_info.is_valid:
        return "目标窗口的矩形无效"
    if int(window_info.pid) <= 0 and not (window_info.process or "").strip():
        return "缺少目标窗口的身份信息"
    return ""


def _build_elements(
    reading: ScreenReading, problems: list[str]
) -> list[ObservedElement]:
    """把视觉模型给的元素变成服务端编号的可信元素。

    编号按返回顺序重新生成，完全忽略模型可能塞进来的任何 id 字段。
    坐标越界或缺少名字的元素直接丢弃，并在 problems 里留下原因。
    """
    out: list[ObservedElement] = []
    for index, item in enumerate(getattr(reading, "elements", []) or [], 1):
        name = _clean_text(getattr(item, "name", ""))
        if not name:
            problems.append(f"第 {index} 个元素没有名字，已丢弃")
            continue

        x = _as_float(getattr(item, "x", None))
        y = _as_float(getattr(item, "y", None))
        if x is None or y is None or not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            problems.append(f"元素「{name}」的坐标无效，已丢弃")
            continue

        width = _as_float(getattr(item, "width", None))
        height = _as_float(getattr(item, "height", None))
        if (
            width is None or height is None
            or width <= 0 or height <= 0
        ):
            width = None
            height = None
        else:
            width = min(1.0, width)
            height = min(1.0, height)

        confidence = _as_float(getattr(item, "confidence", 0.0))
        if confidence is None:
            confidence = 0.0

        out.append(ObservedElement(
            element_id=f"e{len(out) + 1}",
            name=name,
            kind=_clean_text(getattr(item, "kind", ""), 40) or "other",
            x=x,
            y=y,
            width=width,
            height=height,
            confidence=max(0.0, min(1.0, confidence)),
        ))
    return out


def _metrics_signature(metrics: ScreenMetrics) -> tuple[Any, ...]:
    """只比较会影响坐标映射的字段。"""
    return (
        int(getattr(metrics, "dpi", 0) or 0),
        float(getattr(metrics, "scale", 0.0) or 0.0),
        str(getattr(metrics, "awareness", "") or ""),
        int(getattr(metrics, "virtual_left", 0) or 0),
        int(getattr(metrics, "virtual_top", 0) or 0),
        int(getattr(metrics, "virtual_width", 0) or 0),
        int(getattr(metrics, "virtual_height", 0) or 0),
        int(getattr(metrics, "primary_width", 0) or 0),
        int(getattr(metrics, "primary_height", 0) or 0),
        int(getattr(metrics, "monitor_count", 0) or 0),
    )


# ----------------------------------------------------------------------
# 登记处
# ----------------------------------------------------------------------
class ObservationStore:
    """内存里的观察记录登记处。

    所有外部依赖（时钟、窗口查询、屏幕度量）都能从构造函数注入，
    测试因此可以完全离线运行，不会碰真实桌面。

    等待确认、截图这类操作可能来自不同线程（工作流在后台跑），
    所以内部用一把可重入锁保护字典。锁不会包住系统查询。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        window_lookup: Callable[[int], WindowInfo | None] | None = None,
        metrics_lookup: Callable[[], ScreenMetrics] | None = None,
    ) -> None:
        self.ttl_seconds = max(0.1, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or datetime.now
        self._window_lookup = window_lookup
        self._metrics_lookup = metrics_lookup
        self._records: dict[str, ObservationRecord] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 依赖
    # ------------------------------------------------------------------
    def _lookup_window(self, hwnd: int) -> WindowInfo | None:
        if self._window_lookup is not None:
            return self._window_lookup(hwnd)
        return window_mod.get_window_info(hwnd)

    def _current_metrics(self) -> ScreenMetrics:
        if self._metrics_lookup is not None:
            return self._metrics_lookup()
        return dpi.screen_metrics()

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------
    def create_from_capture(
        self,
        snapshot: Snapshot,
        reading: ScreenReading,
        *,
        scope: str = "window",
        ttl_seconds: float | None = None,
        content_size: tuple[int, int] | None = None,
    ) -> ObservationRecord:
        """在一次成功的真实捕获 + 视觉分析之后建立记录。

        这是唯一能创建记录的入口，只在工具执行路径里调用。
        模型无法调用它，也无法提供 `observation_id`。
        """
        if snapshot is None or reading is None:
            raise ValueError("建立观察记录需要真实的截图和分析结果")

        active_scope = str(scope or "").strip().lower()
        if active_scope not in ("window", "desktop"):
            raise ValueError(f"不支持的观察范围：{scope!r}")

        window_info = snapshot.window
        if active_scope == "window" and (
            window_info is None or int(window_info.hwnd) <= 0
        ):
            raise ValueError("窗口观察缺少目标窗口")

        mapping_problem = _mapping_problem(snapshot, active_scope)
        element_problems: list[str] = []
        elements = _build_elements(reading, element_problems)

        ttl = self.ttl_seconds if ttl_seconds is None else max(0.1, float(ttl_seconds))
        now = float(self._clock())
        wall_now = self._wall_clock()
        captured_at = str(getattr(snapshot, "taken_at", "") or "").strip()
        if not captured_at:
            captured_at = wall_now.isoformat(timespec="seconds")

        rect = (
            tuple(int(v) for v in window_info.rect)
            if window_info is not None
            else (0, 0, 0, 0)
        )
        image_size = tuple(int(v) for v in snapshot.original_size)
        preview_size = tuple(int(v) for v in snapshot.preview_size)
        effective_content = tuple(content_size) if content_size else image_size

        record = ObservationRecord(
            observation_id=f"obs_{uuid.uuid4().hex}",
            scope=active_scope,
            window=window_info,
            window_rect=rect,  # type: ignore[arg-type]
            captured_at=captured_at,
            expires_at=(wall_now + timedelta(seconds=ttl)).isoformat(
                timespec="seconds"
            ),
            ttl_seconds=ttl,
            captured_monotonic=now,
            expires_monotonic=now + ttl,
            image_size=image_size,  # type: ignore[assignment]
            preview_size=preview_size,  # type: ignore[assignment]
            content_size=effective_content,  # type: ignore[assignment]
            image_digest=_digest(snapshot.preview),
            metrics=snapshot.metrics,
            elements=elements,
            provider=_clean_text(getattr(reading, "provider", ""), 100),
            model=_clean_text(getattr(reading, "model", ""), 100),
            question=_clean_text(getattr(reading, "question", ""), 500),
            operable=(active_scope == "window" and not mapping_problem),
            mapping_problem=mapping_problem,
            element_problems=element_problems,
        )

        with self._lock:
            self._refresh_all_locked(now)
            self._records[record.observation_id] = record
            self._trim_locked()

        logger.info(
            "登记观察记录 %s：范围 %s，HWND %s，元素 %d 个，有效期 %.0f 秒",
            record.observation_id,
            record.scope,
            window_info.hwnd if window_info is not None else "desktop",
            len(record.elements),
            ttl,
        )
        return record

    # ------------------------------------------------------------------
    # 查询 / 失效
    # ------------------------------------------------------------------
    def get(self, observation_id: str) -> ObservationRecord | None:
        key = _clean_id(observation_id)
        if not key:
            return None
        with self._lock:
            record = self._records.get(key)
            if record is not None:
                self._refresh_one_locked(record, float(self._clock()))
            return record

    def all(self) -> list[ObservationRecord]:
        with self._lock:
            self._refresh_all_locked(float(self._clock()))
            return sorted(
                self._records.values(),
                key=lambda item: item.captured_monotonic,
            )

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def invalidate(self, observation_id: str, reason: str = "") -> bool:
        key = _clean_id(observation_id)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return False
            if record.status == STATUS_VALID:
                record.status = STATUS_INVALIDATED
                record.status_reason = _clean_text(reason) or "已主动失效"
            return True

    def prune(self) -> int:
        with self._lock:
            self._refresh_all_locked(float(self._clock()))
            dead = [
                key for key, record in self._records.items()
                if record.status != STATUS_VALID
            ]
            for key in dead:
                self._records.pop(key, None)
            return len(dead)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    # ------------------------------------------------------------------
    # 坐标校验
    # ------------------------------------------------------------------
    def validate(
        self,
        observation_id: str,
        element_id: str,
        *,
        x: Any = None,
        y: Any = None,
        tolerance: float = DEFAULT_COORD_TOLERANCE,
    ) -> ValidationResult:
        """校验一个后续操作引用的坐标现在是否仍然可信。

        成功时返回映射后的屏幕物理坐标；任何一项不确定都返回 ok=False，
        并且 screen_x / screen_y 一定是 None。
        """
        obs_key = _clean_id(observation_id)
        element_key = _clean_id(element_id)
        if not obs_key or not element_key:
            return self._fail(
                CODE_INVALID_ARGUMENTS,
                "observation_id 和 element_id 都必须是非空字符串。",
                observation_id=obs_key,
                element_id=element_key,
            )

        try:
            record = self.get(obs_key)
            if record is None:
                return self._fail(
                    CODE_NOT_FOUND,
                    "观察记录不存在或已被清理，请重新查看屏幕后再操作。",
                    observation_id=obs_key,
                    element_id=element_key,
                )

            if record.status == STATUS_INVALIDATED:
                return self._fail(
                    CODE_INVALIDATED,
                    f"这条观察记录已失效：{record.status_reason or '已主动失效'}。"
                    "请重新查看屏幕。",
                    observation_id=obs_key,
                    element_id=element_key,
                    scope=record.scope,
                )

            if record.status == STATUS_EXPIRED:
                return self._fail(
                    CODE_EXPIRED,
                    f"观察记录已过期（有效期 {record.ttl_seconds:g} 秒），"
                    "请重新查看屏幕后再操作。",
                    observation_id=obs_key,
                    element_id=element_key,
                    scope=record.scope,
                )

            if record.scope != "window":
                return self._fail(
                    CODE_SCOPE,
                    "整桌面观察不能用于定位点击，请针对具体应用窗口重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    scope=record.scope,
                )

            if not record.operable:
                return self._fail(
                    CODE_MAPPING,
                    "这条观察记录的坐标映射不可信："
                    f"{record.mapping_problem or '缺少可用的窗口上下文'}。"
                    "请重新查看屏幕。",
                    observation_id=obs_key,
                    element_id=element_key,
                    scope=record.scope,
                )

            element = record.element(element_key)
            if element is None:
                available = "、".join(item.element_id for item in record.elements)
                return self._fail(
                    CODE_ELEMENT_NOT_FOUND,
                    f"观察记录里没有元素 {element_key!r}。"
                    f"这条记录可用的元素编号是：{available or '（无）'}。"
                    "元素编号必须来自同一次观察，不能编造。",
                    observation_id=obs_key,
                    element_id=element_key,
                    scope=record.scope,
                )

            # ---- 坐标：要么用元素中心，要么必须落在元素的可信边界内 ----
            has_x = x is not None
            has_y = y is not None
            if has_x != has_y:
                return self._fail(
                    CODE_COORDINATES,
                    "x 和 y 必须同时提供，或者都不提供（使用元素中心点）。",
                    observation_id=obs_key,
                    element_id=element_key,
                    scope=record.scope,
                )

            if has_x:
                nx = _as_float(x)
                ny = _as_float(y)
                if nx is None or ny is None:
                    return self._fail(
                        CODE_COORDINATES,
                        "x / y 必须是 0~1 之间的数字。",
                        observation_id=obs_key,
                        element_id=element_key,
                        scope=record.scope,
                    )
                if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
                    return self._fail(
                        CODE_COORDINATES_OUT_OF_BOUNDS,
                        f"坐标 ({nx:g}, {ny:g}) 超出截图范围 0~1。",
                        observation_id=obs_key,
                        element_id=element_key,
                        x=nx,
                        y=ny,
                        scope=record.scope,
                    )
                if not element.contains(nx, ny, tolerance):
                    box = element.bbox
                    where = (
                        f"可信边界 x={box[0]:.3f}~{box[2]:.3f}，"
                        f"y={box[1]:.3f}~{box[3]:.3f}"
                        if box is not None
                        else f"中心 x={element.x:.3f}，y={element.y:.3f}"
                    )
                    return self._fail(
                        CODE_COORDINATES_MISMATCH,
                        f"坐标与可信观察记录不一致：元素「{element.name}」"
                        f"的{where}，传入点 ({nx:g}, {ny:g}) 不在其中。"
                        "不会按模型给的坐标猜测。",
                        observation_id=obs_key,
                        element_id=element_key,
                        x=nx,
                        y=ny,
                        scope=record.scope,
                    )
            else:
                nx, ny = element.x, element.y

            # ---- 窗口现在还在不在、还是不是同一个 ----
            assert record.window is not None
            info = self._lookup_window(int(record.window.hwnd))
            if info is None:
                return self._fail(
                    CODE_WINDOW_GONE,
                    "目标窗口已关闭或句柄失效，旧坐标不再可信。请重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    scope=record.scope,
                )

            if not info.visible:
                return self._fail(
                    CODE_WINDOW_HIDDEN,
                    "目标窗口当前不可见，旧坐标不再可信。请先恢复窗口再重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    scope=record.scope,
                )
            if info.minimized:
                return self._fail(
                    CODE_WINDOW_MINIMIZED,
                    "目标窗口已最小化，旧坐标不再可信。请先恢复窗口再重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    scope=record.scope,
                )
            if not info.is_valid:
                return self._fail(
                    CODE_WINDOW_CHANGED,
                    "目标窗口的矩形已经无效，旧坐标不再可信。请重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    scope=record.scope,
                )

            recorded = record.window
            if int(info.hwnd) != int(recorded.hwnd):
                return self._fail(
                    CODE_WINDOW_CHANGED,
                    "目标窗口句柄已经变化，旧坐标不再可信。请重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    scope=record.scope,
                )
            if recorded.pid > 0 and int(info.pid) != int(recorded.pid):
                return self._fail(
                    CODE_WINDOW_CHANGED,
                    "目标窗口已经变成另一个进程，旧坐标不再可信。请重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    scope=record.scope,
                )
            if (
                recorded.process
                and (info.process or "").casefold()
                != recorded.process.casefold()
            ):
                return self._fail(
                    CODE_WINDOW_CHANGED,
                    "目标窗口的进程名已经变化，旧坐标不再可信。请重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    scope=record.scope,
                )
            if (info.title or "") != (recorded.title or ""):
                return self._fail(
                    CODE_WINDOW_CHANGED,
                    "目标窗口的标题已经变化，界面可能已刷新。请重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    scope=record.scope,
                )
            if (
                recorded.class_name and info.class_name
                and info.class_name != recorded.class_name
            ):
                return self._fail(
                    CODE_WINDOW_CHANGED,
                    "目标窗口的窗口类已经变化，旧坐标不再可信。请重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    scope=record.scope,
                )

            if tuple(info.rect) != tuple(record.window_rect):
                old_w = record.window_rect[2] - record.window_rect[0]
                old_h = record.window_rect[3] - record.window_rect[1]
                new_w = info.width
                new_h = info.height
                if (old_w, old_h) != (new_w, new_h):
                    return self._fail(
                        CODE_WINDOW_RESIZED,
                        f"目标窗口的尺寸已经变化（记录 {old_w}x{old_h}，"
                        f"现在是 {new_w}x{new_h}）。旧坐标不再可信，请重新观察。",
                        observation_id=obs_key,
                        element_id=element_key,
                        x=nx,
                        y=ny,
                        window_rect=tuple(info.rect),  # type: ignore[arg-type]
                        scope=record.scope,
                    )
                return self._fail(
                    CODE_WINDOW_MOVED,
                    f"目标窗口的位置已经变化（记录 {list(record.window_rect)}，"
                    f"现在是 {list(info.rect)}）。旧坐标不再可信，请重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    window_rect=tuple(info.rect),  # type: ignore[arg-type]
                    scope=record.scope,
                )

            # ---- DPI / 屏幕度量 ----
            current_metrics = self._current_metrics()
            if current_metrics is None:
                return self._fail(
                    CODE_ENVIRONMENT_CHANGED,
                    "无法读取当前屏幕度量，不能确认坐标仍然有效。请重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    window_rect=tuple(info.rect),  # type: ignore[arg-type]
                    scope=record.scope,
                )
            if _metrics_signature(current_metrics) != _metrics_signature(record.metrics):
                return self._fail(
                    CODE_ENVIRONMENT_CHANGED,
                    "DPI 或屏幕度量已经变化（记录 DPI "
                    f"{record.metrics.dpi}，现在 {current_metrics.dpi}）。"
                    "旧坐标不再可信，请重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    window_rect=tuple(info.rect),  # type: ignore[arg-type]
                    scope=record.scope,
                )

            # ---- 映射后的点必须在窗口和屏幕范围内 ----
            point = record.screen_point(nx, ny)
            if point is None:
                return self._fail(
                    CODE_MAPPING,
                    "无法把元素坐标映射到屏幕，旧坐标不再可信。请重新观察。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    scope=record.scope,
                )
            screen_x, screen_y = point

            left, top, right, bottom = info.rect
            if not (left <= screen_x < right and top <= screen_y < bottom):
                return self._fail(
                    CODE_POINT_OUT_OF_BOUNDS,
                    f"映射后的屏幕坐标 ({screen_x}, {screen_y}) 超出目标窗口范围。"
                    "不会点击窗口外的位置。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    window_rect=tuple(info.rect),  # type: ignore[arg-type]
                    scope=record.scope,
                )

            vx = int(current_metrics.virtual_left)
            vy = int(current_metrics.virtual_top)
            vw = int(current_metrics.virtual_width)
            vh = int(current_metrics.virtual_height)
            if not (vx <= screen_x < vx + vw and vy <= screen_y < vy + vh):
                return self._fail(
                    CODE_POINT_OUT_OF_BOUNDS,
                    f"映射后的屏幕坐标 ({screen_x}, {screen_y}) 不在可见桌面范围内。"
                    "不会点击屏幕外的位置。",
                    observation_id=obs_key,
                    element_id=element_key,
                    x=nx,
                    y=ny,
                    window_rect=tuple(info.rect),  # type: ignore[arg-type]
                    scope=record.scope,
                )

            return ValidationResult(
                ok=True,
                code="ok",
                reason="坐标仍然有效。",
                observation_id=obs_key,
                element_id=element_key,
                x=round(nx, 6),
                y=round(ny, 6),
                screen_x=screen_x,
                screen_y=screen_y,
                window_rect=tuple(info.rect),  # type: ignore[arg-type]
                scope=record.scope,
            )

        except Exception as exc:  # noqa: BLE001 - 校验绝不把异常抛给调用方
            logger.exception("校验观察坐标时出错")
            return self._fail(
                CODE_INTERNAL,
                f"校验过程中出错：{type(exc).__name__}: {exc}",
                observation_id=obs_key,
                element_id=element_key,
            )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _fail(
        code: str,
        reason: str,
        *,
        observation_id: str = "",
        element_id: str = "",
        x: float | None = None,
        y: float | None = None,
        window_rect: tuple[int, int, int, int] | None = None,
        scope: str = "",
    ) -> ValidationResult:
        # 失败时无论调用方传了什么坐标，都不返回可执行的目标点。
        return ValidationResult(
            ok=False,
            code=code,
            reason=reason,
            observation_id=observation_id,
            element_id=element_id,
            x=x,
            y=y,
            screen_x=None,
            screen_y=None,
            window_rect=window_rect,
            scope=scope,
        )

    @staticmethod
    def _refresh_one_locked(record: ObservationRecord, now: float) -> None:
        if record.status == STATUS_VALID and now >= record.expires_monotonic:
            record.status = STATUS_EXPIRED
            record.status_reason = "超过有效期"

    def _refresh_all_locked(self, now: float) -> None:
        for record in self._records.values():
            self._refresh_one_locked(record, now)

    def _trim_locked(self) -> None:
        """记录无限增长会吃内存。超过上限时先清理过期/失效的，再清最旧的。"""
        if len(self._records) <= self.max_entries:
            return
        ordered = sorted(
            self._records.values(), key=lambda item: item.captured_monotonic
        )
        for record in ordered:
            if len(self._records) <= self.max_entries:
                break
            if record.status != STATUS_VALID:
                self._records.pop(record.observation_id, None)
        for record in ordered:
            if len(self._records) <= self.max_entries:
                break
            self._records.pop(record.observation_id, None)


# 全局登记处。生产代码只通过它登记和校验。
store = ObservationStore()


def validate_click_target(
    observation_id: str,
    element_id: str,
    *,
    x: Any = None,
    y: Any = None,
    tolerance: float = DEFAULT_COORD_TOLERANCE,
    registry: ObservationStore | None = None,
) -> ValidationResult:
    """独立的坐标校验入口，供未来的点击工具调用。

    本阶段没有任何工具调用它。它不发送输入，只回答"这个点现在还能不能点"。
    """
    target = registry or store
    return target.validate(
        observation_id, element_id, x=x, y=y, tolerance=tolerance
    )
