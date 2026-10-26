"""工作流持久化隔离测试（视觉感知 · 阶段 1）。

守四件事：
  1. 工具的**完整输出**不会写进 task_steps.result
  2. 工具的**原始错误**不会写进 task_steps.error，也不会进会话记录
  3. 模型在当前任务里**仍然拿得到完整结果**（内存里那份）
  4. 没设置 persist_* 的工具，行为与以前完全一致

全部用假模型、假工具、临时数据库：不联网、不碰主库。

用法：
    .venv\\Scripts\\python.exe scripts\\test_workflow_persist.py
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
from app.logging_setup import setup_logging  # noqa: E402
from app.tools.base import Tool, ToolResult  # noqa: E402
from app.workflow import store  # noqa: E402
from app.workflow.engine import WorkflowEngine  # noqa: E402
from app.workflow.model import TaskStatus  # noqa: E402
from app.workflow.planner import Planner  # noqa: E402

SESSION = "test-persist-session"
RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


# ----------------------------------------------------------------------
# 假模型 / 假工具
# ----------------------------------------------------------------------
class FakeLLM(LLMAdapter):
    def __init__(self, plan: dict, verify: dict, decide: dict | None = None) -> None:
        super().__init__(provider_name="fake", model="fake", api_key="x", base_url="x")
        self.plan = plan
        self.verify = verify
        self.decide = decide or {"action": "continue"}
        self.prompts: list[str] = []

    async def chat(self, messages: list[Message], **options: Any) -> str:
        # system 和 user 都拼进来：识别标志有的在 system 里
        text = " ".join(str(m.get("content", "")) for m in messages)
        self.prompts.append(text)
        if "拆成有顺序的执行步骤" in text:
            return json.dumps(self.plan, ensure_ascii=False)
        if "决定接下来怎么办" in text:
            return json.dumps(self.decide, ensure_ascii=False)
        if "判断目标有没有达成" in text:
            return json.dumps(self.verify, ensure_ascii=False)
        if "现在请你完成这一步" in text:
            return "一句话汇报。"
        return "{}"

    async def stream_chat(self, messages, **options):
        for piece in ():
            yield piece

    async def stream_turn(self, messages, tools=None, **options):
        for event in ():
            yield event


class FakeTool(Tool):
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name: str, result: ToolResult) -> None:
        self.name = name
        self.label = name
        self.description = "测试用"
        self.requires_confirmation = False
        self._result = result
        self.calls = 0

    async def run(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        return self._result


async def run_case(tmp: Path, tool: FakeTool, *, verify: dict,
                   max_replans: int = 0):
    db = tmp / f"{tool.name}.db"
    task = store.create_task(SESSION, "做一件测试的事", db_path=db)
    plan = {
        "title": "一步任务",
        "steps": [{"goal": "干这件事", "tool": tool.name, "arguments": {}}],
    }
    adapter = FakeLLM(plan=plan, verify=verify)
    planner = Planner(
        adapter,
        tools=[{"type": "function", "function": {
            "name": tool.name, "description": "测试用",
            "parameters": FakeTool.parameters,
        }}],
        max_replans=max_replans,
    )
    published: list[tuple[str, str]] = []
    engine = WorkflowEngine(
        task.id,
        planner=planner,
        tool_lookup=lambda name: tool,
        db_path=db,
        publisher=lambda role, text: published.append((role, text)),
    )
    await engine.run(skip_approval=True)
    return store.get_task(task.id, db_path=db), adapter, published


def published_text(published: list[tuple[str, str]]) -> str:
    return "\n".join(text for _, text in published)


# ----------------------------------------------------------------------
# 用例
# ----------------------------------------------------------------------
async def case_success(tmp: Path) -> None:
    print("\n[一] 成功：完整结果只给模型，库里只有安全摘要")
    marker = "SECRET-FULL-ANALYSIS-MARKER"
    tool = FakeTool("secret_tool", ToolResult(
        ok=True, output=marker, persist_text="SAFE-SUMMARY"
    ))
    task, adapter, published = await run_case(
        tmp, tool, verify={"ok": True, "summary": "做完了", "missing": ""}
    )

    check("任务最终成功", task.status is TaskStatus.SUCCEEDED, task.status.value)
    check("数据库存的是安全摘要", task.steps[0].result == "SAFE-SUMMARY",
          repr(task.steps[0].result))
    check("数据库里没有完整分析", marker not in task.steps[0].result)
    check("模型在当前任务里拿到了完整结果",
          any(marker in prompt for prompt in adapter.prompts),
          f"共 {len(adapter.prompts)} 次模型调用")
    check("写进会话记录的也没有完整分析",
          marker not in published_text(published), published_text(published)[:120])


async def case_failure(tmp: Path) -> None:
    print("\n[二] 失败：原始错误只给模型，库里只有安全错误")
    marker = "RAW-SECRET-ERROR-MARKER"
    tool = FakeTool("broken_tool", ToolResult(
        ok=False, error=marker, persist_error="SAFE-ERROR"
    ))
    task, adapter, published = await run_case(
        tmp, tool, verify={"ok": False, "summary": "没做成", "missing": "目标没达成"}
    )

    check("任务最终失败", task.status is TaskStatus.FAILED, task.status.value)
    check("数据库存的是安全错误", task.steps[0].error == "SAFE-ERROR",
          repr(task.steps[0].error))
    check("数据库里没有原始错误", marker not in task.steps[0].error)
    check("模型看到了真实错误（才能正确决策）",
          any(marker in prompt for prompt in adapter.prompts))
    check("写进会话记录的没有原始错误",
          marker not in published_text(published), published_text(published)[:120])


async def case_compat(tmp: Path) -> None:
    print("\n[三] 兼容：没设置 persist_* 的工具行为不变")
    tool = FakeTool("plain_tool", ToolResult(ok=True, output="普通输出"))
    task, _, _ = await run_case(
        tmp, tool, verify={"ok": True, "summary": "做完了", "missing": ""}
    )
    check("普通工具的输出照旧落库", task.steps[0].result == "普通输出",
          repr(task.steps[0].result))

    tool2 = FakeTool("plain_fail", ToolResult(ok=False, error="普通错误"))
    task2, _, _ = await run_case(
        tmp, tool2, verify={"ok": False, "summary": "没做成", "missing": "没达成"}
    )
    check("普通工具的错误照旧落库", task2.steps[0].error == "普通错误",
          repr(task2.steps[0].error))


async def case_retry(tmp: Path) -> None:
    print("\n[四] 失败后重试：旧错误不会粘在新结果上")
    # 第一次失败、第二次成功；用同一个 ToolResult 不行，所以这里手工模拟
    class FlakyTool(Tool):
        parameters = {"type": "object", "properties": {}, "required": []}
        name = "flaky_tool"
        label = "flaky_tool"
        description = "测试用"
        requires_confirmation = False

        def __init__(self) -> None:
            self.calls = 0

        async def run(self, **kwargs: Any) -> ToolResult:
            self.calls += 1
            if self.calls == 1:
                # 带"timeout"才符合引擎的可重试判断
                return ToolResult(ok=False, error="timeout 模拟",
                                  persist_error="SAFE-TIMEOUT")
            return ToolResult(ok=True, output="SECOND-TRY-FULL",
                              persist_text="SAFE-OUTPUT")

    tool = FlakyTool()
    db = tmp / "flaky.db"
    task = store.create_task(SESSION, "重试测试", db_path=db)
    plan = {"title": "重试", "steps": [
        {"goal": "干这件事", "tool": "flaky_tool", "arguments": {}},
    ]}
    adapter = FakeLLM(plan=plan, verify={"ok": True, "summary": "成功", "missing": ""})
    planner = Planner(adapter, tools=[{"type": "function", "function": {
        "name": "flaky_tool", "description": "测试用",
        "parameters": FlakyTool.parameters,
    }}])
    engine = WorkflowEngine(task.id, planner=planner, tool_lookup=lambda n: tool,
                            db_path=db, publisher=lambda r, t: None)
    await engine.run(skip_approval=True)
    final = store.get_task(task.id, db_path=db)

    check("重试后成功", final.steps[0].status.value == "succeeded",
          final.steps[0].status.value)
    check("库里写的是第二次的安全摘要", final.steps[0].result == "SAFE-OUTPUT",
          repr(final.steps[0].result))
    check("旧的错误没有被留在库里", final.steps[0].error == "",
          repr(final.steps[0].error))
    check("工具被调用了两次", tool.calls == 2, str(tool.calls))
    check("模型看到的是第二次的完整结果",
          any("SECOND-TRY-FULL" in prompt for prompt in adapter.prompts))


async def main_async(tmp: Path) -> None:
    await case_success(tmp)
    await case_failure(tmp)
    await case_compat(tmp)
    await case_retry(tmp)


def main() -> int:
    setup_logging("ERROR")
    print("=" * 70)
    print("工作流持久化隔离测试（视觉感知 · 阶段 1）")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(main_async(Path(tmp)))

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 70)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for note, _, detail in failed:
        print(f"  [失败] {note}" + (f"　{detail}" if detail else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
