"""确认闸门测试（自主工作流引擎 · 阶段 0）。

这个脚本守的是一条**安全底线**，不是功能：

  工作流引擎要替用户执行工具。如果它绕开确认闸门、直接调 tool.execute()，
  就等于把「高风险操作必须用户同意」这条规则整个废掉——而且用户看不出来，
  因为任务看起来照样「跑成功了」。

所以这里用一个假的、需要确认的工具，走**真实的** gate.authorize_tool()：

  用户拒绝 / 不答复  ->  outcome.proceed 必须是 False，而且 run() 一次都不能被调用
  用户允许          ->  才允许执行

**关键**：必须真的调用 authorize_tool。如果测一份复制粘贴出来的逻辑，
就等于什么都没测。所以下面绝不 import core.py 里那段老代码，只打闸门本身。

不联网、不启动任何程序、不碰真实数据库。

用法：
  .venv\\Scripts\\python.exe scripts\\test_workflow_confirm.py
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import gate, pending  # noqa: E402
from app.tools.base import Resolution, Tool, ToolResult  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
SESSION = "test-workflow-confirm"


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, ok, detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


# ----------------------------------------------------------------------
# 假工具：记录自己被调用过几次，好断言"到底执行了没有"
# ----------------------------------------------------------------------


class FakeDangerousTool(Tool):
    """装作一个高风险工具（比如删除文件）。要确认才能跑。"""

    name = "fake_dangerous"
    label = "假的高风险工具"
    description = "测试用，什么也不会真的做"
    parameters = {
        "type": "object",
        "properties": {"what": {"type": "string"}},
        "required": ["what"],
    }
    requires_confirmation = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, what: str = "", **_: Any) -> ToolResult:
        self.calls.append(what)
        return ToolResult(ok=True, output=f"执行了 {what}")


class FakeChoiceTool(Tool):
    """装作一个「对上了好几个候选，得先挑一个」的工具。"""

    name = "fake_choice"
    label = "假的选择工具"
    description = "测试用，什么也不会真的做"
    parameters = {
        "type": "object",
        "properties": {"app": {"type": "string"}},
        "required": ["app"],
    }
    requires_confirmation = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        return Resolution(
            args=args,
            question="对上了好几个，你要哪一个？",
            choices=[{"id": "id-a", "name": "甲"}, {"id": "id-b", "name": "乙"}],
        )

    async def run(self, app: str = "", resolved_id: str = "", **_: Any) -> ToolResult:
        self.calls.append({"app": app, "resolved_id": resolved_id})
        return ToolResult(ok=True, output=f"打开了 {resolved_id or app}")


class FakeReadOnlyTool(Tool):
    """只读工具：不该出现任何确认卡片。"""

    name = "fake_readonly"
    label = "假的只读工具"
    description = "测试用"
    parameters = {"type": "object", "properties": {}, "required": []}
    requires_confirmation = False

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, **_: Any) -> ToolResult:
        self.calls += 1
        return ToolResult(ok=True, output="读到了")


# ----------------------------------------------------------------------
# 驱动一次闸门。
#
# 这里**忠实模拟真实调用方**（app/agent/core.py 和以后的 workflow/engine.py）：
#   过闸门 -> 只有 proceed 才执行。少了后半步，就测不出"该执行的有没有执行"。
# ----------------------------------------------------------------------


async def run_gate(
    tool: Tool,
    args: dict[str, Any],
    decider,
    timeout: float = 1.0,
):
    """跑一遍 authorize_tool，并按真实调用方的做法决定要不要执行。

    decider(action) 是**普通函数**，返回：
      ("approve", None) / ("reject", None) / ("choose", option_id) / ("ignore", None)
    ignore 表示不答复，让闸门自己超时。
    """
    outcome = gate.GateOutcome()
    events: list[dict[str, Any]] = []

    answered: set[str] = set()

    async def answer_when_asked():
        # 一次闸门可能**先后**产出两张卡片（歧义工具：先 choose、再 confirm）。
        # 所以这里要一直守着，不能只答第一张——只答第一张的话，第二张会一直
        # 等到超时，工具就永远不会被执行。这个坑写第一版的时候就踩了。
        # 后面网页那边轮询 /api/pending 时同样要注意这一点。
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
                # ignore: 什么都不做，让它自己超时

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

    # 只有放行了才执行——和 core.py 里的顺序完全一致
    result = None
    if outcome.proceed:
        result = await tool.execute(args, injected=outcome.injected)
    return outcome, events, result


async def main_async() -> None:
    # ---------------- 拒绝 ----------------
    print()
    print("用户拒绝时，工具绝不能被执行：")
    tool = FakeDangerousTool()
    outcome, events, result = await run_gate(
        tool, {"what": "删掉 D 盘"}, lambda a: ("reject", None)
    )
    check("拒绝后没有放行", outcome.proceed is False)
    check("拒绝后 run() 一次都没被调用", tool.calls == [], f"calls={tool.calls}")
    check("拒绝后没有执行结果", result is None)
    check("拒绝了会产出一张确认卡片",
          [e.get("action", {}).get("kind") for e in events] == ["confirm"])
    check("拒绝的话术是真实的", "没有允许" in outcome.result_text, outcome.result_text[:30])
    check("拒绝会留下会话记录", outcome.log.startswith("[已拒绝]"), outcome.log[:30])

    # ---------------- 不答复（超时） ----------------
    print()
    print("用户不答复（超时）时，同样绝不能执行：")
    tool = FakeDangerousTool()
    outcome, _, result = await run_gate(
        tool, {"what": "删掉 D 盘"}, lambda a: ("ignore", None), timeout=0.2
    )
    check("超时后没有放行", outcome.proceed is False)
    check("超时后 run() 一次都没被调用", tool.calls == [], f"calls={tool.calls}")

    # ---------------- 允许 ----------------
    print()
    print("用户允许时才放行：")
    tool = FakeDangerousTool()
    outcome, _, result = await run_gate(
        tool, {"what": "删掉 D 盘"}, lambda a: ("approve", None)
    )
    check("允许后放行", outcome.proceed is True)
    check("允许后 run() 被调用一次", tool.calls == ["删掉 D 盘"], f"calls={tool.calls}")
    check("允许后拿到了真实执行结果",
          result is not None and result.ok, result.as_text() if result else "None")
    check("摘要取自 describe_action", bool(outcome.summary), outcome.summary[:40])

    # ---------------- 候选选择 ----------------
    print()
    print("歧义工具：先选，再确认，选中编号由服务端注入：")
    tool = FakeChoiceTool()

    def pick_second(action):
        # 第一张卡片是 choose（先选），选完之后还会来一张 confirm
        if action.kind == "choose":
            return ("choose", "id-b")
        return ("approve", None)

    outcome, events, _ = await run_gate(tool, {"app": "示例"}, pick_second)
    kinds = [e.get("action", {}).get("kind") for e in events]
    check("先产出一张选择卡片、再产出一张确认卡片", kinds == ["choose", "confirm"], str(kinds))
    check("两张都答完之后才放行", outcome.proceed is True)
    check("选中的编号被注入进工具参数",
          bool(tool.calls) and tool.calls[0]["resolved_id"] == "id-b", f"calls={tool.calls}")
    check("原始参数里没有被污染（app 仍是用户说的那个）",
          bool(tool.calls) and tool.calls[0]["app"] == "示例")
    check("确认卡片显示的是用户选中的那个",
          "乙（resolved_id=id-b）" in outcome.summary or "id-b" in outcome.summary,
          outcome.summary[:50])

    print()
    print("歧义工具：用户不选，也不能执行：")
    tool = FakeChoiceTool()
    outcome, _, result = await run_gate(
        tool, {"app": "示例"}, lambda a: ("ignore", None), timeout=0.2
    )
    check("不选后没有放行", outcome.proceed is False)
    check("不选后 run() 一次都没被调用", tool.calls == [], f"calls={tool.calls}")
    check("不选的话术是真实的", "没有从候选里挑" in outcome.result_text,
          outcome.result_text[:30])

    # ---------------- 只读工具 ----------------
    print()
    print("只读工具不该打扰用户：")
    tool = FakeReadOnlyTool()
    outcome, events, result = await run_gate(tool, {}, lambda a: ("approve", None))
    check("只读工具不产生确认卡片", events == [], str(events))
    check("只读工具直接放行", outcome.proceed is True)
    check("只读工具被执行了一次", tool.calls == 1, f"calls={tool.calls}")

    # ---------------- 收尾 ----------------
    print()
    print("清理：不该留下没答复的待办")
    leftover = pending.store.pending_for(SESSION)
    check("没有残留待办", leftover == [], f"{len(leftover)} 个")


def main() -> int:
    print("=" * 68)
    print("确认闸门测试（阶段 0）")
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
