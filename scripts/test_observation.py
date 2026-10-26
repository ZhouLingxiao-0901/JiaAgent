"""可信观察登记 + 坐标校验测试。

全部离线：假窗口、假屏幕度量、假时钟。不截图、不联网、不动鼠标键盘，
也不会创建任何真实桌面输入。

覆盖：
  - 正常登记与坐标映射
  - 观察记录不存在 / 元素不存在 / 跨观察引用
  - 记录过期 / 主动失效
  - 窗口关闭、隐藏、最小化、移动、缩放、身份变化
  - DPI 或屏幕度量变化
  - 坐标越界、只给一个轴、非数字
  - 模型伪造坐标 / 与可信记录不一致
  - 整桌面观察不能用于点击
  - 点击工具已登记，但默认没有输入后端，也不会产生真实输入

用法：
    .venv\\Scripts\\python.exe scripts\\test_observation.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from app.vision import observation  # noqa: E402
from app.vision.analyze import ScreenReading, UiElement  # noqa: E402
from app.vision.model import ScreenMetrics, Snapshot, WindowInfo  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


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


class FakeDesktop:
    """只返回测试指定的窗口和度量，绝不访问真实系统。"""

    def __init__(self, window: WindowInfo | None, metrics: ScreenMetrics) -> None:
        self.window = window
        self.metrics = metrics
        self.window_calls = 0
        self.metrics_calls = 0

    def lookup_window(self, _hwnd: int) -> WindowInfo | None:
        self.window_calls += 1
        return self.window

    def lookup_metrics(self) -> ScreenMetrics:
        self.metrics_calls += 1
        return self.metrics


def win_info(
    *,
    hwnd: int = 7,
    title: str = "记事本",
    process: str = "notepad.exe",
    pid: int = 42,
    class_name: str = "Notepad",
    rect: tuple[int, int, int, int] = (100, 50, 900, 650),
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


def metrics(
    *,
    dpi_value: int = 96,
    scale: float = 1.0,
    virtual_left: int = 0,
    virtual_top: int = 0,
    virtual_width: int = 1920,
    virtual_height: int = 1080,
    awareness: str = "per_monitor",
    consistent: bool | None = True,
) -> ScreenMetrics:
    return ScreenMetrics(
        primary_width=1920,
        primary_height=1080,
        virtual_left=virtual_left,
        virtual_top=virtual_top,
        virtual_width=virtual_width,
        virtual_height=virtual_height,
        monitor_count=1,
        dpi=dpi_value,
        scale=scale,
        awareness=awareness,
        awareness_method="test",
        consistent=consistent,
    )


def snapshot(
    window: WindowInfo | None,
    screen: ScreenMetrics,
    *,
    image_size: tuple[int, int] = (800, 600),
    preview_size: tuple[int, int] = (400, 300),
) -> Snapshot:
    image = Image.new("RGB", image_size, (20, 20, 20))
    preview = image.resize(preview_size)
    return Snapshot(
        image=image,
        preview=preview,
        metrics=screen,
        window=window,
        taken_at="2026-10-21T12:00:00",
        original_size=image_size,
        preview_size=preview_size,
    )


def element(
    name: str = "登录",
    *,
    x: float = 0.5,
    y: float = 0.5,
    width: float | None = 0.2,
    height: float | None = 0.1,
    kind: str = "button",
    confidence: float = 0.9,
) -> UiElement:
    return UiElement(
        name=name, kind=kind, x=x, y=y,
        width=width, height=height, confidence=confidence,
    )


def reading(elements: list[UiElement] | None = None) -> ScreenReading:
    return ScreenReading(
        description="测试界面",
        elements=list(elements or [element()]),
        provider="fake",
        model="fake-vision",
        question="登录按钮在哪",
        scope="window",
    )


def make_store(
    desktop: FakeDesktop,
    clock: Clock,
    *,
    ttl: float = 30.0,
    max_entries: int = 64,
) -> observation.ObservationStore:
    return observation.ObservationStore(
        ttl_seconds=ttl,
        max_entries=max_entries,
        clock=clock,
        wall_clock=lambda: datetime(2026, 10, 21, 12, 0, 0),
        window_lookup=desktop.lookup_window,
        metrics_lookup=desktop.lookup_metrics,
    )


def standard() -> tuple[FakeDesktop, Clock, observation.ObservationStore]:
    desktop = FakeDesktop(win_info(), metrics())
    clock = Clock()
    return desktop, clock, make_store(desktop, clock)


# ----------------------------------------------------------------------
# 一、正常登记与校验
# ----------------------------------------------------------------------
def test_normal() -> None:
    print("\n[一] 正常观察记录与坐标校验")

    desktop, clock, store = standard()
    snap = snapshot(desktop.window, desktop.metrics)
    record = store.create_from_capture(snap, reading(), scope="window")

    check("observation_id 由服务端生成", record.observation_id.startswith("obs_"),
          record.observation_id)
    check("默认状态有效", record.status == observation.STATUS_VALID, record.status)
    check("记录可操作", record.operable is True, record.mapping_problem)
    check("元素编号由服务端生成", [e.element_id for e in record.elements] == ["e1"],
          str([e.element_id for e in record.elements]))
    check("记录了窗口句柄和矩形",
          record.window is not None and record.window.hwnd == 7
          and record.window_rect == (100, 50, 900, 650),
          f"{record.window_rect}")
    check("记录了截图尺寸", record.image_size == (800, 600), str(record.image_size))
    check("记录了预览尺寸", record.preview_size == (400, 300), str(record.preview_size))
    check("记录了 DPI 和缩放",
          record.metrics.dpi == 96 and record.metrics.scale == 1.0,
          f"{record.metrics.dpi}/{record.metrics.scale}")
    check("记录了截图指纹（不保存图像）", len(record.image_digest) == 64,
          record.image_digest[:16])
    check("记录了有效期", record.ttl_seconds == 30.0
          and record.expires_monotonic == 1030.0, str(record.expires_monotonic))
    check("登记处里有一条记录", store.count() == 1, str(store.count()))

    result = store.validate(record.observation_id, "e1")
    check("不带坐标时使用元素中心并通过", result.ok is True, result.reason)
    check("映射到正确的屏幕坐标",
          (result.screen_x, result.screen_y) == (500, 350),
          f"{result.screen_x},{result.screen_y}")
    check("返回了窗口矩形", result.window_rect == (100, 50, 900, 650),
          str(result.window_rect))
    check("校验失败字段为空", result.code == "ok", result.code)

    inside = store.validate(record.observation_id, "e1", x=0.55, y=0.52)
    check("元素边界框内的坐标通过", inside.ok is True, inside.reason)
    check("坐标原样返回", (inside.x, inside.y) == (0.55, 0.52),
          f"{inside.x},{inside.y}")
    check("只读校验只读取窗口信息，不产生输入", desktop.window_calls == 2,
          f"window_lookup={desktop.window_calls}")

    as_dict = record.to_dict()
    check("对外的记录不含图像对象", "image" not in as_dict and "preview" not in as_dict,
          str(sorted(as_dict)))
    check("对外记录包含元素和窗口",
          as_dict["window"]["hwnd"] == 7 and as_dict["elements"][0]["element_id"] == "e1")

    text = record.describe_for_model()
    check("给模型的摘要有 observation_id", record.observation_id in text)
    check("给模型的摘要有元素编号", "e1" in text)
    check("给模型的摘要说明不会点击", "不会产生任何鼠标或键盘操作" in text)
    check("给模型的摘要不宣称点击成功", "点击成功" not in text)


# ----------------------------------------------------------------------
# 二、不存在的记录 / 元素 / 跨观察引用
# ----------------------------------------------------------------------
def test_missing_and_forged() -> None:
    print("\n[二] 不存在的记录、不存在的元素、跨观察引用")

    desktop, _, store = standard()
    snap = snapshot(desktop.window, desktop.metrics)
    rec1 = store.create_from_capture(snap, reading([element("登录")]), scope="window")
    rec2 = store.create_from_capture(
        snap, reading([element("登录"), element("取消", x=0.7)]), scope="window"
    )

    result = store.validate("obs_does_not_exist", "e1")
    check("不存在的观察记录被拒绝",
          result.ok is False and result.code == observation.CODE_NOT_FOUND,
          result.code)
    check("失败时不返回屏幕坐标",
          result.screen_x is None and result.screen_y is None)

    result = store.validate(rec1.observation_id, "e9")
    check("不存在的元素被拒绝",
          result.ok is False and result.code == observation.CODE_ELEMENT_NOT_FOUND,
          result.code)
    check("拒绝原因提示编号不能编造", "不能编造" in result.reason, result.reason)

    result = store.validate(rec1.observation_id, "e2")
    check("引用另一条观察的元素编号被拒绝",
          result.ok is False and result.code == observation.CODE_ELEMENT_NOT_FOUND,
          result.code)
    check("另一条观察的记录本身仍然有效",
          store.validate(rec2.observation_id, "e2").ok is True)

    result = store.validate("", "e1")
    check("空 observation_id 被拒绝",
          result.ok is False and result.code == observation.CODE_INVALID_ARGUMENTS,
          result.code)
    result = store.validate(rec1.observation_id, 123)
    check("非字符串 element_id 被拒绝",
          result.ok is False and result.code == observation.CODE_INVALID_ARGUMENTS,
          result.code)


# ----------------------------------------------------------------------
# 三、过期与主动失效
# ----------------------------------------------------------------------
def test_expiry_and_invalidation() -> None:
    print("\n[三] 观察记录过期 / 主动失效")

    desktop, clock, store = standard()
    snap = snapshot(desktop.window, desktop.metrics)
    rec = store.create_from_capture(snap, reading(), scope="window")

    clock.advance(29.9)
    check("到期前仍然有效", store.validate(rec.observation_id, "e1").ok is True)
    clock.advance(0.2)
    result = store.validate(rec.observation_id, "e1")
    check("到期后被拒绝",
          result.ok is False and result.code == observation.CODE_EXPIRED,
          result.code)
    check("过期原因写明了有效期", "30" in result.reason, result.reason)
    check("过期后不返回坐标",
          result.screen_x is None and result.screen_y is None)
    check("记录状态变成 expired",
          store.get(rec.observation_id).status == observation.STATUS_EXPIRED,
          store.get(rec.observation_id).status)

    desktop2, _, store2 = standard()
    rec2 = store2.create_from_capture(
        snapshot(desktop2.window, desktop2.metrics), reading(), scope="window"
    )
    check("主动失效成功", store2.invalidate(rec2.observation_id, "窗口换了页面") is True)
    result = store2.validate(rec2.observation_id, "e1")
    check("主动失效后拒绝",
          result.ok is False and result.code == observation.CODE_INVALIDATED,
          result.code)
    check("失效原因保留", "窗口换了页面" in result.reason, result.reason)
    check("对不存在的记录调用失效返回 False",
          store2.invalidate("obs_nope", "x") is False)


# ----------------------------------------------------------------------
# 四、窗口变化
# ----------------------------------------------------------------------
def test_window_changes() -> None:
    print("\n[四] 窗口关闭 / 隐藏 / 最小化 / 移动 / 缩放 / 身份变化")

    cases = [
        (
            "窗口关闭",
            None,
            observation.CODE_WINDOW_GONE,
            "关闭",
        ),
        (
            "窗口隐藏",
            win_info(visible=False),
            observation.CODE_WINDOW_HIDDEN,
            "不可见",
        ),
        (
            "窗口最小化",
            win_info(minimized=True),
            observation.CODE_WINDOW_MINIMIZED,
            "最小化",
        ),
        (
            "窗口移动",
            win_info(rect=(110, 50, 910, 650)),
            observation.CODE_WINDOW_MOVED,
            "位置",
        ),
        (
            "窗口缩放",
            win_info(rect=(100, 50, 1000, 650)),
            observation.CODE_WINDOW_RESIZED,
            "尺寸",
        ),
        (
            "PID 变化",
            win_info(pid=99),
            observation.CODE_WINDOW_CHANGED,
            "进程",
        ),
        (
            "进程名变化",
            win_info(process="evil.exe"),
            observation.CODE_WINDOW_CHANGED,
            "进程",
        ),
        (
            "标题变化",
            win_info(title="另一个窗口"),
            observation.CODE_WINDOW_CHANGED,
            "标题",
        ),
        (
            "窗口类变化",
            win_info(class_name="OtherClass"),
            observation.CODE_WINDOW_CHANGED,
            "窗口类",
        ),
    ]

    for label, after, code, keyword in cases:
        desktop, _, store = standard()
        rec = store.create_from_capture(
            snapshot(desktop.window, desktop.metrics), reading(), scope="window"
        )
        desktop.window = after
        result = store.validate(rec.observation_id, "e1")
        check(f"{label} -> 拒绝",
              result.ok is False and result.code == code,
              f"{result.code} {result.reason}")
        check(f"{label}：原因可读", keyword in result.reason, result.reason)
        check(f"{label}：不返回坐标",
              result.screen_x is None and result.screen_y is None)


# ----------------------------------------------------------------------
# 五、DPI / 屏幕度量变化
# ----------------------------------------------------------------------
def test_environment_changes() -> None:
    print("\n[五] DPI 或坐标映射条件变化")

    cases = [
        ("DPI 从 96 变成 144",
         metrics(dpi_value=144, scale=1.5)),
        ("虚拟桌面尺寸变化",
         metrics(virtual_width=2560, virtual_height=1440)),
        ("虚拟桌面原点变化",
         metrics(virtual_left=-1920)),
        ("多显示器数量变化",
         ScreenMetrics(
             primary_width=1920, primary_height=1080,
             virtual_left=0, virtual_top=0,
             virtual_width=3840, virtual_height=1080,
             monitor_count=2, dpi=96, scale=1.0,
             awareness="per_monitor", consistent=True,
         )),
        ("DPI 感知级别变化",
         metrics(awareness="system")),
    ]

    for label, after_metrics in cases:
        desktop, _, store = standard()
        rec = store.create_from_capture(
            snapshot(desktop.window, desktop.metrics), reading(), scope="window"
        )
        desktop.metrics = after_metrics
        result = store.validate(rec.observation_id, "e1")
        check(f"{label} -> 拒绝",
              result.ok is False and result.code == observation.CODE_ENVIRONMENT_CHANGED,
              f"{result.code} {result.reason}")
        check(f"{label}：不返回坐标",
              result.screen_x is None and result.screen_y is None)


# ----------------------------------------------------------------------
# 六、坐标越界 / 伪造
# ----------------------------------------------------------------------
def test_coordinates() -> None:
    print("\n[六] 坐标越界、只给一个轴、非数字、与记录不一致")

    desktop, _, store = standard()
    rec = store.create_from_capture(
        snapshot(desktop.window, desktop.metrics), reading(), scope="window"
    )
    oid = rec.observation_id

    cases = [
        ("x 大于 1", {"x": 1.2, "y": 0.5}, observation.CODE_COORDINATES_OUT_OF_BOUNDS),
        ("y 小于 0", {"x": 0.5, "y": -0.1}, observation.CODE_COORDINATES_OUT_OF_BOUNDS),
        ("只给 x", {"x": 0.5}, observation.CODE_COORDINATES),
        ("只给 y", {"y": 0.5}, observation.CODE_COORDINATES),
        ("非数字", {"x": "abc", "y": 0.5}, observation.CODE_COORDINATES),
        ("布尔值", {"x": True, "y": False}, observation.CODE_COORDINATES),
        ("NaN", {"x": float("nan"), "y": 0.5}, observation.CODE_COORDINATES),
        ("元素之外", {"x": 0.8, "y": 0.5}, observation.CODE_COORDINATES_MISMATCH),
    ]

    for label, kwargs, code in cases:
        result = store.validate(oid, "e1", **kwargs)
        check(f"{label} -> 拒绝", result.ok is False and result.code == code,
              f"{result.code} {result.reason}")
        check(f"{label}：不返回坐标",
              result.screen_x is None and result.screen_y is None)

    result = store.validate(oid, "e1", x=0.8, y=0.5)
    check("不一致时明确说明不会猜测", "不会按模型给的坐标猜测" in result.reason,
          result.reason)
    check("不一致时列出可信边界", "可信边界" in result.reason, result.reason)

    # 没有宽高的元素：只认中心附近的小范围
    rec2 = store.create_from_capture(
        snapshot(desktop.window, desktop.metrics),
        reading([element("图标", width=None, height=None)]),
        scope="window",
    )
    ok_near = store.validate(rec2.observation_id, "e1", x=0.505, y=0.505)
    bad_far = store.validate(rec2.observation_id, "e1", x=0.7, y=0.7)
    check("无边框元素：中心附近通过", ok_near.ok is True, ok_near.reason)
    check("无边框元素：远处坐标拒绝", bad_far.ok is False, bad_far.reason)


# ----------------------------------------------------------------------
# 七、整桌面 / 映射不可信
# ----------------------------------------------------------------------
def test_scope_and_mapping() -> None:
    print("\n[七] 整桌面观察不能用于点击；映射条件不可信时拒绝")

    desktop, _, store = standard()
    desktop_snap = snapshot(desktop.window, desktop.metrics)
    rec = store.create_from_capture(desktop_snap, reading(), scope="desktop")
    check("整桌面观察也会登记", store.count() == 1)
    check("整桌面观察不可操作", rec.operable is False)
    result = store.validate(rec.observation_id, "e1")
    check("整桌面观察的坐标校验被拒绝",
          result.ok is False and result.code == observation.CODE_SCOPE,
          result.code)
    check("拒绝原因说明要看具体窗口", "具体应用窗口" in result.reason, result.reason)

    desktop2 = FakeDesktop(win_info(), metrics(awareness="system"))
    store2 = make_store(desktop2, Clock())
    rec2 = store2.create_from_capture(
        snapshot(desktop2.window, desktop2.metrics), reading(), scope="window"
    )
    check("非 per_monitor 感知时记录仍保存", store2.count() == 1)
    check("非 per_monitor 感知时不可操作", rec2.operable is False)
    check("映射问题写明了原因", "per_monitor" in rec2.mapping_problem,
          rec2.mapping_problem)
    result = store2.validate(rec2.observation_id, "e1")
    check("映射不可信时拒绝",
          result.ok is False and result.code == observation.CODE_MAPPING,
          result.code)

    desktop3 = FakeDesktop(win_info(), metrics(consistent=False))
    store3 = make_store(desktop3, Clock())
    rec3 = store3.create_from_capture(
        snapshot(desktop3.window, desktop3.metrics), reading(), scope="window"
    )
    result = store3.validate(rec3.observation_id, "e1")
    check("截图尺寸和度量对不上时拒绝",
          result.ok is False and result.code == observation.CODE_MAPPING,
          result.code)


# ----------------------------------------------------------------------
# 八、登记处上限 / 清理
# ----------------------------------------------------------------------
def test_store_limits() -> None:
    print("\n[八] 记录上限与清理")

    desktop, _, store = standard()
    snap = snapshot(desktop.window, desktop.metrics)
    ids = [
        store.create_from_capture(snap, reading(), scope="window").observation_id
        for _ in range(5)
    ]
    check("上限内全部保留", store.count() == 5, str(store.count()))
    check("最早的一条仍然有效", store.get(ids[0]) is not None)

    tiny = make_store(desktop, Clock(), max_entries=3)
    tiny_ids = [
        tiny.create_from_capture(snap, reading(), scope="window").observation_id
        for _ in range(5)
    ]
    check("超过上限后只保留最新的",
          tiny.count() == 3 and tiny.get(tiny_ids[0]) is None,
          f"count={tiny.count()}")
    check("最新一条还在", tiny.get(tiny_ids[-1]) is not None)

    store.clear()
    check("clear 后为空", store.count() == 0)
    check("clear 后查询返回不存在",
          store.validate(ids[0], "e1").code == observation.CODE_NOT_FOUND)


# ----------------------------------------------------------------------
# 九、没有真实输入面
# ----------------------------------------------------------------------
def test_no_input_surface() -> None:
    print("\n[九] 本阶段没有任何真实输入入口")

    from app.tools.registry import registry

    names = {tool.name for tool in registry.all()}
    forbidden = {
        "mouse_click", "double_click", "move_mouse",
        "type_text", "press_key", "hotkey", "scroll", "drag",
    }
    check("工具登记处没有双击/拖拽/键盘工具",
          not (names & forbidden), str(sorted(names & forbidden)))
    check("没有名为 click 的工具", registry.get("click") is None)
    check("没有名为 type_text 的工具", registry.get("type_text") is None)

    # 阶段 2.6 新增了单次点击工具，但它默认没有输入后端：prepare 阶段
    # 就会失败关闭，不会弹确认卡片，更不会产生真实鼠标事件。
    click_tool = registry.get("click_element")
    check("点击工具已登记", click_tool is not None)
    check("点击工具必须逐次确认",
          click_tool is not None and click_tool.requires_confirmation is True)
    check("点击工具禁止自动重试",
          click_tool is not None and click_tool.retry_safe is False)
    check("点击工具默认没有输入后端",
          click_tool is not None and getattr(click_tool, "_backend", None) is None)
    check("点击工具只接受观察编号和元素编号",
          click_tool is not None
          and set(click_tool.parameters["properties"]) == {
              "observation_id", "element_id",
          },
          str(sorted(click_tool.parameters["properties"])) if click_tool else "")

    # 独立校验入口只返回结论，不执行动作
    desktop, _, store = standard()
    rec = store.create_from_capture(
        snapshot(desktop.window, desktop.metrics), reading(), scope="window"
    )
    result = observation.validate_click_target(
        rec.observation_id, "e1", registry=store
    )
    check("独立的 validate_click_target 可用", result.ok is True, result.reason)
    check("校验入口只返回坐标结论",
          result.screen_x == 500 and result.screen_y == 350,
          f"{result.screen_x},{result.screen_y}")
    check("校验过程只读窗口和度量",
          desktop.window_calls == 1 and desktop.metrics_calls == 1,
          f"{desktop.window_calls}/{desktop.metrics_calls}")


# ----------------------------------------------------------------------
def main() -> int:
    print("=" * 70)
    print("可信观察登记 + 坐标校验测试（只读，不产生任何桌面输入）")
    print("=" * 70)
    test_normal()
    test_missing_and_forged()
    test_expiry_and_invalidation()
    test_window_changes()
    test_environment_changes()
    test_coordinates()
    test_scope_and_mapping()
    test_store_limits()
    test_no_input_surface()

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 70)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for note, _, detail in failed:
        print(f"  [失败] {note}" + (f"　{detail}" if detail else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
