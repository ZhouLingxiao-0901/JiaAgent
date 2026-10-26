"""单次点击链路测试（模拟后端，不产生任何真实输入）。

全程离线：
  - 假窗口、假屏幕度量、假时钟
  - 真实 gate.authorize_tool()，但确认由测试协程代为点击
  - FakeInputBackend 只把"聚焦"和"点击"记在内存里
  - 不调用截图、不联网、不碰真实鼠标键盘

覆盖：
  - 正常授权后的模拟点击（坐标由服务端从观察记录算出）
  - 用户拒绝授权
  - 伪造/不存在的观察记录、不存在的元素、模型伪造坐标
  - 窗口移动、关闭、无法聚焦、前台被抢走
  - DPI / 映射条件变化
  - UAC / 安全桌面
  - 输入后端不可用
  - 结果未知时不自动重试
  - 工具没有真实输入入口

用法：
    .venv\\Scripts\\python.exe scripts\\test_click_tool.py
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from app.agent import gate, pending  # noqa: E402
from app.input import backend as backend_mod  # noqa: E402
from app.input import fake as fake_mod  # noqa: E402
from app.input.control import EmergencyStop  # noqa: E402
from app.input.fake import FakeInputBackend  # noqa: E402
from app.tools.base import ToolError  # noqa: E402
from app.tools.click_tools import ClickElementTool  # noqa: E402
from app.vision import observation  # noqa: E402
from app.vision.analyze import ScreenReading, UiElement  # noqa: E402
from app.vision.model import ScreenMetrics, Snapshot, WindowInfo  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
SESSION = "test-click-tool"
ANSWERED: set[str] = set()


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
    """只有测试指定的窗口和度量，绝不访问真实系统。"""

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
    awareness: str = "per_monitor",
) -> ScreenMetrics:
    return ScreenMetrics(
        primary_width=1920,
        primary_height=1080,
        virtual_left=0,
        virtual_top=0,
        virtual_width=1920,
        virtual_height=1080,
        monitor_count=1,
        dpi=dpi_value,
        scale=scale,
        awareness=awareness,
        awareness_method="test",
        consistent=True,
    )


def snapshot(window: WindowInfo | None, screen: ScreenMetrics) -> Snapshot:
    image = Image.new("RGB", (800, 600), (20, 20, 20))
    preview = image.resize((400, 300))
    return Snapshot(
        image=image,
        preview=preview,
        metrics=screen,
        window=window,
        taken_at="2026-10-21T12:00:00",
        original_size=(800, 600),
        preview_size=(400, 300),
    )


def reading(elements: list[UiElement] | None = None) -> ScreenReading:
    return ScreenReading(
        description="测试界面",
        elements=list(elements or [UiElement(
            name="登录", kind="button", x=0.5, y=0.5,
            width=0.2, height=0.1, confidence=0.9,
        )]),
        provider="fake",
        model="fake-vision",
        question="登录按钮在哪",
        scope="window",
    )


def make_store(desktop: FakeDesktop, clock: Clock) -> observation.ObservationStore:
    return observation.ObservationStore(
        ttl_seconds=30,
        clock=clock,
        wall_clock=lambda: datetime(2026, 10, 21, 12, 0, 0),
        window_lookup=desktop.lookup_window,
        metrics_lookup=desktop.lookup_metrics,
    )


def standard() -> tuple[FakeDesktop, Clock, observation.ObservationStore]:
    desktop = FakeDesktop(win_info(), metrics())
    clock = Clock()
    return desktop, clock, make_store(desktop, clock)


def make_record(
    store: observation.ObservationStore,
    desktop: FakeDesktop,
    elements: list[UiElement] | None = None,
):
    return store.create_from_capture(
        snapshot(desktop.window, desktop.metrics),
        reading(elements),
        scope="window",
    )


def make_tool(
    store: observation.ObservationStore,
    desktop: FakeDesktop,
    **backend_kwargs,
) -> tuple[ClickElementTool, FakeInputBackend]:
    backend = FakeInputBackend(desktop.lookup_window, **backend_kwargs)
    # 急停状态用独立实例：不读也不写全局的持久化文件，测试互不影响。
    tool = ClickElementTool(
        store=store, backend=backend, emergency=EmergencyStop(path=None),
    )
    return tool, backend


# ----------------------------------------------------------------------
# 驱动真实闸门
# ----------------------------------------------------------------------
async def authorize_and_run(
    tool: ClickElementTool,
    args: dict,
    *,
    approve: bool = True,
    answer: bool = True,
    timeout: float = 1.0,
):
    outcome = gate.GateOutcome()
    events: list[dict] = []

    async def answer_when_asked() -> None:
        while True:
            await asyncio.sleep(0.005)
            for action in pending.store.pending_for(SESSION):
                if action.id in ANSWERED:
                    continue
                ANSWERED.add(action.id)
                pending.store.answer(action.id, approve)
                return

    answerer = asyncio.create_task(answer_when_asked()) if answer else None
    try:
        async for event in gate.authorize_tool(
            tool, args, session_id=SESSION, outcome=outcome, timeout=timeout
        ):
            events.append(event)
    finally:
        if answerer is not None:
            answerer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await answerer

    result = None
    if outcome.proceed:
        result = await tool.execute(args, injected=outcome.injected)
    return outcome, events, result


def confirm_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "need_confirm"]


# ----------------------------------------------------------------------
# 一、正常授权 + 模拟点击
# ----------------------------------------------------------------------
async def test_normal() -> None:
    print("\n[一] 正常授权后的模拟点击")
    desktop, _, store = standard()
    rec = make_record(store, desktop)
    tool, backend = make_tool(store, desktop)

    check("工具要求用户确认", tool.requires_confirmation is True)
    check("工具禁止自动重试", tool.retry_safe is False)
    check(
        "模型只能提交 observation_id 和 element_id",
        set(tool.parameters["properties"]) == {"observation_id", "element_id"},
        str(sorted(tool.parameters["properties"])),
    )

    args = {"observation_id": rec.observation_id, "element_id": "e1"}
    outcome, events, result = await authorize_and_run(tool, args)

    check("闸门放行", outcome.proceed is True, outcome.result_text)
    confirms = confirm_events(events)
    check("弹出了确认卡片", len(confirms) == 1, f"{len(confirms)} 个")
    summary = outcome.summary
    check("卡片写明了目标元素", "登录" in summary, summary)
    check("卡片写明了目标窗口", "记事本" in summary, summary)
    check("卡片写明了目标应用/进程", "notepad.exe" in summary, summary)
    check("卡片注明是模拟点击", "模拟点击" in summary, summary)
    check("卡片注明没有真实鼠标输入", "不会产生真实鼠标输入" in summary, summary)

    check("工具执行成功", result is not None and result.ok is True,
          result.as_text() if result else "没有执行")
    check("结果状态是 succeeded",
          result is not None and result.outcome == "succeeded",
          getattr(result, "outcome", ""))
    check("后端确实模拟聚焦了一次", len(backend.focus_calls) == 1,
          str(backend.focus_calls))
    check("后端确实记录了一次点击", len(backend.clicks) == 1,
          str(backend.clicks))
    if backend.clicks:
        click = backend.clicks[0]
        check("点击的是目标窗口", click["hwnd"] == 7, str(click["hwnd"]))
        check("点击坐标由服务端从记录映射",
              (click["screen_x"], click["screen_y"]) == (500, 350),
              f"{click['screen_x']},{click['screen_y']}")
        check("点击被标记为模拟", click["simulated"] is True)
    check("输出明确说是模拟", "模拟" in result.output, result.output)
    check("输出明确说没有真实输入",
          "没有发送任何真实鼠标输入" in result.output, result.output)
    check("落库摘要是安全摘要",
          "模拟点击" in result.as_persist_text()
          and "记事本" in result.as_persist_text(),
          result.as_persist_text())
    check("点击后旧观察记录被主动失效",
          store.get(rec.observation_id).status == observation.STATUS_INVALIDATED,
          store.get(rec.observation_id).status)


# ----------------------------------------------------------------------
# 二、用户拒绝
# ----------------------------------------------------------------------
async def test_denied() -> None:
    print("\n[二] 用户拒绝授权")
    desktop, _, store = standard()
    rec = make_record(store, desktop)
    tool, backend = make_tool(store, desktop)

    args = {"observation_id": rec.observation_id, "element_id": "e1"}
    outcome, events, result = await authorize_and_run(tool, args, approve=False)

    check("闸门没有放行", outcome.proceed is False)
    check("工具没有被调用", result is None)
    check("没有弹出多余卡片", len(confirm_events(events)) == 1)
    check("后端没有聚焦", backend.focus_calls == [])
    check("后端没有点击", backend.clicks == [])
    check("告诉用户没有执行", "没有允许" in outcome.result_text,
          outcome.result_text)
    check("观察记录仍然保留（没有发生任何操作）",
          store.get(rec.observation_id).status == observation.STATUS_VALID)


# ----------------------------------------------------------------------
# 二点五、没有答复 / 记录过期 / 主动失效
# ----------------------------------------------------------------------
async def test_expired_and_no_answer() -> None:
    print("\n[二点五] 用户没有答复、观察记录过期或主动失效")

    # 用户没有点允许，也没有点拒绝：超时按"没同意"处理
    desktop, _, store = standard()
    rec = make_record(store, desktop)
    tool, backend = make_tool(store, desktop)
    outcome, events, result = await authorize_and_run(
        tool, {"observation_id": rec.observation_id, "element_id": "e1"},
        answer=False, timeout=0.05,
    )
    check("没人答复时不执行", outcome.proceed is False)
    check("确认卡片曾经弹出过", len(confirm_events(events)) == 1)
    check("没有执行工具", result is None)
    check("没有聚焦或点击",
          backend.focus_calls == [] and backend.clicks == [])
    check("告诉用户没有得到允许", "没有允许" in outcome.result_text,
          outcome.result_text)

    # 记录过期
    desktop2, clock2, store2 = standard()
    rec2 = make_record(store2, desktop2)
    tool2, backend2 = make_tool(store2, desktop2)
    clock2.advance(31)
    outcome2, events2, _ = await authorize_and_run(
        tool2, {"observation_id": rec2.observation_id, "element_id": "e1"}
    )
    check("过期记录不弹确认卡片", outcome2.proceed is False)
    check("没有弹确认卡片", confirm_events(events2) == [])
    check("原因说明记录已过期", "过期" in outcome2.result_output,
          outcome2.result_output)
    check("过期记录没有聚焦或点击",
          backend2.focus_calls == [] and backend2.clicks == [])

    # 记录被主动失效
    desktop3, _, store3 = standard()
    rec3 = make_record(store3, desktop3)
    tool3, backend3 = make_tool(store3, desktop3)
    store3.invalidate(rec3.observation_id, "测试主动失效")
    outcome3, events3, _ = await authorize_and_run(
        tool3, {"observation_id": rec3.observation_id, "element_id": "e1"}
    )
    check("失效记录不弹确认卡片", outcome3.proceed is False)
    check("没有弹确认卡片", confirm_events(events3) == [])
    check("原因说明记录已失效", "失效" in outcome3.result_output,
          outcome3.result_output)
    check("失效记录没有聚焦或点击",
          backend3.focus_calls == [] and backend3.clicks == [])


# ----------------------------------------------------------------------
# 三、伪造记录 / 元素 / 坐标
# ----------------------------------------------------------------------
async def test_forged() -> None:
    print("\n[三] 伪造观察记录、元素或坐标")
    desktop, _, store = standard()
    rec = make_record(store, desktop)
    tool, backend = make_tool(store, desktop)

    outcome, events, result = await authorize_and_run(
        tool, {"observation_id": "obs_forged", "element_id": "e1"}
    )
    check("伪造的观察编号被拒绝", outcome.proceed is False)
    check("没有弹确认卡片", confirm_events(events) == [])
    check("原因说明记录不存在", "不存在" in outcome.result_output,
          outcome.result_output)
    check("后端没有做任何事",
          backend.focus_calls == [] and backend.clicks == [])

    outcome2, events2, _ = await authorize_and_run(
        tool, {"observation_id": rec.observation_id, "element_id": "e9"}
    )
    check("不存在的元素被拒绝", outcome2.proceed is False)
    check("没有弹确认卡片", confirm_events(events2) == [])
    check("列出可用元素编号", "e1" in outcome2.result_output,
          outcome2.result_output)

    forged_args = {
        "observation_id": rec.observation_id,
        "element_id": "e1",
        "x": 0.9,
        "y": 0.9,
    }
    try:
        tool.validate(forged_args)
        rejected = False
        detail = "居然通过了"
    except ToolError as exc:
        rejected = True
        detail = str(exc)
    check("模型自己塞坐标会被参数校验拒绝", rejected, detail)

    direct = await tool.execute(forged_args)
    check("直接执行伪造坐标失败", direct.ok is False, direct.error)
    check("失败原因是多出来的参数",
          "不认识的参数" in direct.error, direct.error)
    check("后端仍然没有点击", backend.clicks == [])

    direct2 = await tool.execute(
        {"observation_id": rec.observation_id, "element_id": "e1"}
    )
    check("没有经过确认前固定目标的直接执行被拒绝",
          direct2.ok is False and "没有经过确认" in direct2.error,
          direct2.error)
    check("后端仍然没有点击", backend.clicks == [])


# ----------------------------------------------------------------------
# 四、窗口移动 / 关闭 / 无法聚焦
# ----------------------------------------------------------------------
async def test_window_problems() -> None:
    print("\n[四] 窗口移动、关闭、无法聚焦")
    desktop, _, store = standard()
    rec = make_record(store, desktop)
    tool, backend = make_tool(store, desktop)

    # 确认之前窗口已经移动
    desktop.window = win_info(rect=(120, 50, 920, 650))
    outcome, events, _ = await authorize_and_run(
        tool, {"observation_id": rec.observation_id, "element_id": "e1"}
    )
    check("窗口移动后不再弹确认卡片", outcome.proceed is False)
    check("没有弹确认卡片", confirm_events(events) == [])
    check("原因说明窗口位置变了", "位置" in outcome.result_output,
          outcome.result_output)
    check("没有聚焦或点击",
          backend.focus_calls == [] and backend.clicks == [])

    # 确认之前窗口已经关闭
    desktop2, _, store2 = standard()
    rec2 = make_record(store2, desktop2)
    tool2, backend2 = make_tool(store2, desktop2)
    desktop2.window = None
    outcome2, _, _ = await authorize_and_run(
        tool2, {"observation_id": rec2.observation_id, "element_id": "e1"}
    )
    check("窗口关闭后不再弹确认卡片", outcome2.proceed is False)
    check("原因说明窗口已关闭", "关闭" in outcome2.result_output,
          outcome2.result_output)
    check("没有聚焦或点击",
          backend2.focus_calls == [] and backend2.clicks == [])

    # 确认之前窗口已经最小化
    desktop2b, _, store2b = standard()
    rec2b = make_record(store2b, desktop2b)
    tool2b, backend2b = make_tool(store2b, desktop2b)
    desktop2b.window = win_info(minimized=True)
    outcome2b, events2b, _ = await authorize_and_run(
        tool2b, {"observation_id": rec2b.observation_id, "element_id": "e1"}
    )
    check("窗口最小化后不再弹确认卡片", outcome2b.proceed is False)
    check("没有弹确认卡片", confirm_events(events2b) == [])
    check("原因说明窗口已最小化", "最小化" in outcome2b.result_output,
          outcome2b.result_output)
    check("最小化时没有聚焦或点击",
          backend2b.focus_calls == [] and backend2b.clicks == [])

    # 聚焦失败
    desktop3, _, store3 = standard()
    rec3 = make_record(store3, desktop3)
    tool3, backend3 = make_tool(store3, desktop3, focus_mode="fail")
    outcome3, _, result3 = await authorize_and_run(
        tool3, {"observation_id": rec3.observation_id, "element_id": "e1"}
    )
    check("聚焦失败时闸门仍然放行了（用户已同意）", outcome3.proceed is True)
    check("聚焦失败后工具失败", result3 is not None and result3.ok is False,
          result3.error if result3 else "")
    check("失败原因说明无法聚焦", "无法聚焦" in result3.error, result3.error)
    check("聚焦失败没有点击", backend3.clicks == [])

    # 聚焦过程中窗口移动
    desktop4, _, store4 = standard()
    rec4 = make_record(store4, desktop4)

    def move_after_focus(_backend: FakeInputBackend) -> None:
        desktop4.window = win_info(rect=(110, 50, 910, 650))

    tool4, backend4 = make_tool(store4, desktop4, on_focus=move_after_focus)
    outcome4, _, result4 = await authorize_and_run(
        tool4, {"observation_id": rec4.observation_id, "element_id": "e1"}
    )
    check("聚焦后复核能发现窗口移动", result4 is not None and result4.ok is False,
          result4.error if result4 else "")
    check("失败原因来自复核", "聚焦后复核失败" in result4.error, result4.error)
    check("窗口移动后绝不点击", backend4.clicks == [])

    # 聚焦过程中窗口关闭
    desktop5, _, store5 = standard()
    rec5 = make_record(store5, desktop5)

    def close_after_focus(_backend: FakeInputBackend) -> None:
        desktop5.window = None

    tool5, backend5 = make_tool(store5, desktop5, on_focus=close_after_focus)
    _, _, result5 = await authorize_and_run(
        tool5, {"observation_id": rec5.observation_id, "element_id": "e1"}
    )
    check("聚焦后窗口关闭会被拦住", result5 is not None and result5.ok is False,
          result5.error if result5 else "")
    check("窗口关闭后绝不点击", backend5.clicks == [])


# ----------------------------------------------------------------------
# 五、前台窗口身份不匹配
# ----------------------------------------------------------------------
async def test_foreground_mismatch() -> None:
    print("\n[五] 前台窗口身份不匹配")
    desktop, _, store = standard()
    rec = make_record(store, desktop)
    tool, backend = make_tool(store, desktop, focus_mode="mismatch")

    _, _, result = await authorize_and_run(
        tool, {"observation_id": rec.observation_id, "element_id": "e1"}
    )
    check("前台不匹配时工具失败", result is not None and result.ok is False,
          result.error if result else "")
    check("原因说明前台不是目标窗口", "前台" in result.error, result.error)
    check("前台不匹配绝不点击", backend.clicks == [])


# ----------------------------------------------------------------------
# 六、DPI / 映射条件变化
# ----------------------------------------------------------------------
async def test_environment_change() -> None:
    print("\n[六] DPI / 坐标映射条件变化")
    desktop, _, store = standard()
    rec = make_record(store, desktop)
    tool, backend = make_tool(store, desktop)

    # 确认之前 DPI 已经变了
    desktop.metrics = metrics(dpi_value=144, scale=1.5)
    outcome, events, _ = await authorize_and_run(
        tool, {"observation_id": rec.observation_id, "element_id": "e1"}
    )
    check("DPI 变化后不再弹确认卡片", outcome.proceed is False)
    check("没有弹确认卡片", confirm_events(events) == [])
    check("原因说明 DPI/度量变化", "DPI" in outcome.result_output,
          outcome.result_output)
    check("没有聚焦或点击",
          backend.focus_calls == [] and backend.clicks == [])

    # 聚焦过程中 DPI 变化
    desktop2, _, store2 = standard()
    rec2 = make_record(store2, desktop2)

    def change_dpi(_backend: FakeInputBackend) -> None:
        desktop2.metrics = metrics(dpi_value=144, scale=1.5)

    tool2, backend2 = make_tool(store2, desktop2, on_focus=change_dpi)
    _, _, result2 = await authorize_and_run(
        tool2, {"observation_id": rec2.observation_id, "element_id": "e1"}
    )
    check("聚焦后复核能发现 DPI 变化",
          result2 is not None and result2.ok is False, result2.error if result2 else "")
    check("失败原因来自复核",
          "聚焦后复核失败" in result2.error, result2.error)
    check("DPI 变化后绝不点击", backend2.clicks == [])


# ----------------------------------------------------------------------
# 七、UAC / 安全桌面
# ----------------------------------------------------------------------
async def test_secure_desktop() -> None:
    print("\n[七] UAC / 安全桌面")
    desktop, _, store = standard()
    uac_window = win_info(
        title="用户账户控制",
        process="consent.exe",
        class_name="#32770",
    )
    desktop.window = uac_window
    rec = make_record(store, desktop)
    tool, backend = make_tool(store, desktop)

    outcome, events, _ = await authorize_and_run(
        tool, {"observation_id": rec.observation_id, "element_id": "e1"}
    )
    check("UAC 窗口在确认前被拒绝", outcome.proceed is False)
    check("UAC 窗口不弹确认卡片", confirm_events(events) == [])
    check("原因说明是系统安全界面", "安全界面" in outcome.result_output,
          outcome.result_output)
    check("UAC 窗口没有聚焦或点击",
          backend.focus_calls == [] and backend.clicks == [])

    # 普通窗口，但当前处于安全桌面
    desktop2, _, store2 = standard()
    rec2 = make_record(store2, desktop2)
    tool2, backend2 = make_tool(store2, desktop2, secure_desktop_mode=True)
    outcome2, events2, _ = await authorize_and_run(
        tool2, {"observation_id": rec2.observation_id, "element_id": "e1"}
    )
    check("安全桌面在确认前被拒绝", outcome2.proceed is False)
    check("安全桌面不弹确认卡片", confirm_events(events2) == [])
    check("原因说明是安全桌面",
          "安全桌面" in outcome2.result_output, outcome2.result_output)
    check("没有聚焦或点击",
          backend2.focus_calls == [] and backend2.clicks == [])

    # 用户确认之后、执行之前突然进入安全桌面
    desktop3, _, store3 = standard()
    rec3 = make_record(store3, desktop3)
    tool3, backend3 = make_tool(store3, desktop3)
    args = {"observation_id": rec3.observation_id, "element_id": "e1"}
    resolution = await tool3.prepare(args)
    backend3.secure_desktop_mode = True
    direct = await tool3.execute(args, injected=resolution.injected)
    check("执行阶段发现安全桌面时失败", direct.ok is False, direct.error)
    check("原因说明是安全桌面", "安全桌面" in direct.error, direct.error)
    check("执行阶段安全桌面不点击", backend3.clicks == [])


# ----------------------------------------------------------------------
# 八、输入后端不可用
# ----------------------------------------------------------------------
async def test_backend_unavailable() -> None:
    print("\n[八] 输入后端不可用")
    desktop, _, store = standard()
    rec = make_record(store, desktop)
    args = {"observation_id": rec.observation_id, "element_id": "e1"}

    tool = ClickElementTool(store=store, backend=None)
    outcome, events, _ = await authorize_and_run(tool, args)
    check("没有后端时闸门不放行", outcome.proceed is False)
    check("没有后端时连确认卡片都不弹", confirm_events(events) == [])
    check("原因说明没有输入后端",
          "输入后端" in outcome.result_output, outcome.result_output)

    tool2, backend2 = make_tool(store, desktop, available=False,
                                available_reason="测试：后端未启用")
    outcome2, events2, _ = await authorize_and_run(tool2, args)
    check("后端不可用时闸门不放行", outcome2.proceed is False)
    check("后端不可用时不弹确认卡片", confirm_events(events2) == [])
    check("原因带上了不可用原因",
          "后端未启用" in outcome2.result_output, outcome2.result_output)
    check("后端不可用时没有聚焦或点击",
          backend2.focus_calls == [] and backend2.clicks == [])


# ----------------------------------------------------------------------
# 九、结果未知：绝不自动重试
# ----------------------------------------------------------------------
async def test_unknown() -> None:
    print("\n[九] 输入结果未知")
    desktop, _, store = standard()
    rec = make_record(store, desktop)
    tool, backend = make_tool(
        store, desktop, click_mode="unknown",
        unknown_reason="输入超时，无法确认点击是否生效",
    )
    check("工具被标记为不可重试", tool.retry_safe is False)

    _, _, result = await authorize_and_run(
        tool, {"observation_id": rec.observation_id, "element_id": "e1"}
    )
    check("结果未知时工具不算成功", result is not None and result.ok is False,
          result.error if result else "")
    check("结果状态是 unknown",
          result is not None and result.outcome == "unknown",
          getattr(result, "outcome", ""))
    check("明确告诉用户结果未知",
          "结果未知" in result.error, result.error)
    check("明确说不会自动重试",
          "不会自动重试" in result.error, result.error)
    check("请用户确认后续处理",
          "确认目标程序现在的状态" in result.error, result.error)
    check("后端只收到一次点击", len(backend.clicks) == 1,
          str(len(backend.clicks)))
    check("未知结果后旧记录被失效",
          store.get(rec.observation_id).status == observation.STATUS_INVALIDATED,
          store.get(rec.observation_id).status)


# ----------------------------------------------------------------------
# 九点五、输入后端明确失败
# ----------------------------------------------------------------------
async def test_failed() -> None:
    print("\n[九点五] 输入后端明确失败")
    desktop, _, store = standard()
    rec = make_record(store, desktop)
    tool, backend = make_tool(
        store, desktop, click_mode="fail",
        click_reason="模拟：目标控件已禁用，点击没有生效",
    )

    _, _, result = await authorize_and_run(
        tool, {"observation_id": rec.observation_id, "element_id": "e1"}
    )
    check("明确失败时工具不算成功",
          result is not None and result.ok is False,
          result.error if result else "")
    check("结果状态是 failed",
          result is not None and result.outcome == "failed",
          getattr(result, "outcome", ""))
    check("失败原因来自后端",
          "目标控件已禁用" in result.error, result.error)
    check("明确失败不会被说成结果未知",
          "结果未知" not in result.error, result.error)
    check("后端确实尝试了一次", len(backend.clicks) == 1,
          str(len(backend.clicks)))
    check("失败后旧记录被失效",
          store.get(rec.observation_id).status == observation.STATUS_INVALIDATED,
          store.get(rec.observation_id).status)


# ----------------------------------------------------------------------
# 十、真实输入入口默认关闭
# ----------------------------------------------------------------------
async def test_no_real_input() -> None:
    print("\n[十] 真实输入入口默认关闭；本测试只走模拟后端")

    # 只看 AST 里的真实调用和 import，不看注释/文档字符串——
    # 文档里写"不会调用 SendInput"是说明，不是输入调用。
    forbidden_calls = {
        "SendInput", "mouse_event", "keybd_event", "SetCursorPos",
        "MoveCursor", "click", "write", "press", "hotkey",
    }
    forbidden_modules = {
        "pyautogui", "pydirectinput", "pynput", "win32api", "win32con",
        "ctypes.windll.user32",
    }
    found_calls: set[str] = set()
    found_modules: set[str] = set()
    for source in (
        inspect.getsource(fake_mod),
        inspect.getsource(backend_mod),
        inspect.getsource(ClickElementTool),
    ):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    found_calls.add(func.id)
                elif isinstance(func, ast.Attribute):
                    found_calls.add(func.attr)
            elif isinstance(node, ast.Import):
                found_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found_modules.add(node.module)

    # ClickElementTool.click 是后端方法名，不是真实输入 API；它出现在
    # 方法调用里是设计本身，所以从禁用集合中去掉，只检查真正的系统调用名。
    real_forbidden = forbidden_calls - {"click"}
    check("输入相关源码没有系统输入调用",
          not (found_calls & real_forbidden), str(sorted(found_calls & real_forbidden)))
    check("输入相关源码没有引入自动化输入库",
          not (found_modules & forbidden_modules),
          str(sorted(found_modules & forbidden_modules)))

    from app.tools.registry import registry

    tool = registry.get("click_element")
    check("点击工具已登记", tool is not None)
    check("登记处里的点击工具要求确认",
          tool is not None and tool.requires_confirmation is True)
    check("登记处里的点击工具禁止重试",
          tool is not None and tool.retry_safe is False)
    check("登记处没有双击/拖拽/键盘工具",
          registry.get("double_click") is None
          and registry.get("drag") is None
          and registry.get("type_text") is None
          and registry.get("press_key") is None)

    # 生产登记的工具默认没有后端：拿一条不存在的记录，prepare 也不会
    # 走到"执行"，更不会启用任何真实输入。
    check("生产点击工具默认没有真实后端",
          isinstance(tool, ClickElementTool) and tool._backend is None)


# ----------------------------------------------------------------------
async def main_async() -> int:
    await test_normal()
    await test_denied()
    await test_expired_and_no_answer()
    await test_forged()
    await test_window_problems()
    await test_foreground_mismatch()
    await test_environment_change()
    await test_secure_desktop()
    await test_backend_unavailable()
    await test_unknown()
    await test_failed()
    await test_no_real_input()
    return 0


def main() -> int:
    print("=" * 72)
    print("单次点击链路测试（模拟后端，不产生任何真实输入）")
    print("=" * 72)
    asyncio.run(main_async())

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 72)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for note, _, detail in failed:
        print(f"  [失败] {note}" + (f"　{detail}" if detail else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
