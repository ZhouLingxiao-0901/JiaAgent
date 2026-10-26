"""问模型要计划、解析它的输出、每一步之后问它下一步怎么办。

这个模块负责和模型打交道的**全部**地方。分成三件事：

    plan()        把一个目标拆成有顺序的步骤
    decide_next() 每跑完一步，根据**真实结果**决定：继续 / 改计划 / 求助 / 收尾
    verify()      全部跑完之后，校验目标到底达成了没有

**为什么四处都在防着模型的输出格式？**
因为"模型一定会给你合法 JSON"这个假设是不成立的。它可能包一层 ```json 围栏、
可能先寒暄一句再给 JSON、也可能干脆给一段根本不是 JSON 的话。所以这里的做法是：
先宽容地解析（剥围栏、抠最外层花括号），不行就让模型**重说一次**，再不行就
**老实承认没拿到计划**（返回 None，由上层如实告诉用户），绝不瞎猜、绝不假装拆好了。

**这个模块不执行任何工具**——它只产出"要做什么"，怎么执行、要不要用户确认，
是 engine.py 和 app/agent/gate.py 的事。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.llm.base import LLMAdapter
from app.workflow.model import Step, StepStatus

logger = logging.getLogger(__name__)

MAX_STEPS = 8          # 一个计划最多几步。步数一多，模型越容易跑偏，用户也看不下去
MAX_REPLANS = 3        # 一个任务最多改几次计划，防止无限循环

# 允许的后续动作
ACTION_CONTINUE = "continue"
ACTION_REPLAN = "replan"
ACTION_ASK_USER = "ask_user"
ACTION_FINISH = "finish"
_ACTIONS = frozenset({ACTION_CONTINUE, ACTION_REPLAN, ACTION_ASK_USER, ACTION_FINISH})


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------


@dataclass
class PlanStep:
    """计划里的一步。和执行阶段落库的 Step 分开：这一步还没有数据库编号。"""

    goal: str
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    expect: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "tool": self.tool,
            "arguments": self.arguments,
            "expect": self.expect,
        }


@dataclass
class Plan:
    """拆好的计划。problems 记录的是"模型给的这一步我做了处理"，
    要如实告诉用户，不能悄悄改掉。"""

    title: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class Decision:
    """跑完一步之后的决定。"""

    action: str = ACTION_CONTINUE
    reason: str = ""
    new_steps: list[PlanStep] = field(default_factory=list)
    question: str = ""
    options: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Verdict:
    """终局校验的结论。"""

    ok: bool
    summary: str = ""
    missing: str = ""
    # False 表示"这次校验不是模型给的，是按步骤状态机械推出来的"
    verified: bool = True


# ----------------------------------------------------------------------
# 宽容地解析 JSON
# ----------------------------------------------------------------------


def _try_load(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _strip_fence(text: str) -> str:
    """剥掉 markdown 代码围栏。"""
    match = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else text


def _outermost_object(text: str) -> str:
    """抠出最外层的一对花括号。应对"先说了句话，然后才给 JSON"。"""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return text
    return text[start:end + 1]


def parse_json_object(text: str) -> dict[str, Any] | None:
    """尽力从一段文字里解出一个 JSON 对象。实在解不出来返回 None。

    纯函数，不调模型，很容易测。
    """
    if not text or not text.strip():
        return None

    for candidate in (
        text.strip(),
        _strip_fence(text).strip(),
        _outermost_object(text).strip(),
    ):
        data = _try_load(candidate)
        if data is not None:
            return data
    return None


# ----------------------------------------------------------------------
# 工具目录（塞进提示词，让模型只能用真实存在的工具）
# ----------------------------------------------------------------------


def build_tool_catalog(tools: list[dict[str, Any]]) -> str:
    """把 registry.openai_tools() 的输出压成人能看、模型也能看的一小段文字。"""
    lines: list[str] = []
    for item in tools:
        func = item.get("function") or {}
        name = func.get("name") or "?"
        params = func.get("parameters") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])

        bits = []
        for arg_name, spec in props.items():
            kind = (spec or {}).get("type", "any")
            bits.append(f"{arg_name}:{kind}" if arg_name in required else f"{arg_name}:{kind}(可选)")
        args_text = "、".join(bits) if bits else "无参数"

        desc = (func.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 90:
            desc = desc[:90] + "…"
        lines.append(f"- {name}（{args_text}）：{desc}")
    return "\n".join(lines)


def _render_steps(steps: list[Step]) -> str:
    """把当前步骤清单压成一段文字给模型看。"""
    lines: list[str] = []
    for step in steps:
        parts = [f"{step.step_index + 1}. [{step.status.label}] {step.goal}"]
        if step.tool:
            parts.append(f"   工具：{step.tool} 参数：{json.dumps(step.arguments, ensure_ascii=False)}")
        if step.result:
            parts.append(f"   结果：{step.result[:300]}")
        if step.error:
            parts.append(f"   错误：{step.error[:300]}")
        lines.append("\n".join(parts))
    return "\n".join(lines) if lines else "（还没有任何步骤）"


def mechanical_summary(goal: str, steps: list[Step]) -> str:
    """不靠模型，直接把步骤状态数一遍得出的总结。

    用在两个地方：校验没能完成时的兜底，以及最终写进任务的那句话——
    它是**机械的、不会骗人**的，所以哪怕模型那边出了问题，用户也能看到发生了什么。
    """
    done = sum(1 for s in steps if s.status is StepStatus.SUCCEEDED)
    failed = [s for s in steps if s.status is StepStatus.FAILED]
    skipped = sum(1 for s in steps if s.status is StepStatus.SKIPPED)

    head = f"目标：{goal}"
    body = f"共 {len(steps)} 步，成功 {done} 步"
    if skipped:
        body += f"，跳过 {skipped} 步"
    if failed:
        body += f"，失败 {len(failed)} 步（第 " + "、".join(
            str(s.step_index + 1) for s in failed
        ) + " 步）"
    return f"{head}\n{body}。"


# ----------------------------------------------------------------------
# 规划器
# ----------------------------------------------------------------------


class Planner:
    """和模型打交道的三个动作。adapter 和工具目录都可以注入，方便测试。"""

    def __init__(
        self,
        adapter: LLMAdapter,
        *,
        tools: list[dict[str, Any]] | None = None,
        max_steps: int = MAX_STEPS,
        max_replans: int = MAX_REPLANS,
        decide_mode: str = "always",
    ) -> None:
        self.adapter = adapter
        self._tools = tools
        self.max_steps = max(1, int(max_steps))
        self.max_replans = max(0, int(max_replans))
        # always    —— 每步之后都问模型（更贴合"根据真实结果决定下一步"，但更费调用）
        # on_failure —— 成功的步骤直接往下走，只有失败时才问模型
        self.decide_mode = decide_mode if decide_mode in ("always", "on_failure") else "always"

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def tools(self) -> list[dict[str, Any]]:
        """工具目录。没注入就现去 registry 拿（惰性 import，避免和 tools 层绕成环）。"""
        if self._tools is None:
            from app.tools.registry import registry

            self._tools = registry.openai_tools()
        return self._tools

    def known_tools(self) -> set[str]:
        return {
            (item.get("function") or {}).get("name", "")
            for item in self.tools()
        } - {""}

    async def _ask_json(self, system: str, user: str, *, what: str) -> dict[str, Any] | None:
        """问模型要一个 JSON 对象，带**一次**修复机会。

        修复机会是必要的：模型偶尔会包个围栏或者说句废话，重说一次基本就好了。
        但只给一次——给多了就是拿用户的钱去刷格式，而且真不行的话本来就该认输。
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        text = await self.adapter.chat(messages)
        data = parse_json_object(text)
        if data is not None:
            return data

        logger.warning("%s：模型第一次给的不是合法 JSON，让它重说一次", what)
        repair = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": (text or "")[:1500]},
            {
                "role": "user",
                "content": "你刚才的输出不是合法的 JSON。请**只**输出那个 JSON 对象本身，"
                           "不要有任何解释、前后缀或代码围栏。",
            },
        ]
        text2 = await self.adapter.chat(repair)
        data = parse_json_object(text2)
        if data is None:
            logger.error("%s：修复之后仍然不是合法 JSON，只能放弃", what)
        return data

    def _clean_steps(
        self, raw_steps: Any, *, what: str
    ) -> tuple[list[PlanStep], list[str]]:
        """把模型给的步骤清单清洗成能用的形式。返回（步骤, 处理说明）。

        清洗规则和**为什么**这么做：
          - 不是列表 / 条目不是对象  -> 丢掉，记一笔
          - goal 是空的             -> 丢掉，记一笔（没法执行一个没说要干什么的步骤）
          - tool 不在已注册的清单里 -> **保留这一步，但把 tool 清空**，降级成"纯说明步"。
            这样计划还连贯（步骤序号不乱），但绝不会去执行一个不存在的工具。
          - arguments 不是对象      -> 换成空对象
        """
        steps: list[PlanStep] = []
        problems: list[str] = []
        known = self.known_tools()

        if not isinstance(raw_steps, list):
            return [], [f"{what}：模型给的 steps 不是一个列表"]

        for index, item in enumerate(raw_steps):
            position = index + 1
            if not isinstance(item, dict):
                problems.append(f"第 {position} 步不是一个对象，已丢掉")
                continue

            goal = str(item.get("goal", "") or "").strip()
            if not goal:
                problems.append(f"第 {position} 步没说要干什么，已丢掉")
                continue

            tool = str(item.get("tool", "") or "").strip()
            if tool and tool not in known:
                problems.append(f"第 {position} 步想用不存在的工具「{tool}」，已降级为说明步骤")
                tool = ""

            arguments = item.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}

            steps.append(PlanStep(
                goal=goal,
                tool=tool,
                arguments=arguments,
                expect=str(item.get("expect", "") or "").strip(),
            ))

        if len(steps) > self.max_steps:
            problems.append(f"模型给了 {len(steps)} 步，超过上限 {self.max_steps}，后面的已砍掉")
            steps = steps[:self.max_steps]

        return steps, problems

    # ------------------------------------------------------------------
    # 一、拆计划
    # ------------------------------------------------------------------
    async def plan(self, goal: str) -> Plan | None:
        """把目标拆成步骤。拆不出来返回 None——调用方要如实告诉用户，不许假装拆好了。"""
        catalog = build_tool_catalog(self.tools())
        system = (
            "你是一个任务规划器，负责把用户的目标拆成有顺序的执行步骤。"
            "你只负责拆步骤，不负责执行。"
        )
        user = "\n".join([
            "把下面这个目标拆成有顺序的执行步骤。",
            "",
            f"目标：{goal}",
            "",
            "可用工具（**只能**用这些，不要编造）：",
            catalog,
            "",
            "输出要求：",
            "- 只输出一个 JSON 对象，不要解释、不要前后缀、不要代码围栏",
            '- 结构：{"title": "短标题", "steps": [{"goal": "这一步要干什么", '
            '"tool": "工具名或空字符串", "arguments": {}, "expect": "预期结果"}]}',
            f"- 最多 {self.max_steps} 步，能少就少",
            '- tool 填空字符串表示这一步不调工具，只是说明或汇报',
            "- 打开应用、写文件这类会动电脑的操作照常列，系统会自动请用户授权",
        ])

        data = await self._ask_json(system, user, what="拆计划")
        if data is None:
            return None

        steps, problems = self._clean_steps(data.get("steps"), what="拆计划")
        if not steps:
            logger.warning("拆计划失败：一步都没能留下")
            return None

        title = str(data.get("title", "") or "").strip()
        return Plan(title=title, steps=steps, problems=problems)

    # ------------------------------------------------------------------
    # 二、跑完一步之后怎么办
    # ------------------------------------------------------------------
    async def decide_next(
        self,
        goal: str,
        steps: list[Step],
        last_step: Step | None = None,
    ) -> Decision:
        """根据**真实执行结果**决定下一步。

        decide_mode=on_failure 时，上一步成功了就直接往下走，不问模型（省调用）。
        """
        last_ok = last_step is None or last_step.status is StepStatus.SUCCEEDED
        if self.decide_mode == "on_failure" and last_ok:
            return Decision(action=ACTION_CONTINUE, reason="上一步成功，直接继续")

        system = (
            "你是一个任务执行监督者。你要根据**真实的执行结果**决定接下来怎么办，"
            "不要凭空假设某一步成功了。"
        )
        user = "\n".join([
            f"目标：{goal}",
            "",
            "目前的步骤和结果：",
            _render_steps(steps),
            "",
            "请决定接下来怎么办。只输出一个 JSON 对象，不要解释、不要代码围栏：",
            '{"action": "continue|replan|ask_user|finish", "reason": "为什么这么决定", '
            '"new_steps": [], "question": "", "options": []}',
            "",
            "action 的含义：",
            "- continue：按原计划继续下一步",
            "- replan：原计划行不通了，用 new_steps 换掉**还没执行**的步骤",
            "- ask_user：卡住了，需要用户拿主意（question 是要问的话，"
            "options 是几个选项，每个形如 {\"id\": \"retry\", \"name\": \"重试\"}）",
            "- finish：目标已经达成，可以收尾了",
            "",
            "注意：如果某一步失败了，不要假装它成功了再往下走。",
        ])

        data = await self._ask_json(system, user, what="决定下一步")
        if data is None:
            # 兜底：不瞎猜。成功就继续，失败就交给用户定夺。
            if last_ok:
                return Decision(action=ACTION_CONTINUE, reason="没能解析模型的决定，按继续处理")
            return Decision(
                action=ACTION_ASK_USER,
                reason="没能解析模型的决定，而这一步又失败了，所以交给你定夺",
                question="这一步没成功，我也没能判断出下一步该怎么办。你想怎么处理？",
                options=_default_help_options(),
            )

        action = str(data.get("action", "") or "").strip()
        if action not in _ACTIONS:
            logger.warning("模型给了一个不认识的动作：%r，按 continue 处理", action)
            action = ACTION_CONTINUE

        decision = Decision(action=action, reason=str(data.get("reason", "") or ""))

        if action == ACTION_REPLAN:
            new_steps, problems = self._clean_steps(data.get("new_steps"), what="改计划")
            if not new_steps:
                # 说要改计划，却没给出能用的新步骤。这时候**不能**装作没事继续按原计划
                # 往下走——监督者刚刚说了这条路走不通。硬着头皮往下走就是在假装进展。
                # 所以老实停下来，把事情交回给用户。
                logger.warning("模型说要改计划，但没给出能用的新步骤，转为求助")
                decision.action = ACTION_ASK_USER
                decision.question = "原计划看起来走不通了，但我没能拿到新的计划。你想怎么处理？"
                decision.options = _default_help_options()
                decision.new_steps = []
            else:
                decision.new_steps = new_steps
                if problems:
                    decision.reason += "；" + "；".join(problems)

        if action == ACTION_ASK_USER:
            question = str(data.get("question", "") or "").strip()
            decision.question = question or "这一步没能按计划走下去，你想怎么处理？"
            raw_options = data.get("options")
            decision.options = _clean_options(raw_options) or _default_help_options()

        return decision

    # ------------------------------------------------------------------
    # 三、终局校验
    # ------------------------------------------------------------------
    async def verify(self, goal: str, steps: list[Step]) -> Verdict:
        """全部跑完之后，检查目标到底达成了没有，并给一句总结。"""
        system = (
            "你是一个结果校验者。你要根据**真实的步骤执行结果**判断目标有没有达成，"
            "不要凭计划本身推断成功。"
        )
        user = "\n".join([
            f"目标：{goal}",
            "",
            "步骤和真实结果：",
            _render_steps(steps),
            "",
            "只输出一个 JSON 对象，不要解释、不要代码围栏：",
            '{"ok": true 或 false, "summary": "一两句话的总结", "missing": "没达成的部分，达成了就留空"}',
        ])

        data = await self._ask_json(system, user, what="终局校验")
        if data is None:
            # 校验没做成。这时**不能**假装校验通过了——但也不能因此把一件
            # 明明跑完的事判成彻底失败。折中：按步骤状态机械判断，并明说是机械判断。
            ok = all(s.status is StepStatus.SUCCEEDED for s in steps) and bool(steps)
            logger.warning("终局校验没能完成，退回机械判断：ok=%s", ok)
            return Verdict(
                ok=ok,
                summary=mechanical_summary(goal, steps),
                missing="模型没能给出可解析的校验结论，上面是按步骤状态机械推出来的",
                verified=False,
            )

        return Verdict(
            ok=bool(data.get("ok")),
            summary=str(data.get("summary", "") or "").strip()
                    or mechanical_summary(goal, steps),
            missing=str(data.get("missing", "") or "").strip(),
        )


    # ------------------------------------------------------------------
    # 四、给"不调工具"的步骤写出真正的内容
    # ------------------------------------------------------------------
    async def compose(self, goal: str, step: Step, steps: list[Step]) -> str:
        """给一个不调工具的步骤（通常是"把结论一句话告诉我"）写出真正要对用户说的话。

        **为什么非要有这一步**：计划里很常见会有一步是"用一句话汇报"。如果引擎
        只是把它标成"完成"而什么都不说，那这一步就等于没做——用户什么都没收到，
        而且收尾校验会（正确地）判定目标没达成，最后报一个**假失败**：
        真实结果明明拿到了，状态却是失败。

        所以这里让模型基于**真实执行结果**把那段话写出来，用户才真的收到东西。
        """
        system = "你是 JiaAgent（佳），一个中文 AI 助手。用简洁、口语化的简体中文说话。"
        user = "\n".join([
            f"用户的原始目标：{goal}",
            "",
            "到目前为止的真实执行结果：",
            _render_steps(steps),
            "",
            f"现在请你完成这一步：{step.goal}",
            "只输出要对用户说的那段话本身。不要客套、不要罗列原始数据、不要标题。",
        ])
        text = await self.adapter.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        return (text or "").strip()


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def _default_help_options() -> list[dict[str, str]]:
    """卡住时给用户的默认选项。复用现有的"选择型待办"，不另造一套问答。"""
    return [
        {"id": "retry", "name": "重试这一步"},
        {"id": "skip", "name": "跳过，继续后面的"},
        {"id": "cancel", "name": "取消任务"},
    ]


def _clean_options(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw[:6]:
        if not isinstance(item, dict):
            continue
        option_id = str(item.get("id", "") or "").strip()
        name = str(item.get("name", "") or "").strip()
        if option_id and name:
            out.append({"id": option_id, "name": name})
    return out
