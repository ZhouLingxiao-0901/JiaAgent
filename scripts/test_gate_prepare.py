"""确认闸门的 prepare 扩展测试（阻止 / 服务端注入）。

背景：
  `Resolution.blocked` 非空时，闸门必须在**创建确认卡片之前**停下，
  不弹卡片、不执行；`Resolution.injected` 的值由服务端在 prepare 阶段
  固定，用户同意后随执行注入，模型无法在 arguments 里伪造。

这个脚本走**真实的** gate.authorize_tool()，不复制一份逻辑。

不联网、不启动程序、不碰真实数据库、不截图。

用法：
  .venv\\Scripts\\python.exe scripts\\test_gate_prepare.py
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import gate, pending  # noqa: E402
from app.tools import vision_tools  # noqa: E402
from app.tools.base import Resolution, Tool, ToolResult  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
SESSION = "test-gate-prepare"


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


# ----------------------------------------------------------------------
# 假工具
# ----------------------------------------------------------------------
class BlockedTool(Tool):
    name = "blocked_tool"
    label = "假的有目标的工具"
    description = "测试用"
    parameters = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": [],
    }
    requires_confirmation = True

    def __init__(self) -> None:
        self.calls = 0

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        return Resolution(args=args, blocked="没有可用的目标窗口")

    async def run(self, x: str = "", **_: Any) -> ToolResult:
        self.calls += 1
        return ToolResult(ok=True, output=f"执行了 {x}")


class BlockedChoiceTool(Tool):
    """既说“不能做”，又带了候选：blocked 必须优先。"""

    name = "blocked_choice_tool"
    label = "假的阻止+选择工具"
    description = "测试用"
    parameters = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": [],
    }
    requires_confirmation = True

    def __init__(self) -> None:
        self.calls = 0

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        return Resolution(
            args=args,
            blocked="目标不可用",
            choices=[{"id": "id-a", "name": "甲"}, {"id": "id-b", "name": "乙"}],
        )

    async def run(self, x: str = "", resolved_id: str = "", **_: Any) -> ToolResult:
        self.calls += 1
        return ToolResult(ok=True, output="不该执行")


class InjectedTool(Tool):
    """prepare 固定一个目标，用户同意后由服务端注入。"""

    name = "injected_tool"
    label = "假的注入工具"
    description = "测试用"
    parameters = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": [],
    }
    requires_confirmation = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        return Resolution(args=args, injected={"target": "VS Code"})

    def describe_action(self, args: dict[str, Any]) -> str:
        return f"目标固定为：{args.get('target')}"

    async def run(self, x: str = "", target: str = "") -> ToolResult:
        self.calls.append((x, target))
        return ToolResult(ok=True, output=f"对着 {target} 做了 {x}")


class InjectedChoiceTool(Tool):
    """同时有候选选择和 prepare 注入，两边的值都要到达执行。"""

    name = "injected_choice_tool"
    label = "假的注入+选择工具"
    description = "测试用"
    parameters = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": [],
    }
    requires_confirmation = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        return Resolution(
            args=args,
            injected={"target": "固定目标"},
            choices=[{"id": "id-a", "name": "甲"}, {"id": "id-b", "name": "乙"}],
        )

    async def run(
        self, x: str = "", target: str = "", resolved_id: str = ""
    ) -> ToolResult:
        self.calls.append((x, target, resolved_id))
        return ToolResult(ok=True, output="ok")


class RejectCaptureTool(Tool):
    """模拟“一旦执行就会截图并上传”的工具，用来验证拒绝后什么都没发生。"""

    name = "reject_capture_tool"
    label = "假的截图工具"
    description = "测试用"
    parameters = {"type": "object", "properties": {}, "required": []}
    requires_confirmation = True

    def __init__(self) -> None:
        self.capture_calls = 0
        self.upload_calls = 0
        self.model_calls = 0

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        return Resolution(args=args, injected={"target": "窗口 A"})

    def describe_action(self, args: dict[str, Any]) -> str:
        return f"查看窗口：{args.get('target')}"

    async def run(self, target: str = "") -> ToolResult:
        self.capture_calls += 1
        self.upload_calls += 1
        self.model_calls += 1
        return ToolResult(ok=True, output=f"看了 {target}")


class PrepareRaisesTool(Tool):
    """prepare() 崩溃，而且这个工具要求确认前固定目标。"""

    name = "prepare_raises_tool"
    label = "假的 prepare 崩溃工具"
    description = "测试用"
    parameters = {"type": "object", "properties": {}, "required": []}
    requires_confirmation = True
    requires_prepared_target = True

    def __init__(self) -> None:
        self.calls = 0

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        raise RuntimeError("模拟 prepare 崩溃")

    async def run(self, **_: Any) -> ToolResult:
        self.calls += 1
        return ToolResult(ok=True, output="不该执行")


class NoInjectionTool(Tool):
    """prepare() 正常返回，但一个注入值都没有。"""

    name = "no_injection_tool"
    label = "假的没有注入工具"
    description = "测试用"
    parameters = {"type": "object", "properties": {}, "required": []}
    requires_confirmation = True
    requires_prepared_target = True

    def __init__(self) -> None:
        self.calls = 0

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        return Resolution(args=args)

    async def run(self, **_: Any) -> ToolResult:
        self.calls += 1
        return ToolResult(ok=True, output="不该执行")


class MalformedPrepareTool(Tool):
    """prepare() 返回 None（不是 Resolution）。"""

    name = "malformed_prepare_tool"
    label = "假的返回值错误工具"
    description = "测试用"
    parameters = {"type": "object", "properties": {}, "required": []}
    requires_confirmation = True
    requires_prepared_target = True

    def __init__(self) -> None:
        self.calls = 0

    async def prepare(self, args: dict[str, Any]):
        return None

    async def run(self, **_: Any) -> ToolResult:
        self.calls += 1
        return ToolResult(ok=True, output="不该执行")


class LegacyPrepareRaisesTool(Tool):
    """不要求固定目标的普通工具：prepare 崩溃时保持原有行为。"""

    name = "legacy_prepare_raises_tool"
    label = "假的旧行为工具"
    description = "测试用"
    parameters = {"type": "object", "properties": {}, "required": []}
    requires_confirmation = True
    # requires_prepared_target 默认 False

    def __init__(self) -> None:
        self.calls = 0

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        raise RuntimeError("模拟旧工具 prepare 崩溃")

    async def run(self, **_: Any) -> ToolResult:
        self.calls += 1
        return ToolResult(ok=True, output="照常执行")


class CollisionTool(Tool):
    """模型参数和注入值同名时，注入值必须赢。"""

    name = "collision_tool"
    label = "假的同名参数工具"
    description = "测试用"
    parameters = {
        "type": "object",
        "properties": {"target": {"type": "string"}},
        "required": [],
    }
    requires_confirmation = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        return Resolution(args=args, injected={"target": "服务端固定"})

    def describe_action(self, args: dict[str, Any]) -> str:
        return f"目标={args.get('target')}"

    async def run(self, target: str = "") -> ToolResult:
        self.calls.append(target)
        return ToolResult(ok=True, output=f"用了 {target}")


# ----------------------------------------------------------------------
# 驱动真实闸门
# ----------------------------------------------------------------------
async def run_gate(tool: Tool, args: dict[str, Any], decider, timeout: float = 1.0):
    outcome = gate.GateOutcome()
    events: list[dict[str, Any]] = []
    answered: set[str] = set()

    async def answer_when_asked():
        while True:
            await asyncio.sleep(0.005)
            for action in pending.store.pending_for(SESSION):
                if action.id in answered:
                    continue
                answered.add(action.id)
                kind, value = decider(action)
                if kind == "approve":
                    pending.store.answer(action.id, True)
                elif kind == "reject":
                    pending.store.answer(action.id, False)
                elif kind == "choose":
                    pending.store.choose(action.id, value)

    answerer = asyncio.create_task(answer_when_asked())
    try:
        async for event in gate.authorize_tool(
            tool, args, session_id=SESSION, outcome=outcome, timeout=timeout
        ):
            events.append(event)
    finally:
        answerer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await answerer

    result = None
    if outcome.proceed:
        result = await tool.execute(args, injected=outcome.injected)
    return outcome, events, result


async def run_inspect_with_prepare(patched_prepare):
    """用真实 inspect_screen 走一遍闸门，把截图/模型调用都换成计数器。"""
    original_prepare = vision_tools.InspectScreenTool.prepare
    original_snapshot = vision_tools.capture.snapshot
    original_window = vision_tools.wincapture.capture_window
    original_client = vision_tools.build_client
    counts = {"desktop": 0, "window": 0, "model": 0}

    def desktop(*_args, **_kwargs):
        counts["desktop"] += 1
        raise AssertionError("不应截图")

    def window(*_args, **_kwargs):
        counts["window"] += 1
        raise AssertionError("不应截图")

    def client(*_args, **_kwargs):
        counts["model"] += 1
        raise AssertionError("不应调用视觉模型")

    vision_tools.InspectScreenTool.prepare = patched_prepare
    vision_tools.capture.snapshot = desktop
    vision_tools.wincapture.capture_window = window
    vision_tools.build_client = client
    try:
        tool = vision_tools.InspectScreenTool()
        outcome, events, result = await run_gate(
            tool, {"question": "看看"}, lambda _action: ("approve", None)
        )
    finally:
        vision_tools.InspectScreenTool.prepare = original_prepare
        vision_tools.capture.snapshot = original_snapshot
        vision_tools.wincapture.capture_window = original_window
        vision_tools.build_client = original_client
    return outcome, events, result, counts


async def main_async() -> None:
    # ---------------- blocked：确认前停下 ----------------
    print()
    print("prepare 明确阻止：不弹卡片、不执行")
    tool = BlockedTool()
    outcome, events, result = await run_gate(
        tool, {"x": "a"}, lambda _a: ("approve", None)
    )
    check("被阻止时没有放行", outcome.proceed is False)
    check("被阻止时没有确认卡片", events == [], str(events))
    check("被阻止时 run() 一次都没调用", tool.calls == 0, str(tool.calls))
    check("被阻止时没有执行结果", result is None)
    check("结果输出带上了真实原因", "没有可用的目标窗口" in outcome.result_output,
          outcome.result_output)
    check("会话记录标明未执行", outcome.log.startswith("[未执行]"), outcome.log[:40])
    check("没有残留待办", pending.store.pending_for(SESSION) == [])

    # ---------------- blocked 优先于 choices ----------------
    print()
    print("blocked 与 choices 同时存在时，blocked 优先")
    tool = BlockedChoiceTool()
    outcome, events, _ = await run_gate(
        tool, {"x": "a"}, lambda _a: ("approve", None)
    )
    check("没有弹选择卡片", events == [], str(events))
    check("没有执行", outcome.proceed is False and tool.calls == 0)
    check("原因保留", "目标不可用" in outcome.result_output, outcome.result_output)
    check("没有残留待办", pending.store.pending_for(SESSION) == [])

    # ---------------- 注入 ----------------
    print()
    print("prepare 注入的目标会到达执行，模型伪造不了")
    tool = InjectedTool()
    outcome, events, result = await run_gate(
        tool, {"x": "a"}, lambda _a: ("approve", None)
    )
    check("同意后放行", outcome.proceed is True)
    check("只产生一张确认卡片",
          [e.get("action", {}).get("kind") for e in events] == ["confirm"])
    check("卡片摘要用了注入后的目标", "VS Code" in outcome.summary, outcome.summary)
    check("执行拿到注入值", tool.calls == [("a", "VS Code")], str(tool.calls))
    check("执行成功", result is not None and result.ok)

    forged = await tool.execute({"x": "a", "target": "伪造目标"})
    check("模型伪造注入键会被 validate 拒绝",
          forged.ok is False and "不认识的参数" in forged.error, forged.error)

    # ---------------- 注入 + 候选选择 ----------------
    print()
    print("注入和候选选择可以同时工作")
    tool = InjectedChoiceTool()

    def pick_second(action):
        if action.kind == "choose":
            return ("choose", "id-b")
        return ("approve", None)

    outcome, events, _ = await run_gate(tool, {"x": "a"}, pick_second)
    kinds = [e.get("action", {}).get("kind") for e in events]
    check("先选择再确认", kinds == ["choose", "confirm"], str(kinds))
    check("两个值都到达执行",
          tool.calls == [("a", "固定目标", "id-b")], str(tool.calls))
    check("摘要里两个值都在",
          "固定目标" in outcome.summary and "id-b" in outcome.summary,
          outcome.summary)

    # ---------------- 拒绝：不捕获 / 不上传 / 不调模型 ----------------
    print()
    print("用户拒绝时，截图工具什么都不会做")
    tool = RejectCaptureTool()
    outcome, events, result = await run_gate(
        tool, {}, lambda _a: ("reject", None)
    )
    check("拒绝后没有放行", outcome.proceed is False)
    check("拒绝后没有截图", tool.capture_calls == 0, str(tool.capture_calls))
    check("拒绝后没有上传", tool.upload_calls == 0, str(tool.upload_calls))
    check("拒绝后没有调用视觉模型", tool.model_calls == 0, str(tool.model_calls))
    check("拒绝后没有执行结果", result is None)
    check("拒绝会留下真实记录", outcome.log.startswith("[已拒绝]"), outcome.log[:40])
    check("拒绝后没有残留待办", pending.store.pending_for(SESSION) == [])

    tool = RejectCaptureTool()
    outcome, _, _ = await run_gate(tool, {}, lambda _a: ("approve", None))
    check("同意的对照：确实会执行一次",
          outcome.proceed is True and tool.capture_calls == 1, str(tool.capture_calls))

    # ---------------- prepare 异常：需要固定目标的工具 ----------------
    print()
    print("prepare 抛异常 + 需要固定目标：确认前停止")
    tool = PrepareRaisesTool()
    outcome, events, result = await run_gate(
        tool, {}, lambda _action: ("approve", None)
    )
    check("没有放行", outcome.proceed is False)
    check("没有确认卡片", events == [], str(events))
    check("run() 没有被调用", tool.calls == 0, str(tool.calls))
    check("原因写明准备阶段出错",
          "准备阶段出错" in outcome.result_output, outcome.result_output)
    check("原因里带异常类型", "RuntimeError" in outcome.result_output,
          outcome.result_output)
    check("没有残留待办", pending.store.pending_for(SESSION) == [])

    # ---------------- prepare 没有注入 ----------------
    print()
    print("prepare 没有注入目标：确认前停止")
    tool = NoInjectionTool()
    outcome, events, result = await run_gate(
        tool, {}, lambda _action: ("approve", None)
    )
    check("没有放行", outcome.proceed is False)
    check("没有确认卡片", events == [], str(events))
    check("run() 没有被调用", tool.calls == 0, str(tool.calls))
    check("原因写明没有确定目标",
          "没有确定执行目标" in outcome.result_output, outcome.result_output)
    check("没有残留待办", pending.store.pending_for(SESSION) == [])

    # ---------------- prepare 返回非 Resolution ----------------
    print()
    print("prepare 返回非 Resolution：需要固定目标的工具直接停止")
    tool = MalformedPrepareTool()
    outcome, events, result = await run_gate(
        tool, {}, lambda _action: ("approve", None)
    )
    check("没有放行", outcome.proceed is False)
    check("没有确认卡片", events == [], str(events))
    check("run() 没有被调用", tool.calls == 0, str(tool.calls))
    check("原因写明了没有有效结果",
          "没有返回有效结果" in outcome.result_output, outcome.result_output)
    check("没有残留待办", pending.store.pending_for(SESSION) == [])

    # ---------------- 旧工具行为不变 ----------------
    print()
    print("不要求固定目标的旧工具：prepare 异常仍按旧行为继续")
    tool = LegacyPrepareRaisesTool()
    outcome, events, result = await run_gate(
        tool, {}, lambda _action: ("approve", None)
    )
    check("旧工具仍然放行", outcome.proceed is True)
    check("旧工具仍然有一张确认卡片",
          [e.get("action", {}).get("kind") for e in events] == ["confirm"],
          str(events))
    check("旧工具仍然被执行一次", tool.calls == 1, str(tool.calls))

    # ---------------- 真实 inspect_screen：prepare 崩溃 ----------------
    print()
    print("真实 inspect_screen：prepare 崩溃 -> 不弹卡片、不截图、不调模型")

    async def inspect_boom(_self, _args):
        raise RuntimeError("模拟 inspect_screen prepare 崩溃")

    outcome, events, result, counts = await run_inspect_with_prepare(inspect_boom)
    check("没有放行", outcome.proceed is False)
    check("没有确认卡片", events == [], str(events))
    check("没有截图",
          counts["desktop"] == 0 and counts["window"] == 0, str(counts))
    check("没有调用视觉模型", counts["model"] == 0, str(counts))
    check("原因写明准备阶段出错",
          "准备阶段出错" in outcome.result_output, outcome.result_output)
    check("没有残留待办", pending.store.pending_for(SESSION) == [])

    # ---------------- 真实 inspect_screen：注入不完整 ----------------
    print()
    print("真实 inspect_screen：注入不完整 -> 确认前停止")
    partial_cases = [
        ("没有注入", {}, "没有确定执行目标"),
        ("只有 scope", {"_scope": "window"}, "视觉模型"),
        ("缺 HWND", {"_scope": "window", "_vision_model": "m"},
         "没有固定目标窗口"),
        ("缺身份", {"_scope": "window", "_vision_model": "m",
                    "_target_hwnd": 7}, "身份信息"),
        ("desktop 缺模型", {"_scope": "desktop"}, "视觉模型"),
    ]
    for label, injected_values, keyword in partial_cases:
        async def partial(_self, _args, _inj=injected_values):
            return Resolution(args=_args, injected=dict(_inj))

        outcome, events, result, counts = await run_inspect_with_prepare(partial)
        check(f"{label} -> 没有放行", outcome.proceed is False)
        check(f"{label} -> 没有确认卡片", events == [], str(events))
        check(f"{label} -> 没有截图/模型",
              counts == {"desktop": 0, "window": 0, "model": 0}, str(counts))
        check(f"{label} -> 原因含「{keyword}」",
              keyword in outcome.result_output, outcome.result_output)

    # ---------------- 注入覆盖同名模型参数 ----------------
    print()
    print("服务端注入的同名参数会覆盖模型给的参数")
    tool = CollisionTool()
    outcome, events, result = await run_gate(
        tool, {"target": "模型写的"}, lambda _action: ("approve", None)
    )
    check("放行", outcome.proceed is True)
    check("执行使用的是服务端固定值",
          tool.calls == ["服务端固定"], str(tool.calls))
    check("确认卡片显示服务端固定值",
          "服务端固定" in outcome.summary, outcome.summary)
    check("确认卡片不显示模型写的值",
          "模型写的" not in outcome.summary, outcome.summary)


def main() -> int:
    print("=" * 68)
    print("确认闸门的 prepare 扩展测试（阻止 / 注入）")
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
