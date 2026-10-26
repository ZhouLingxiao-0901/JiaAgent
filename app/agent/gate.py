"""用户确认闸门：一个工具到底能不能真的执行，由这里说了算。

**为什么单独抽成一个模块？**
这段逻辑原先长在 `app/agent/core.py` 的 `_handle_one_call()` 里。现在多了一个
调用方——工作流引擎（`app/workflow/engine.py`）也要执行工具。如果让引擎自己
再写一遍确认逻辑，两边迟早会长歪；最危险的歪法是引擎那一边「忘了」确认，
那就等于把「高风险操作必须用户同意」这条底线废掉了。

所以抽到这里，让**智能体循环和工作流引擎调用同一份代码**。要改确认行为，
改这一个文件。

**闸门有三段，顺序不能变：**
  1. `prepare()` —— 工具自己说「这事有歧义，先让用户挑一个」（目前只有打开应用用）
  2. 确认       —— `requires_confirmation` 的工具，等用户点允许/拒绝
  3. 注入       —— 用户挑中的编号由服务端注入，模型看不到也塞不进来

**这里只 import pending 和 tools.base，绝不 import core**，免得绕成环。

**关于取消**：调用方（比如工作流引擎）可以在别处调 `pending.store.answer(...)`
把等待中的确认提前唤醒并置为拒绝，`wait()` 会正常返回 False。这里不需要特殊处理。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.agent import pending
from app.tools.base import Resolution, Tool

logger = logging.getLogger(__name__)


@dataclass
class GateOutcome:
    """闸门的结论。

    调用方用 `async for` 消费事件，用**这个传入的对象**拿结果。为什么不直接
    `return`：`async for` 天然会丢掉生成器的返回值，写成 return 极容易误用
    （拿到 None 还以为是「没放行」）。传进来一个 holder，`async for` 就成了
    唯一正确的用法。
    """

    proceed: bool = False          # True = 放行，可以真的执行工具了
    injected: dict[str, Any] = field(default_factory=dict)
    summary: str = ""              # describe_action 的结果，授权卡片和 tool_start 都用它
    log: str = ""                  # 写进会话的 tool 记录，例如 "[已拒绝] 打开应用：记事本"
    result_text: str = ""          # 回灌给模型的话
    result_output: str = ""        # 给网页显示的输出
    # 给界面看的细分状态：rejected / blocked / path_not_allowed /
    # file_not_found / invalid_argument / failed。空串 = 通用状态。
    status: str = ""


async def authorize_tool(
    tool: Tool,
    args: dict[str, Any],
    *,
    session_id: str,
    outcome: GateOutcome,
    timeout: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """过一遍闸门，产出 0~2 个 `need_confirm` 事件。

    歧义工具会先产出一个 `kind="choose"`（选哪个），再产出 `kind="confirm"`
    （确认要动手）。不需要确认的工具一个都不产出，直接放行。

    timeout 是等用户答复的秒数；传 None 就用 pending 的默认值。后台任务
    跑着的时候用户未必盯着屏幕，所以工作流会传一个更长的值。
    """
    wait_seconds = pending.DEFAULT_TIMEOUT if timeout is None else timeout
    injected: dict[str, Any] = {}

    def _stop(reason: str, status: str = "blocked") -> None:
        """把闸门结论置为「不执行」，并写明真实原因。"""
        text = str(reason).strip() or "这次操作没有可用的目标"
        outcome.summary = text
        outcome.log = f"[未执行] {text}"
        outcome.result_text = f"这次没有执行：{text}。不要假设它已经完成。"
        outcome.result_output = text
        outcome.status = status or "blocked"

    # ---- 第一段：prepare，工具可能要先请用户挑一个 ----
    try:
        resolution = await tool.prepare(args)
    except Exception as exc:  # noqa: BLE001 - prepare 出错不该把整轮对话带崩
        logger.exception("工具 %s 的 prepare 出错", tool.name)
        if getattr(tool, "requires_prepared_target", False):
            # 这类工具的目标必须在确认前固定。prepare 崩了就没有可信目标，
            # 绝不能按「直接就绪」继续，更不能拿模型给的原始参数去执行。
            logger.info("工具 %s 需要 prepare 固定目标，已停止本次调用", tool.name)
            _stop(f"准备阶段出错，已停止：{type(exc).__name__}", status="failed")
            return
        logger.warning("工具 %s 不要求 prepare 固定目标，按「直接就绪」继续", tool.name)
        resolution = Resolution(args=args)

    if not isinstance(resolution, Resolution):
        if getattr(tool, "requires_prepared_target", False):
            _stop("准备阶段没有返回有效结果，已停止", status="failed")
            return
        resolution = Resolution(args=args)

    # ---- 第一段半：工具明确说“这次不能做” ----
    # 必须在创建确认卡片之前停。注意不能用抛异常来表达这个结论：上面的
    # except 会吞掉它并按“就绪”继续，那等于悄悄放行。
    if resolution.blocked:
        reason = str(resolution.blocked).strip() or "这次操作没有可用的目标"
        logger.info("工具 %s 在 prepare 阶段停下：%s", tool.name, reason)
        _stop(reason, status=resolution.blocked_code or "blocked")
        return

    try:
        prepared = dict(resolution.injected or {})
    except Exception as exc:  # noqa: BLE001 - 工具返回了畸形注入
        logger.exception("工具 %s 的注入参数不是字典", tool.name)
        _stop(f"准备阶段的目标信息无效：{type(exc).__name__}", status="failed")
        return

    if getattr(tool, "requires_prepared_target", False) and not prepared:
        _stop("准备阶段没有确定执行目标，已停止", status="failed")
        return

    # 工具可以检查注入是否完整（比如 auto/window 必须有 HWND）。
    # 默认返回空串，现有工具行为不变；检查本身出错也按不通过处理。
    try:
        problem = tool.check_injected(prepared)
    except Exception as exc:  # noqa: BLE001
        logger.exception("工具 %s 检查注入参数时出错", tool.name)
        problem = f"无法确认准备阶段的目标：{type(exc).__name__}"
    if problem:
        logger.info("工具 %s 的注入参数不完整：%s", tool.name, problem)
        _stop(problem, status="failed")
        return

    # prepare 阶段由服务端确定的值（比如固定的窗口句柄）在这里进入注入表。
    # 模型自己写不进这些键：它们不在工具 schema 里，validate() 会拒掉。
    injected.update(prepared)

    if resolution.needs_choice:
        choice = pending.store.create_choice(
            session_id=session_id,
            tool_name=tool.name,
            tool_label=tool.label or tool.name,
            question=resolution.question,
            options=resolution.choices,
        )
        yield {"type": "need_confirm", "action": choice.to_public_dict()}

        picked = await pending.store.wait_choice(choice, timeout=wait_seconds)
        if not picked:
            logger.info("用户没有从候选里选：%s", resolution.question)
            outcome.log = f"[未选择] {resolution.question}"
            outcome.result_text = "用户没有从候选里挑，所以这次没有执行。不要假设它已经完成。"
            outcome.result_output = "用户没有选择要操作哪一个，没有执行。"
            outcome.status = "rejected"
            return

        # 用户选中的编号只在这里注入。模型看不到也塞不进来——它不在工具的
        # 公开 schema 里，模型自己往参数里写这个键会被 validate() 判成非法参数。
        # 所以“用户选了哪个”这件事没法被伪造。
        injected["resolved_id"] = picked

    # 摘要要按“用户最后选定的那一个”来显示，所以把注入的参数也给
    # describe_action 看一眼（它只用来查真实身份，不会传给工具执行）。
    describe_args = dict(args)
    describe_args.update(injected)
    outcome.summary = tool.describe_action(describe_args)
    outcome.injected = injected

    # ---- 第二段：需要授权的，停下来等用户点头 ----
    if tool.requires_confirmation:
        action = pending.store.create(
            session_id=session_id,
            tool_name=tool.name,
            tool_label=tool.label or tool.name,
            arguments=args,
            summary=outcome.summary,
        )
        yield {"type": "need_confirm", "action": action.to_public_dict()}

        approved = await pending.store.wait(action, timeout=wait_seconds)
        if not approved:
            logger.info("用户拒绝执行 %s", outcome.summary)
            outcome.log = f"[已拒绝] {outcome.summary}"
            outcome.result_text = "用户没有允许这次操作，所以它没有执行。不要假设它已经完成。"
            outcome.result_output = "用户拒绝了这次操作，没有执行。"
            outcome.injected = {}
            outcome.status = "rejected"
            return

    # ---- 第三段：放行 ----
    outcome.proceed = True
