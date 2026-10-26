"""规划器测试（自主工作流引擎 · 阶段 2）。

全部用**假模型**，不联网、不花钱、不启动任何程序、不碰数据库。

守的是「模型输出不可靠」这件事：
  模型会包 ```json 围栏、会先寒暄一句、会给根本不是 JSON 的废话、会编造不存在的
  工具名、会给一堆超量的步骤。这些**全都必须被接住**，而且接的方式要诚实——
  宁可说"我没能拆出计划"，也绝不能假装拆好了。

用法：
  .venv\\Scripts\\python.exe scripts\\test_workflow_planner.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm.base import LLMAdapter, Message, TurnEvent  # noqa: E402
from app.workflow.model import Step, StepStatus  # noqa: E402
from app.workflow.planner import (  # noqa: E402
    ACTION_ASK_USER,
    ACTION_CONTINUE,
    ACTION_FINISH,
    ACTION_REPLAN,
    Planner,
    build_tool_catalog,
    mechanical_summary,
    parse_json_object,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, ok, detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


# ----------------------------------------------------------------------
# 假模型：按顺序吐预设的回复，并记下每次说了什么
# ----------------------------------------------------------------------


class FakeAdapter(LLMAdapter):
    def __init__(self, replies: list[str]) -> None:
        super().__init__(provider_name="fake", model="fake-model",
                         api_key="fake", base_url="http://fake")
        self.replies = list(replies)
        self.seen: list[list[Message]] = []

    async def chat(self, messages: list[Message], **options: Any) -> str:
        self.seen.append(messages)
        if not self.replies:
            return ""
        return self.replies.pop(0)

    async def stream_chat(self, messages, **options):
        for piece in ():
            yield piece

    async def stream_turn(self, messages, tools=None, **options) -> Any:
        for event in ():
            yield event


# 工具目录用假的，不依赖真实 registry
FAKE_TOOLS = [
    {"type": "function", "function": {
        "name": "list_directory",
        "description": "列出文件夹里有什么",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "create_text_file",
        "description": "写一个文本文件",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]},
    }},
]


def step(index: int, status: StepStatus, *, goal: str = "做点事",
         tool: str = "", result: str = "", error: str = "") -> Step:
    return Step(step_index=index, goal=goal, tool=tool, status=status,
                result=result, error=error)


def planner_with(replies: list[str], **kwargs) -> tuple[Planner, FakeAdapter]:
    adapter = FakeAdapter(replies)
    return Planner(adapter, tools=FAKE_TOOLS, **kwargs), adapter


# ----------------------------------------------------------------------
# 一、纯解析（不调模型）
# ----------------------------------------------------------------------


def test_parse() -> None:
    print()
    print("宽容解析（纯函数，不调模型）：")
    check("干净的 JSON", parse_json_object('{"a": 1}') == {"a": 1})
    check("包了 ```json 围栏",
          parse_json_object('```json\n{"a": 1}\n```') == {"a": 1})
    check("包了没标语言的围栏",
          parse_json_object('```\n{"a": 1}\n```') == {"a": 1})
    check("前面夹了句废话",
          parse_json_object('好的，我拆好了：{"a": 1}') == {"a": 1})
    check("后面夹了句废话",
          parse_json_object('{"a": 1}\n以上就是计划。') == {"a": 1})
    check("前后都夹了围栏和废话",
          parse_json_object('拆好了：\n```json\n{"a": 1}\n```\n需要我执行吗？') == {"a": 1})
    check("空字符串返回 None", parse_json_object("") is None)
    check("纯废话返回 None", parse_json_object("我觉得你这个需求不太明确") is None)
    check("是个数组（不是对象）返回 None", parse_json_object("[1, 2, 3]") is None)


# ----------------------------------------------------------------------
# 二、拆计划
# ----------------------------------------------------------------------


async def test_plan() -> None:
    print()
    print("拆计划：正常情况")
    good = json.dumps({
        "title": "看看 D 盘再写个笔记",
        "steps": [
            {"goal": "列出 D 盘顶层", "tool": "list_directory",
             "arguments": {"path": "D:\\"}, "expect": "看到文件夹"},
            {"goal": "把结论写成文件", "tool": "create_text_file",
             "arguments": {"path": "D:\\n.txt", "content": "hi"}},
        ],
    }, ensure_ascii=False)
    planner, adapter = planner_with([good])
    plan = await planner.plan("看看 D 盘然后写个笔记")
    check("拿到了计划", plan is not None)
    check("标题解析正确", plan and plan.title == "看看 D 盘再写个笔记")
    check("两步都在", plan and len(plan.steps) == 2)
    check("参数是 dict", plan and plan.steps[0].arguments == {"path": "D:\\"})
    check("工具名保留", plan and plan.steps[1].tool == "create_text_file")
    check("没有多余的问题记录", plan and plan.problems == [], str(plan.problems if plan else None))
    check("提示词里带了工具清单（模型才知道能用什么）",
          "list_directory" in adapter.seen[0][1]["content"])

    print()
    print("拆计划：模型输出不老实的时候")
    fenced = '```json\n' + good + '\n```'
    planner, _ = planner_with([fenced])
    check("围栏也能拆出计划", (await planner.plan("x")) is not None)

    planner, adapter = planner_with(["这需求我看不懂"] + [good])
    plan = await planner.plan("x")
    check("第一次是废话，修复一次后成功", plan is not None)
    check("确实问了两次", len(adapter.seen) == 2, f"{len(adapter.seen)} 次")
    check("修复那次的提示词里带了重说要求",
          "只**输出" in adapter.seen[1][3]["content"] or "只" in adapter.seen[1][3]["content"])

    planner, adapter = planner_with(["废话一", "废话二"])
    plan = await planner.plan("x")
    check("修复也救不回来时，老实返回 None（不假装拆好了）", plan is None)
    check("只给一次修复机会，不刷格式", len(adapter.seen) == 2, f"{len(adapter.seen)} 次")

    print()
    print("拆计划：模型编造工具 / 给超量步骤")
    bogus = json.dumps({
        "title": "x",
        "steps": [
            {"goal": "删掉整个 D 盘", "tool": "delete_everything", "arguments": {"path": "D:\\"}},
            {"goal": "正常列目录", "tool": "list_directory", "arguments": {"path": "D:\\"}},
        ],
    }, ensure_ascii=False)
    planner, _ = planner_with([bogus])
    plan = await planner.plan("x")
    check("编造的工具有没有被执行的风险", plan is not None and plan.steps[0].tool == "",
          f"实际 tool={plan.steps[0].tool!r}" if plan else "没拿到计划")
    check("那一步被保留（计划还连贯），但降级成了说明步骤",
          plan is not None and plan.steps[0].goal == "删掉整个 D 盘")
    check("如实记下了这次降级", plan is not None and any("不存在的工具" in p for p in plan.problems),
          str(plan.problems if plan else None))
    check("正常的那一步没受影响",
          plan is not None and plan.steps[1].tool == "list_directory")

    many = json.dumps({"title": "x", "steps": [
        {"goal": f"第 {i} 步", "tool": ""} for i in range(20)
    ]}, ensure_ascii=False)
    planner, _ = planner_with([many], max_steps=3)
    plan = await planner.plan("x")
    check("超量步骤被砍到上限", plan is not None and len(plan.steps) == 3,
          f"{len(plan.steps) if plan else None} 步")
    check("砍掉这件事也如实记下了",
          plan is not None and any("超过上限" in p for p in plan.problems))

    empty = json.dumps({"title": "x", "steps": []}, ensure_ascii=False)
    planner, _ = planner_with([empty])
    check("一步都没有时返回 None", (await planner.plan("x")) is None)

    print()
    print("工具目录渲染：")
    catalog = build_tool_catalog(FAKE_TOOLS)
    check("必填参数不带(可选)", "path:string" in catalog and "path:string(可选)" not in catalog)
    check("列出了工具名", "list_directory" in catalog and "create_text_file" in catalog)


# ----------------------------------------------------------------------
# 三、每步之后的决定
# ----------------------------------------------------------------------


async def test_decide() -> None:
    print()
    print("跑完一步之后的决定：")
    steps = [
        step(0, StepStatus.SUCCEEDED, goal="列目录", tool="list_directory", result="看到 3 个文件夹"),
        step(1, StepStatus.PENDING, goal="写文件", tool="create_text_file"),
    ]

    # 每种动作给它一个**合法**的载荷（replan 必须带 new_steps，否则走的是兜底分支）
    action_cases = [
        (ACTION_CONTINUE, {"action": "continue", "reason": "因为这样"}),
        (ACTION_REPLAN, {"action": "replan", "reason": "换个思路",
                         "new_steps": [{"goal": "换个路径再列一次", "tool": "list_directory",
                                        "arguments": {"path": "D:\\other"}}]}),
        (ACTION_ASK_USER, {"action": "ask_user", "reason": "卡住了", "question": "怎么办"}),
        (ACTION_FINISH, {"action": "finish", "reason": "搞定了"}),
    ]
    for action, payload in action_cases:
        planner, _ = planner_with([json.dumps(payload, ensure_ascii=False)])
        decision = await planner.decide_next("目标", steps, steps[0])
        check(f"能解析出 {action}", decision.action == action, decision.action)

    replan_reply = json.dumps({
        "action": "replan", "reason": "换个思路",
        "new_steps": [{"goal": "换个路径再列一次", "tool": "list_directory",
                       "arguments": {"path": "D:\\other"}}],
    }, ensure_ascii=False)
    planner, _ = planner_with([replan_reply])
    decision = await planner.decide_next("目标", steps, steps[0])
    check("改计划时带回了新步骤",
          decision.action == ACTION_REPLAN and len(decision.new_steps) == 1)
    check("新步骤的参数正确",
          decision.new_steps[0].arguments == {"path": "D:\\other"})

    ask_reply = json.dumps({
        "action": "ask_user", "reason": "卡住了",
        "question": "这一步失败了，怎么办？",
        "options": [{"id": "retry", "name": "重试"}, {"id": "cancel", "name": "算了"}],
    }, ensure_ascii=False)
    planner, _ = planner_with([ask_reply])
    decision = await planner.decide_next("目标", steps, steps[0])
    check("求助时带回了问题和选项",
          decision.action == ACTION_ASK_USER and decision.question
          and [o["id"] for o in decision.options] == ["retry", "cancel"])

    planner, _ = planner_with([json.dumps({"action": "ask_user"}, ensure_ascii=False)])
    decision = await planner.decide_next("目标", steps, steps[0])
    check("模型没给选项时补上默认选项（重试/跳过/取消）",
          [o["id"] for o in decision.options] == ["retry", "skip", "cancel"])

    print()
    print("模型又给不老实的东西时：")
    planner, _ = planner_with([json.dumps({"action": "翻个跟头"}, ensure_ascii=False)])
    decision = await planner.decide_next("目标", steps, steps[0])
    check("不认识的动作按继续处理", decision.action == ACTION_CONTINUE)

    planner, _ = planner_with(
        [json.dumps({"action": "replan", "new_steps": []}, ensure_ascii=False)])
    decision = await planner.decide_next("目标", steps, steps[0])
    check("说要改计划却没给新步骤 -> 停下来问用户（不硬着头皮往下走）",
          decision.action == ACTION_ASK_USER, decision.action)
    check("问的话说清了原因", "走不通" in decision.question, decision.question[:44])
    check("同时给了默认选项", [o["id"] for o in decision.options] == ["retry", "skip", "cancel"])

    planner, _ = planner_with(["废话一", "废话二"])
    decision = await planner.decide_next("目标", steps, steps[0])
    check("解析不了 + 上一步成功 -> 默认继续（不瞎折腾）",
          decision.action == ACTION_CONTINUE)

    failed = step(1, StepStatus.FAILED, goal="写文件", tool="create_text_file",
                  error="这个位置不在允许访问的范围内")
    planner, _ = planner_with(["废话一", "废话二"])
    decision = await planner.decide_next("目标", steps, failed)
    check("解析不了 + 上一步失败 -> 交给用户（不瞎猜）",
          decision.action == ACTION_ASK_USER)
    check("求助话术里没有编造成功", "没成功" in decision.question, decision.question[:40])

    print()
    print("省调用的模式：")
    planner, adapter = planner_with([], decide_mode="on_failure")
    decision = await planner.decide_next("目标", steps, steps[0])
    check("on_failure + 上一步成功 -> 不问模型，直接继续",
          decision.action == ACTION_CONTINUE and adapter.seen == [],
          f"问了 {len(adapter.seen)} 次")

    planner, adapter = planner_with(
        [json.dumps({"action": "ask_user", "question": "咋办"}, ensure_ascii=False)],
        decide_mode="on_failure",
    )
    decision = await planner.decide_next("目标", steps, failed)
    check("on_failure + 上一步失败 -> 才去问模型",
          decision.action == ACTION_ASK_USER and len(adapter.seen) == 1,
          f"问了 {len(adapter.seen)} 次")


# ----------------------------------------------------------------------
# 四、终局校验
# ----------------------------------------------------------------------


async def test_verify() -> None:
    print()
    print("终局校验：")
    steps = [
        step(0, StepStatus.SUCCEEDED, goal="列目录", result="看到 3 个文件夹"),
        step(1, StepStatus.SUCCEEDED, goal="写文件", result="写好了 D:\\n.txt"),
    ]

    planner, _ = planner_with([json.dumps(
        {"ok": True, "summary": "看完了 D 盘并写好了笔记", "missing": ""}, ensure_ascii=False)])
    verdict = await planner.verify("看看 D 盘再写笔记", steps)
    check("校验通过", verdict.ok is True and verdict.verified)
    check("总结来自模型", verdict.summary == "看完了 D 盘并写好了笔记")

    planner, _ = planner_with([json.dumps(
        {"ok": False, "summary": "没写完", "missing": "笔记文件没落盘"}, ensure_ascii=False)])
    verdict = await planner.verify("看看 D 盘再写笔记", steps)
    check("校验不通过并说明缺什么",
          verdict.ok is False and verdict.missing == "笔记文件没落盘")

    planner, _ = planner_with(["废话一", "废话二"])
    verdict = await planner.verify("看看 D 盘再写笔记", steps)
    check("校验解析不了时**不假装通过**（明说是机械判断）", verdict.verified is False)
    check("机械判断：全部成功 -> ok", verdict.ok is True)
    check("机械总结里写清了步骤数", "成功 2 步" in verdict.summary, verdict.summary[:60])

    broken = [step(0, StepStatus.SUCCEEDED), step(1, StepStatus.FAILED, error="写不进去")]
    planner, _ = planner_with(["废话一", "废话二"])
    verdict = await planner.verify("目标", broken)
    check("机械判断：有失败 -> 不 ok", verdict.ok is False)

    print()
    print("机械总结本身：")
    text = mechanical_summary("看看 D 盘", [
        step(0, StepStatus.SUCCEEDED), step(1, StepStatus.FAILED),
        step(2, StepStatus.SKIPPED),
    ])
    check("数得对：成功 1、失败 1、跳过 1",
          "成功 1 步" in text and "跳过 1 步" in text and "失败 1 步" in text, text)
    check("指出了失败的是第几步", "第 2 步" in text, text)


async def main_async() -> None:
    test_parse()
    await test_plan()
    await test_decide()
    await test_verify()


def main() -> int:
    print("=" * 68)
    print("规划器测试（阶段 2）")
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
