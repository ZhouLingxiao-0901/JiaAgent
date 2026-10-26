"""工作流引擎测试（自主工作流引擎 · 阶段 3）。

**全部用假模型和假工具**，不联网、不花钱、不启动任何程序、不碰主数据库。

这里守的是几件"看起来像成功但其实是假的"的事：
  - 用户没批准计划  -> 一步都不许执行
  - 用户拒绝了某步  -> 那一步的工具**一次都不能被调用**
  - 工具真的失败了  -> 不许被当成成功往下走
  - 拆不出计划      -> 老实报失败，不许假装任务已经完成
  - 暂停/取消       -> 后面的步骤不许偷偷继续跑

用法：
  .venv\\Scripts\\python.exe scripts\\test_workflow_engine.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm.base import LLMAdapter, Message  # noqa: E402
from app.tools.base import Tool, ToolResult  # noqa: E402
from app.workflow import store  # noqa: E402
from app.workflow.engine import WorkflowEngine  # noqa: E402
from app.workflow.manager import manager  # noqa: E402
from app.workflow.model import StepStatus, TaskStatus  # noqa: E402
from app.workflow.planner import Planner  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
SESSION = "test-engine-session"


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, ok, detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


# ----------------------------------------------------------------------
# 假模型：按提示词内容判断这是在问哪件事
# ----------------------------------------------------------------------


class FakeLLM(LLMAdapter):
    def __init__(self, *, plan: Any = None, decide: Any = None, verify: Any = None,
                 compose: str = "这是给你的一句话汇报。") -> None:
        super().__init__(provider_name="fake", model="fake", api_key="x", base_url="x")
        self.plan_reply = plan
        self.decide_reply = decide if decide is not None else {"action": "continue"}
        self.verify_reply = verify if verify is not None else {"ok": True, "summary": "做完了"}
        self.compose_reply = compose
        self.decide_queue: list[Any] = []
        self.calls: list[str] = []

    @staticmethod
    def _as_text(value: Any) -> str:
        if value is None:
            return "{}"
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    async def chat(self, messages: list[Message], **options: Any) -> str:
        # **把 system 也拼进来**——真实的模型 system 和 user 都看得到。
        # 校验那件事的识别标志恰恰在 system 提示词里（"判断目标有没有达成"），
        # 只看 user 的话会漏掉，校验调用就掉进兜底分支返回 {}，
        # 表现成"校验悄悄失败了、任务被判失败"，极难查。
        text = " ".join(str(m.get("content", "")) for m in messages)
        self.calls.append(text)
        if "拆成有顺序的执行步骤" in text:
            return self._as_text(self.plan_reply)
        if "决定接下来怎么办" in text:
            reply = self.decide_queue.pop(0) if self.decide_queue else self.decide_reply
            return self._as_text(reply)
        if "判断目标有没有达成" in text:
            return self._as_text(self.verify_reply)
        if "现在请你完成这一步" in text:
            return self.compose_reply
        return "{}"

    async def stream_chat(self, messages, **options):
        for piece in ():
            yield piece

    async def stream_turn(self, messages, tools=None, **options):
        for event in ():
            yield event


# ----------------------------------------------------------------------
# 假工具
# ----------------------------------------------------------------------


class FakeTool(Tool):
    """按预设行为干活的假工具，并把每次调用记下来。"""

    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name: str, *, ok: bool = True, error: str = "",
                 fail_times: int = 0, confirm: bool = False,
                 retry_safe: bool = True,
                 emergency_reason: str = "",
                 block: asyncio.Event | None = None) -> None:
        self.name = name
        self.label = name
        self.description = "测试用"
        self.requires_confirmation = confirm
        self.retry_safe = retry_safe
        self.ok = ok
        self.error = error
        self.fail_times = fail_times
        self.emergency_reason = emergency_reason
        self.block = block
        self.calls: list[dict[str, Any]] = []
        self.started = asyncio.Event()

    def emergency_block_reason(self) -> str:
        """模拟受急停约束的工具；空串表示没有急停。"""
        return self.emergency_reason

    async def run(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        if len(self.calls) <= self.fail_times:
            return ToolResult(ok=False, error=self.error)
        if self.ok:
            return ToolResult(ok=True, output=f"{self.name} 做好了")
        return ToolResult(ok=False, error=self.error)


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------


async def wait_until(predicate, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _catalog(tools: dict[str, FakeTool]) -> list[dict[str, Any]]:
    """把假工具拼成 registry.openai_tools() 的形状，喂给规划器。"""
    return [
        {"type": "function", "function": {
            "name": name, "description": "测试用", "parameters": FakeTool.parameters,
        }}
        for name in tools
    ]


def build(db: Path, adapter: FakeLLM, tools: dict[str, FakeTool],
          **kwargs: Any) -> tuple[WorkflowEngine, list[tuple[str, str]]]:
    plan_kwargs: dict[str, Any] = {}
    for key in ("max_replans", "decide_mode"):
        if key in kwargs:
            plan_kwargs[key] = kwargs.pop(key)
    # 规划器必须知道"有哪些工具"。给空表的话，它会把**所有**工具名都当成编造的、
    # 一律降级成纯说明步骤——那样测试就根本测不到工具执行了。
    planner = Planner(adapter, tools=_catalog(tools), **plan_kwargs)
    published: list[tuple[str, str]] = []
    engine = WorkflowEngine(
        kwargs.pop("task_id"),
        planner=planner,
        tool_lookup=tools.get,
        db_path=db,
        publisher=lambda role, text: published.append((role, text)),
        **kwargs,
    )
    return engine, published


def two_step_plan() -> dict[str, Any]:
    return {"title": "两步任务", "steps": [
        {"goal": "第一步", "tool": "tool_a", "arguments": {}},
        {"goal": "第二步", "tool": "tool_b", "arguments": {}},
    ]}


async def auto_answer_choice(option_id: str | None, stop: asyncio.Event) -> None:
    """后台帮用户答"选择题"。option_id 为 None 就故意不答（等超时）。"""
    while not stop.is_set():
        await asyncio.sleep(0.005)
        if option_id is None:
            continue
        for action in manager_choices():
            manager.choices_done.add(action.id)
            manager.raw_choose(action.id, option_id)
            return


class _ChoiceHelper:
    """小包装，免得在测试里到处 import pending。"""

    def __init__(self) -> None:
        from app.agent import pending

        self.pending = pending
        self.choices_done: set[str] = set()

    def manager_choices(self):
        return self.pending.store.pending_for(SESSION)

    def raw_choose(self, action_id: str, option_id: str) -> None:
        self.pending.store.choose(action_id, option_id)


manager_choices = None  # 在 main 里赋值


# ----------------------------------------------------------------------
# 各个用例
# ----------------------------------------------------------------------


async def case_normal(db: Path) -> None:
    print()
    print("正常跑完：")
    tools = {"tool_a": FakeTool("tool_a"), "tool_b": FakeTool("tool_b")}
    adapter = FakeLLM(plan=two_step_plan(),
                      verify={"ok": True, "summary": "两步都做完了", "missing": ""})
    task = store.create_task(SESSION, "做个两步的事", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id)
    await engine.run(skip_approval=True)

    got = store.get_task(task.id, db_path=db)
    check("任务最终成功", got.status is TaskStatus.SUCCEEDED, got.status.value)
    check("总结来自校验", got.summary == "两步都做完了", got.summary)
    check("两步都成功了",
          [s.status for s in got.steps] == [StepStatus.SUCCEEDED, StepStatus.SUCCEEDED])
    check("两个工具各被调用一次",
          len(tools["tool_a"].calls) == 1 and len(tools["tool_b"].calls) == 1)
    check("步骤结果写进了数据库", got.steps[0].result == "tool_a 做好了")
    assistant_texts = [t for r, t in published if r == "assistant"]
    step_texts = [t for r, t in published if r == "tool"]
    check("里程碑用 assistant 角色（会进模型上下文）", any("拆成了 2 步" in t for t in assistant_texts))
    check("步骤执行用 tool 角色（不污染模型上下文）", len(step_texts) >= 2, f"{len(step_texts)} 条")
    check("总结写进了会话记录", any("两步都做完了" in t for t in assistant_texts))


async def case_not_approved(db: Path) -> None:
    print()
    print("用户没批准计划：一步都不许执行")
    tools = {"tool_a": FakeTool("tool_a"), "tool_b": FakeTool("tool_b")}
    adapter = FakeLLM(plan=two_step_plan())
    task = store.create_task(SESSION, "做个两步的事", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id, approve_timeout=0.2)
    await engine.run(skip_approval=False)

    got = store.get_task(task.id, db_path=db)
    check("任务停在待执行（不是失败，等你批准）", got.status is TaskStatus.PENDING, got.status.value)
    check("工具一次都没被调用",
          tools["tool_a"].calls == [] and tools["tool_b"].calls == [],
          f"{len(tools['tool_a'].calls)}/{len(tools['tool_b'].calls)}")
    check("计划已经拆好并入库了（批准后能接着跑）", len(got.steps) == 2)
    check("步骤还都是待执行", all(s.status is StepStatus.PENDING for s in got.steps))
    check("告诉了用户在等他", any("等你确认" in t for _, t in published))


async def case_approve_then_run(db: Path) -> None:
    print()
    print("批准之后接着跑（走 manager 的真实信号）：")
    tools = {"tool_a": FakeTool("tool_a"), "tool_b": FakeTool("tool_b")}
    adapter = FakeLLM(plan=two_step_plan(), verify={"ok": True, "summary": "好了"})
    task = store.create_task(SESSION, "做个两步的事", db_path=db)
    engine, _ = build(db, adapter, tools, task_id=task.id)   # approve_timeout=None
    job = asyncio.create_task(engine.run(skip_approval=False))

    planned = await wait_until(lambda: len(store.list_steps(task.id, db_path=db)) == 2)
    check("引擎在等批准时，计划已经拆好了", planned)
    check("这时候还没跑任何工具", tools["tool_a"].calls == [])

    await manager.approve(task.id)
    await asyncio.wait_for(job, timeout=3)

    got = store.get_task(task.id, db_path=db)
    check("批准之后跑完了", got.status is TaskStatus.SUCCEEDED, got.status.value)
    check("两个工具都跑了",
          len(tools["tool_a"].calls) == 1 and len(tools["tool_b"].calls) == 1)


async def case_plan_failed(db: Path) -> None:
    print()
    print("拆不出计划：老实报失败，不许假装完成")
    tools = {"tool_a": FakeTool("tool_a")}
    adapter = FakeLLM(plan="我觉得你说得不清楚")   # 修复也救不回来
    task = store.create_task(SESSION, "含糊的需求", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id)
    await engine.run(skip_approval=True)

    got = store.get_task(task.id, db_path=db)
    check("任务失败", got.status is TaskStatus.FAILED, got.status.value)
    check("失败原因说清了是「拆不出来」", "拆成步骤" in got.error, got.error[:40])
    check("没有留下任何步骤", got.steps == [])
    check("一个工具都没跑", tools["tool_a"].calls == [])
    check("没有编造成功的总结", "任务完成" not in " ".join(t for _, t in published))


async def case_step_denied(db: Path) -> None:
    print()
    print("某一步的授权被拒绝：那一步的工具一次都不能被调用")
    tools = {"tool_a": FakeTool("tool_a", confirm=True), "tool_b": FakeTool("tool_b")}
    adapter = FakeLLM(plan=two_step_plan())
    task = store.create_task(SESSION, "做个两步的事", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id, confirm_timeout=0.3)
    await engine.run(skip_approval=True)

    got = store.get_task(task.id, db_path=db)
    check("第一步被记为失败", got.steps[0].status is StepStatus.FAILED, got.steps[0].status.value)
    check("第一步的工具**没有被执行**", tools["tool_a"].calls == [])
    check("第一步的尝试次数只有 1（拒绝是确定的，不该重试）",
          got.steps[0].attempts == 1, f"attempts={got.steps[0].attempts}")
    check("拒绝了会有记录", any("没有执行" in t for _, t in published))


async def case_retry(db: Path) -> None:
    print()
    print("失败重试：瞬时性问题才重试")
    tools = {"tool_a": FakeTool("tool_a", fail_times=1, error="连接超时，请稍后重试"),
             "tool_b": FakeTool("tool_b")}
    adapter = FakeLLM(plan={"title": "t", "steps": [
        {"goal": "第一步", "tool": "tool_a", "arguments": {}}]},
        verify={"ok": True, "summary": "好了"})
    task = store.create_task(SESSION, "试一试", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id)
    await engine.run(skip_approval=True)

    got = store.get_task(task.id, db_path=db)
    check("第一次失败后重试成功", got.steps[0].status is StepStatus.SUCCEEDED)
    check("尝试次数是 2", got.steps[0].attempts == 2, f"attempts={got.steps[0].attempts}")
    check("工具真的被调了两次", len(tools["tool_a"].calls) == 2)
    check("重试这件事告诉了用户", any("再试一次" in t for _, t in published))

    print()
    print("确定性失败不重试：")
    tools2 = {"tool_a": FakeTool("tool_a", ok=False, error="这个位置不在允许访问的范围内")}
    task2 = store.create_task(SESSION, "试一试", db_path=db)
    engine2, _ = build(db, FakeLLM(plan={"title": "t", "steps": [
        {"goal": "写文件", "tool": "tool_a", "arguments": {}}]}),
        tools2, task_id=task2.id)
    await engine2.run(skip_approval=True)
    got2 = store.get_task(task2.id, db_path=db)
    check("路径不允许 -> 只试了 1 次", got2.steps[0].attempts == 1,
          f"attempts={got2.steps[0].attempts}")
    check("工具只被调了一次", len(tools2["tool_a"].calls) == 1)

    print()
    print("不可重复的副作用工具即使报瞬时错误也不自动重试：")
    tools3 = {"tool_a": FakeTool(
        "tool_a", fail_times=1, error="连接超时，请稍后重试", retry_safe=False,
    )}
    task3 = store.create_task(SESSION, "点一下", db_path=db)
    engine3, published3 = build(db, FakeLLM(plan={"title": "t", "steps": [
        {"goal": "点击按钮", "tool": "tool_a", "arguments": {}}]}, verify={
            "ok": True, "summary": "好了"}), tools3, task_id=task3.id)
    await engine3.run(skip_approval=True)
    got3 = store.get_task(task3.id, db_path=db)
    check("retry_safe=False -> 只尝试了 1 次", got3.steps[0].attempts == 1,
          f"attempts={got3.steps[0].attempts}")
    check("retry_safe=False -> 工具只被调用 1 次",
          len(tools3["tool_a"].calls) == 1)
    check("没有告诉用户会自动重试",
          not any("再试一次" in t for _, t in published3))


async def case_click_unknown_no_retry(db: Path) -> None:
    """真实的 ClickElementTool 报"结果未知"时，工作流只能执行一次。

    这里不是用 FakeTool 模拟 retry_safe，而是把真正的点击工具接进引擎：
    观察记录、坐标校验、闸门确认、FakeInputBackend 全部走真代码，只有
    窗口和输入是假的。错误文本故意包含"超时"（引擎本来会认为可重试），
    用来证明 retry_safe=False 确实挡住了自动再点一次。
    """
    print()
    print("真实点击工具结果未知：工作流只执行一次，绝不自动再点")

    import contextlib
    from datetime import datetime

    from PIL import Image

    from app.agent import pending
    from app.input.control import EmergencyStop
    from app.input.fake import FakeInputBackend
    from app.tools.click_tools import ClickElementTool
    from app.vision import observation
    from app.vision.analyze import ScreenReading, UiElement
    from app.vision.model import ScreenMetrics, Snapshot, WindowInfo

    window = WindowInfo(
        hwnd=7,
        title="记事本",
        pid=42,
        process="notepad.exe",
        class_name="Notepad",
        rect=(100, 50, 900, 650),
        visible=True,
        rect_source="dwm_frame",
    )
    screen = ScreenMetrics(
        primary_width=1920,
        primary_height=1080,
        virtual_left=0,
        virtual_top=0,
        virtual_width=1920,
        virtual_height=1080,
        monitor_count=1,
        dpi=96,
        scale=1.0,
        awareness="per_monitor",
        awareness_method="test",
        consistent=True,
    )

    class Desktop:
        def lookup_window(self, _hwnd: int) -> WindowInfo | None:
            return window

        def lookup_metrics(self) -> ScreenMetrics:
            return screen

    desktop = Desktop()
    obs_store = observation.ObservationStore(
        ttl_seconds=30,
        wall_clock=lambda: datetime(2026, 10, 21, 12, 0, 0),
        window_lookup=desktop.lookup_window,
        metrics_lookup=desktop.lookup_metrics,
    )
    image = Image.new("RGB", (800, 600), (20, 20, 20))
    snapshot = Snapshot(
        image=image,
        preview=image.resize((400, 300)),
        metrics=screen,
        window=window,
        taken_at="2026-10-21T12:00:00",
        original_size=(800, 600),
        preview_size=(400, 300),
    )
    reading = ScreenReading(
        description="测试界面",
        elements=[UiElement(
            name="登录", kind="button", x=0.5, y=0.5,
            width=0.2, height=0.1, confidence=0.9,
        )],
        provider="fake",
        model="fake-vision",
        question="登录按钮在哪",
        scope="window",
    )
    record = obs_store.create_from_capture(snapshot, reading, scope="window")

    backend = FakeInputBackend(
        desktop.lookup_window,
        click_mode="unknown",
        unknown_reason="输入超时，无法确认点击是否生效",
    )
    # 急停状态用独立实例：不碰全局持久化文件，也不受别的测试影响。
    tool = ClickElementTool(
        store=obs_store, backend=backend, emergency=EmergencyStop(path=None),
    )

    task = store.create_task(SESSION, "点一下登录", db_path=db)
    adapter = FakeLLM(
        plan={"title": "点一下", "steps": [{
            "goal": "点击登录按钮",
            "tool": "click_element",
            "arguments": {
                "observation_id": record.observation_id,
                "element_id": "e1",
            },
        }]},
        verify={"ok": True, "summary": "好了"},
    )
    engine, published = build(
        db, adapter, {"click_element": tool}, task_id=task.id,
    )

    answered: set[str] = set()
    stop = asyncio.Event()

    async def auto_approve() -> None:
        while not stop.is_set():
            await asyncio.sleep(0.005)
            for action in pending.store.pending_for(SESSION):
                if action.id in answered:
                    continue
                if action.tool_name != "click_element":
                    continue
                answered.add(action.id)
                pending.store.answer(action.id, True)

    approver = asyncio.create_task(auto_approve())
    try:
        await engine.run(skip_approval=True)
    finally:
        stop.set()
        approver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await approver

    got = store.get_task(task.id, db_path=db)
    step = got.steps[0]
    check("点击步骤只尝试了 1 次", step.attempts == 1, f"attempts={step.attempts}")
    check("模拟后端只收到 1 次点击", len(backend.clicks) == 1,
          f"{len(backend.clicks)} 次")
    check("步骤记录了「结果未知」", "结果未知" in (step.error or ""),
          (step.error or "")[:80])
    check("没有告诉用户会自动再点一次",
          not any("再试一次" in text for _, text in published))
    check("用户确实确认过（卡片经过闸门）",
          len(backend.focus_calls) == 1, f"{len(backend.focus_calls)} 次聚焦")


async def case_emergency_stop(db: Path) -> None:
    """急停生效时，工作流连确认卡片都不该弹，更不能执行工具。

    引擎在申请授权之前检查工具声明的急停状态，所以这里用 FakeTool 模拟
    一个"受急停约束"的工具：即使它平时需要用户确认，也不该出现候选/确认
    动作，后面的步骤同样不再执行。
    """
    print()
    print("急停生效：工作流在申请确认之前停下，不弹卡片、不执行")

    from app.agent import pending

    reason = "真实鼠标输入已被急停，恢复必须由用户在界面上显式确认。"
    tools = {
        "tool_a": FakeTool("tool_a", confirm=True, emergency_reason=reason),
        "tool_b": FakeTool("tool_b"),
    }
    adapter = FakeLLM(plan=two_step_plan())
    task = store.create_task(SESSION, "点一下", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id)

    before = [action.id for action in pending.store.pending_for(SESSION)]
    await engine.run(skip_approval=True)
    after = [action.id for action in pending.store.pending_for(SESSION)]

    got = store.get_task(task.id, db_path=db)
    check("任务整体失败", got.status is TaskStatus.FAILED, got.status.value)
    check("第一步记为失败，后续步骤跳过",
          [s.status for s in got.steps] == [StepStatus.FAILED, StepStatus.SKIPPED],
          [s.status.value for s in got.steps])
    check("失败原因来自急停", "急停" in (got.error or ""),
          (got.error or "")[:60])
    check("急停步骤一次都没尝试", got.steps[0].attempts == 0,
          f"attempts={got.steps[0].attempts}")
    check("受急停约束的工具没有被调用", tools["tool_a"].calls == [])
    check("后续步骤也没有执行", tools["tool_b"].calls == [])
    check("没有弹出任何确认/选择卡片", before == after,
          f"{len(before)} -> {len(after)}")
    check("用户能看到已停止", any("已停止" in text for _, text in published))


async def case_unknown_tool(db: Path) -> None:
    print()
    print("计划里出现不存在的工具：绝不执行")
    tools = {"tool_a": FakeTool("tool_a")}
    adapter = FakeLLM(plan={"title": "t", "steps": [
        {"goal": "用不存在的工具做坏事", "tool": "delete_everything", "arguments": {}}]})
    task = store.create_task(SESSION, "试一试", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id)
    await engine.run(skip_approval=True)

    got = store.get_task(task.id, db_path=db)
    # 规划阶段已经把它降级成"纯说明步骤"，所以这一步不会去调任何工具
    check("这一步没有去调用任何工具", tools["tool_a"].calls == [])
    check("这一步被如实处理了（要么降级、要么失败）",
          got.steps[0].tool == "" or got.steps[0].status is StepStatus.FAILED,
          f"tool={got.steps[0].tool!r} status={got.steps[0].status.value}")
    check("降级这件事告诉了用户", any("不存在的工具" in t for _, t in published))

    print()
    print("绕过规划器，手工塞一个不存在的工具：引擎自己那道防线也要挡住")
    task2 = store.create_task(SESSION, "手工塞的坏步骤", db_path=db)
    store.add_steps(task2.id, [{"goal": "用不存在的工具", "tool": "nonexistent_tool",
                                "arguments": {}}], db_path=db)
    engine2, published2 = build(db, FakeLLM(plan=two_step_plan()), tools, task_id=task2.id)
    await engine2.run(skip_approval=True)
    got2 = store.get_task(task2.id, db_path=db)
    check("该步被记为失败", got2.steps[0].status is StepStatus.FAILED,
          got2.steps[0].status.value)
    check("并说清了是没有这个工具", "没有名为" in got2.steps[0].error,
          got2.steps[0].error[:44])
    check("一个真工具都没被碰", tools["tool_a"].calls == [])


async def case_ask_user(db: Path) -> None:
    print()
    print("卡住时求助：用户选「跳过」")
    tools = {"tool_a": FakeTool("tool_a", ok=False, error="说不清的失败"),
             "tool_b": FakeTool("tool_b")}
    adapter = FakeLLM(plan=two_step_plan(), verify={"ok": True, "summary": "跳过后做完了"})
    adapter.decide_queue = [
        {"action": "ask_user", "reason": "第一步失败了", "question": "怎么办？",
         "options": [{"id": "retry", "name": "重试"}, {"id": "skip", "name": "跳过"},
                     {"id": "cancel", "name": "取消"}]},
        {"action": "continue"},
        {"action": "continue"},
    ]
    task = store.create_task(SESSION, "做个两步的事", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id, confirm_timeout=2.0)
    job = asyncio.create_task(engine.run(skip_approval=True))
    await wait_until(lambda: len(manager_choices() or []) > 0)
    for action in manager_choices():
        pending_choose_helper.raw_choose(action.id, "skip")
    await asyncio.wait_for(job, timeout=5)

    got = store.get_task(task.id, db_path=db)
    check("第一步被标为跳过", got.steps[0].status is StepStatus.SKIPPED, got.steps[0].status.value)
    check("第二步照常跑了", got.steps[1].status is StepStatus.SUCCEEDED)
    check("任务最终成功", got.status is TaskStatus.SUCCEEDED, got.status.value)
    check("跳过这件事告诉了用户", any("跳过" in t for _, t in published))

    print()
    print("卡住时求助：没人应答（超时）-> 停下来，不自己替用户拿主意")
    tools2 = {"tool_a": FakeTool("tool_a", ok=False, error="说不清的失败"),
              "tool_b": FakeTool("tool_b")}
    adapter2 = FakeLLM(plan=two_step_plan())
    adapter2.decide_queue = [
        {"action": "ask_user", "question": "怎么办？", "options": []},
    ]
    task2 = store.create_task(SESSION, "做个两步的事", db_path=db)
    engine2, _ = build(db, adapter2, tools2, task_id=task2.id, confirm_timeout=0.3)
    await engine2.run(skip_approval=True)
    got2 = store.get_task(task2.id, db_path=db)
    check("没人应答时任务被取消（不是悄悄往下做）",
          got2.status is TaskStatus.CANCELLED, got2.status.value)
    check("第二步没有被执行", tools2["tool_b"].calls == [])


async def case_pause_resume(db: Path) -> None:
    print()
    print("暂停与恢复：")
    block = asyncio.Event()
    tools = {"tool_a": FakeTool("tool_a", block=block), "tool_b": FakeTool("tool_b")}
    adapter = FakeLLM(plan=two_step_plan(), verify={"ok": True, "summary": "好了"})
    task = store.create_task(SESSION, "做个两步的事", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id)
    job = asyncio.create_task(engine.run(skip_approval=True))

    await asyncio.wait_for(tools["tool_a"].started.wait(), timeout=3)
    await manager.pause(task.id)          # 第一步正卡着，先把暂停标志立起来
    block.set()                           # 放它过去，它会在下一步边界停下来

    paused = await wait_until(
        lambda: (store.get_task(task.id, db_path=db) or task).paused, timeout=3)
    check("到达步骤边界后真的暂停了", paused)
    check("暂停时第二步还没跑", tools["tool_b"].calls == [])
    check("暂停状态独立于任务状态（仍是执行中）",
          store.get_task(task.id, db_path=db).status is TaskStatus.RUNNING)
    check("暂停这件事告诉了用户", any("已暂停" in t for _, t in published))

    await manager.resume(task.id)
    await asyncio.wait_for(job, timeout=3)
    got = store.get_task(task.id, db_path=db)
    check("恢复之后跑完了", got.status is TaskStatus.SUCCEEDED, got.status.value)
    check("暂停标志被清掉了", got.paused is False)
    check("第二步在恢复后才跑", len(tools["tool_b"].calls) == 1)


async def case_cancel(db: Path) -> None:
    print()
    print("取消：后面的步骤不许偷偷继续跑")
    block = asyncio.Event()
    tools = {"tool_a": FakeTool("tool_a", block=block), "tool_b": FakeTool("tool_b")}
    adapter = FakeLLM(plan=two_step_plan())
    task = store.create_task(SESSION, "做个两步的事", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id)
    job = asyncio.create_task(engine.run(skip_approval=True))

    await asyncio.wait_for(tools["tool_a"].started.wait(), timeout=3)
    cancelling = asyncio.create_task(manager.cancel(task.id))
    await wait_until(lambda: manager.control_of(task.id).cancel_requested, timeout=2)
    block.set()

    await asyncio.wait_for(job, timeout=5)
    await asyncio.wait_for(cancelling, timeout=5)

    got = store.get_task(task.id, db_path=db)
    check("任务被取消", got.status is TaskStatus.CANCELLED, got.status.value)
    check("第二步没有被执行", tools["tool_b"].calls == [])
    check("未执行的第二步标成已取消",
          got.steps[1].status is StepStatus.CANCELLED,
          got.steps[1].status.value)
    check("取消这件事告诉了用户", any("已取消" in t for _, t in published))


async def case_cancel_while_waiting_confirmation(db: Path) -> None:
    print()
    print("等待工具确认时取消：不许执行，也不许之后再批准")
    tools = {"tool_a": FakeTool("tool_a", confirm=True)}
    adapter = FakeLLM(plan={"title": "一步", "steps": [
        {"goal": "打开应用", "tool": "tool_a", "arguments": {}},
    ]})
    task = store.create_task(SESSION, "打开一个应用", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id)
    job = asyncio.create_task(engine.run(skip_approval=True))

    waiting = await wait_until(
        lambda: bool(manager.control_of(task.id).current_action_id), timeout=3
    )
    check("任务确实停在工具确认上", waiting)
    cancelling = asyncio.create_task(manager.cancel(task.id))
    await asyncio.wait_for(job, timeout=5)
    await asyncio.wait_for(cancelling, timeout=5)

    got = store.get_task(task.id, db_path=db)
    check("等待确认时取消，任务落到取消终态",
          got.status is TaskStatus.CANCELLED, got.status.value)
    check("等待确认时取消，工具一次都没执行", tools["tool_a"].calls == [])
    check("等待确认的步骤标成已取消",
          got.steps[0].status is StepStatus.CANCELLED,
          got.steps[0].status.value)
    check("取消没有写成成功",
          not any("任务完成" in t for _, t in published))


async def case_cancel_at_final_step(db: Path) -> None:
    print()
    print("最后一步执行期间取消：不允许悄悄变成成功")
    task = store.create_task(SESSION, "做最后一步", db_path=db)

    class CancelAfterRunTool(FakeTool):
        async def run(self, **kwargs):
            result = await super().run(**kwargs)
            # 工具已经产生了副作用，然后取消才到达。
            manager.control_of(task.id).cancel_requested = True
            return result

    tools = {"tool_a": CancelAfterRunTool("tool_a")}
    adapter = FakeLLM(
        plan={"title": "一步", "steps": [
            {"goal": "做一步", "tool": "tool_a", "arguments": {}},
        ]},
        verify={"ok": True, "summary": "做完了"},
    )
    engine, published = build(db, adapter, tools, task_id=task.id)
    await engine.run(skip_approval=True)

    got = store.get_task(task.id, db_path=db)
    check("最后一步后收到取消，任务仍是取消而不是成功",
          got.status is TaskStatus.CANCELLED, got.status.value)
    check("最后一步确实执行过一次（底层副作用已发生）",
          len(tools["tool_a"].calls) == 1)
    check("没有写成功总结", not any("任务完成" in t for _, t in published))
    check("取消终态没有被后续校验覆盖",
          got.summary == "" and "取消" in got.error, got.error)


async def case_forced_cancel_while_tool_running(db: Path) -> None:
    print()
    print("工具不响应协作式取消：管家强制掐掉后，数据库不能停在执行中")
    block = asyncio.Event()
    tools = {"tool_a": FakeTool("tool_a", block=block)}
    adapter = FakeLLM(plan={"title": "一步", "steps": [
        {"goal": "卡住一步", "tool": "tool_a", "arguments": {}},
    ]})
    task = store.create_task(SESSION, "卡住一步", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id)
    # 必须走管家的生产路径，强制掐断才是真的（直接 create_task 管家不知道它）。
    started = await manager.start(
        task.id, lambda: engine.run(skip_approval=True)
    )
    check("任务已经交给管家启动", started)

    await asyncio.wait_for(tools["tool_a"].started.wait(), timeout=3)
    cancelling = asyncio.create_task(manager.cancel(task.id))
    await asyncio.wait_for(cancelling, timeout=9)
    got = store.get_task(task.id, db_path=db)
    check("管家返回时任务已经是取消终态",
          got.status is TaskStatus.CANCELLED, got.status.value)
    finished = await wait_until(
        lambda: not manager.is_running(task.id), timeout=3
    )
    check("强制取消后执行体已经结束", finished)

    got = store.get_task(task.id, db_path=db)
    check("强制取消后任务不再显示执行中",
          got.status is TaskStatus.CANCELLED, got.status.value)
    check("强制取消后步骤不再显示执行中",
          got.steps[0].status is StepStatus.CANCELLED,
          got.steps[0].status.value)
    check("执行中的步骤如实说明底层操作无法确认停止",
          "无法" in got.steps[0].error and "执行" in got.steps[0].error,
          got.steps[0].error)
    check("没有假装任务完成", not any("任务完成" in t for _, t in published))


async def case_verify_failed(db: Path) -> None:
    print()
    print("校验不通过：如实报失败，不许报成功")
    tools = {"tool_a": FakeTool("tool_a"), "tool_b": FakeTool("tool_b")}
    adapter = FakeLLM(plan=two_step_plan(),
                      verify={"ok": False, "summary": "只做了一半", "missing": "笔记文件没落盘"},
                      decide={"action": "finish"})
    task = store.create_task(SESSION, "做个两步的事", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id, max_replans=0)
    await engine.run(skip_approval=True)

    got = store.get_task(task.id, db_path=db)
    check("任务判为失败", got.status is TaskStatus.FAILED, got.status.value)
    check("失败原因来自校验", "笔记文件没落盘" in got.error, got.error[:44])
    check("保留了校验给的总结", got.summary == "只做了一半")
    check("没有编造成功", "任务完成" not in " ".join(t for _, t in published))


async def case_report_step(db: Path) -> None:
    print()
    print("计划里的「汇报」步骤（不调工具）：必须真的说出点东西")
    tools = {"tool_a": FakeTool("tool_a")}
    adapter = FakeLLM(
        plan={"title": "看一眼再汇报", "steps": [
            {"goal": "列出目录", "tool": "tool_a", "arguments": {}},
            {"goal": "用一句话告诉用户结果", "tool": "", "arguments": {}},
        ]},
        verify={"ok": True, "summary": "看完了也说了"},
        compose="D 盘里有 3 个文件夹。",
    )
    task = store.create_task(SESSION, "看一眼再汇报", db_path=db)
    engine, published = build(db, adapter, tools, task_id=task.id)
    await engine.run(skip_approval=True)

    got = store.get_task(task.id, db_path=db)
    check("汇报步被记为成功", got.steps[1].status is StepStatus.SUCCEEDED,
          got.steps[1].status.value)
    check("汇报步存下了真正的内容", got.steps[1].result == "D 盘里有 3 个文件夹。",
          got.steps[1].result)
    check("那句话以 assistant 身份进了聊天（用户真的看得到）",
          ("assistant", "D 盘里有 3 个文件夹。") in published)
    check("任务整体成功（不再因为汇报步假失败）",
          got.status is TaskStatus.SUCCEEDED, got.status.value)

    print()
    print("汇报写不出来时：如实标失败，不许假装完成")
    tools2 = {"tool_a": FakeTool("tool_a")}
    adapter2 = FakeLLM(
        plan={"title": "x", "steps": [
            {"goal": "列出目录", "tool": "tool_a", "arguments": {}},
            {"goal": "用一句话告诉用户结果", "tool": "", "arguments": {}},
        ]},
        verify={"ok": True, "summary": "算了"},
        compose="",          # 模型什么都没写出来
    )
    task2 = store.create_task(SESSION, "看一眼再汇报", db_path=db)
    engine2, published2 = build(db, adapter2, tools2, task_id=task2.id)
    await engine2.run(skip_approval=True)
    got2 = store.get_task(task2.id, db_path=db)
    check("汇报步被如实标为失败", got2.steps[1].status is StepStatus.FAILED,
          got2.steps[1].status.value)
    check("失败原因说清了是写不出来", "没能写出" in got2.steps[1].error,
          got2.steps[1].error[:40])
    check("汇报步没有留下任何内容", got2.steps[1].result == "",
          repr(got2.steps[1].result))
    check("聊天里只有那句如实的说明，没有编造内容",
          any(r == "tool" and "没能写出汇报内容" in t for r, t in published2)
          and not any("D 盘里有" in t for _, t in published2))


async def case_manager_dedupe(db: Path) -> None:
    print()
    print("管家：同一个任务不会被跑两遍")
    started = asyncio.Event()

    async def slow_runner() -> None:
        started.set()
        await asyncio.sleep(0.3)

    tid = "dedupe-task"
    first = await manager.start(tid, slow_runner)
    await asyncio.wait_for(started.wait(), timeout=2)
    second = await manager.start(tid, slow_runner)
    check("第一次启动成功", first is True)
    check("重复启动被拒（不会跑两遍）", second is False)
    check("管家知道它在跑", manager.is_running(tid))
    await asyncio.sleep(0.4)
    await wait_until(lambda: not manager.is_running(tid), timeout=2)
    check("跑完之后自动清理", not manager.is_running(tid))


async def main_async() -> None:
    global manager_choices, pending_choose_helper
    pending_choose_helper = _ChoiceHelper()
    manager_choices = pending_choose_helper.manager_choices

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "workflow_engine.db"
        await case_normal(db)
        await case_not_approved(db)
        await case_approve_then_run(db)
        await case_plan_failed(db)
        await case_step_denied(db)
        await case_retry(db)
        await case_click_unknown_no_retry(db)
        await case_emergency_stop(db)
        await case_unknown_tool(db)
        await case_ask_user(db)
        await case_pause_resume(db)
        await case_cancel(db)
        await case_cancel_while_waiting_confirmation(db)
        await case_cancel_at_final_step(db)
        await case_forced_cancel_while_tool_running(db)
        await case_verify_failed(db)
        await case_report_step(db)
        await case_manager_dedupe(db)

    print()
    print("隔离性检查：")
    from app.memory.db import DB_PATH

    print(f"  主库位置：{DB_PATH}")
    check("主库里没有留下测试任务",
          all(t.session_id != SESSION for t in store.list_tasks(session_id=SESSION)))


def main() -> int:
    print("=" * 68)
    print("工作流引擎测试（阶段 3）")
    print("=" * 68)
    asyncio.run(main_async())

    print()
    print("=" * 68)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    if passed != total:
        print(f"结果：{passed}/{total} 通过，{total - passed} 项不通过")
        for note, ok, detail in RESULTS:
            if not ok:
                print(f"  [!!] {note}　{detail}")
        return 1
    print(f"结果：{passed}/{total} 全部通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
