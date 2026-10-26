"""智能体核心：把记忆、模型、工具串起来的地方。

阶段一只有一条直线：存消息 -> 读历史 -> 调模型 -> 存回复。

阶段二多了一个循环，也就是真正让"佳"能干活的那部分：

    用户发消息
       └─► 调模型（同时把工具清单给它）
             ├─ 模型只想说话  -> 存下来，结束
             └─ 模型要求调工具
                   ├─ 需要授权的 -> 推给网页，停在这里等你点头
                   ├─ 真正执行工具，拿到真实结果（或真实报错）
                   ├─ 把结果塞回上下文，再问模型一次
                   └─ 回到开头（最多 MAX_TOOL_ROUNDS 轮）

关键点：模型永远不直接碰你的电脑，它只能"提出请求"，
真正动手的是工具，而且动手前可能还要过你这一关。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from app.agent import gate, pending
from app.llm.base import Message, TextDelta, ToolCall, ToolCallsReady
from app.llm.registry import create_adapter
from app.memory import store
from app.tools.registry import registry

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "你是 JiaAgent（佳），一个运行在用户 Windows 电脑上的中文 AI 助手。"
    "请始终用简体中文回答，语气简洁、清楚、口语化。"
    "注意：你说的每一句话都要是中文——**包括调用工具之前那句说明**。"
    "不要写「I'll search for…」「Let me…」这类英文开场白。"
    "\n\n你可以调用工具来真正操作这台电脑。几条必须遵守的规则："
    "\n1. 需要查系统信息、看文件夹里有什么时，直接调用工具去查，不要凭印象猜。"
    "\n2. 打开应用、创建文件这类会改变电脑的操作，请正常提出调用请求即可，"
    "系统会自动向用户请求授权。不要自己假装已经做完了。"
    "\n3. 工具返回的是真实执行结果。如果它失败了，就如实把失败原因告诉用户，"
    "绝对不要编造一个成功的结果。"
    "\n4. 你没有删除文件、执行命令行、修改注册表的工具，这些事做不到，不要承诺。"
    "\n5. 拿到工具结果后，用一两句话向用户汇报结论，不要把原始数据整段贴出来。"
)

HISTORY_LIMIT = 20        # 每次最多带多少条历史消息给模型
MAX_TOOL_ROUNDS = 5       # 一轮对话里最多允许几轮工具调用，防止绕圈


class Agent:
    """一次对话的编排者。用完就丢，不需要长期保存。"""

    def __init__(
        self,
        provider_name: str | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        *,
        tools_enabled: bool = True,
    ) -> None:
        self.adapter = create_adapter(provider_name)
        self.system_prompt = system_prompt
        self.tools_enabled = tools_enabled

    # ------------------------------------------------------------------
    # 基本信息
    # ------------------------------------------------------------------
    @property
    def model(self) -> str:
        return self.adapter.model

    @property
    def provider_name(self) -> str:
        return self.adapter.provider_name

    # ------------------------------------------------------------------
    # 拼上下文
    # ------------------------------------------------------------------
    def _build_messages(self, session_id: str) -> list[Message]:
        """拼出这次要发给模型的完整上下文。

        历史只取 user / assistant 两种角色。工具执行记录虽然也存了库，
        但不回灌给模型，免得把远古的工具调用当成现在还有效的状态。
        """
        messages: list[Message] = [{"role": "system", "content": self.system_prompt}]
        for row in store.get_messages(session_id, limit=HISTORY_LIMIT):
            messages.append({"role": row["role"], "content": row["content"]})
        return messages

    @staticmethod
    def _assistant_tool_calls_message(text: str, calls: list[ToolCall]) -> Message:
        """把"模型要求调用工具"这件事，按协议格式补进上下文。"""
        return {
            "role": "assistant",
            "content": text or "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in calls
            ],
        }

    @staticmethod
    def _tool_result_message(call_id: str, content: str) -> Message:
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    # ------------------------------------------------------------------
    # 阶段一的简单通路（保留，脚本和旧调用方还在用）
    # ------------------------------------------------------------------
    async def reply(self, session_id: str, user_text: str) -> dict:
        """非流式、不带工具：等模型说完，一次性拿到完整回复。"""
        store.add_message(session_id, "user", user_text)
        messages = self._build_messages(session_id)
        logger.info("调用 %s（上下文 %d 条消息）", self.model, len(messages))

        text = await self.adapter.chat(messages)

        store.add_message(session_id, "assistant", text, model=self.model)
        return {
            "session_id": session_id,
            "reply": text,
            "model": self.model,
            "provider": self.provider_name,
        }

    async def stream_reply(self, session_id: str, user_text: str) -> AsyncIterator[str]:
        """流式、不带工具：逐段产出回复文字。"""
        store.add_message(session_id, "user", user_text)
        messages = self._build_messages(session_id)
        logger.info("流式调用 %s（上下文 %d 条消息）", self.model, len(messages))

        parts: list[str] = []
        async for piece in self.adapter.stream_chat(messages):
            parts.append(piece)
            yield piece

        full_text = "".join(parts)
        if full_text:
            store.add_message(session_id, "assistant", full_text, model=self.model)

    # ------------------------------------------------------------------
    # 阶段二：带工具的完整一轮
    # ------------------------------------------------------------------
    async def run_turn(self, session_id: str, user_text: str) -> AsyncIterator[dict[str, Any]]:
        """跑完一整轮对话，中间可能夹着好几次工具调用。

        产出的事件（接口层会原样转成 SSE 帧给网页）：
          {"type": "delta",        "text": "..."}              一段回复文字
          {"type": "tool_start",   "tool": ..., "summary": ...} 开始执行某工具
          {"type": "tool_result",  "tool": ..., "ok": bool, "output": "..."}
          {"type": "need_confirm", "action": {...}}             需要用户点确认
          {"type": "final",        "text": "..."}              这一轮说完了
        """
        store.add_message(session_id, "user", user_text)
        messages = self._build_messages(session_id)

        tools = registry.openai_tools() if self.tools_enabled else []
        logger.info(
            "开始一轮对话：模型 %s，上下文 %d 条，可用工具 %d 个",
            self.model, len(messages), len(tools),
        )

        for round_index in range(1, MAX_TOOL_ROUNDS + 1):
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []

            async for event in self.adapter.stream_turn(messages, tools=tools):
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
                    yield {"type": "delta", "text": event.text}
                elif isinstance(event, ToolCallsReady):
                    tool_calls = event.calls

            text = "".join(text_parts)

            # ---- 情况一：模型没要求用工具，这一轮就是最终回答 ----
            if not tool_calls:
                if text.strip():
                    store.add_message(session_id, "assistant", text, model=self.model)
                else:
                    logger.warning("模型既没说话也没调用工具，这一轮是空的")
                yield {"type": "final", "text": text}
                return

            # ---- 情况二：模型要求调用工具 ----
            logger.info(
                "第 %d 轮：模型要求调用 %d 个工具（%s）",
                round_index, len(tool_calls), "、".join(c.name for c in tool_calls),
            )
            if text.strip():
                # 模型在调工具前可能先说了句话，比如"我来帮你打开记事本"
                store.add_message(session_id, "assistant", text, model=self.model)

            messages.append(self._assistant_tool_calls_message(text, tool_calls))

            for call in tool_calls:
                async for event in self._handle_one_call(session_id, messages, call):
                    yield event

        # ---- 转了太多圈还没结束，收尾 ----
        logger.warning("工具调用达到 %d 轮上限，强制收尾", MAX_TOOL_ROUNDS)
        yield {
            "type": "delta",
            "text": f"\n\n（工具调用已经进行了 {MAX_TOOL_ROUNDS} 轮，我先停下来。）",
        }
        closing = ""
        async for event in self.adapter.stream_turn(messages, tools=[]):
            if isinstance(event, TextDelta):
                closing += event.text
                yield {"type": "delta", "text": event.text}
        if closing.strip():
            store.add_message(session_id, "assistant", closing, model=self.model)
        yield {"type": "final", "text": closing}

    async def _handle_one_call(
        self, session_id: str, messages: list[Message], call: ToolCall
    ) -> AsyncIterator[dict[str, Any]]:
        """处理一个工具调用：校验、可能要授权、执行、把结果塞回上下文。"""
        tool = registry.get(call.name)
        if tool is None:
            reason = f"没有名为 {call.name} 的工具，无法执行"
            logger.warning(reason)
            store.add_message(session_id, "tool", f"[未执行] {reason}")
            messages.append(self._tool_result_message(call.id, f"执行失败：{reason}"))
            yield {
                "type": "tool_result",
                "tool": call.name,
                "ok": False,
                "output": reason,
                "summary": f"调用工具：{call.name}",
                "status": "failed",
            }
            return

        args = call.parsed_arguments()
        if not args and call.arguments.strip() not in ("", "{}"):
            # 参数不是一个合法的 JSON 对象
            reason = f"参数格式不对，收到的原始内容是：{call.arguments[:200]}"
            logger.warning("工具 %s %s", call.name, reason)
            store.add_message(session_id, "tool", f"[未执行] {call.name} {reason}")
            messages.append(self._tool_result_message(call.id, f"执行失败：{reason}"))
            yield {
                "type": "tool_result",
                "tool": call.name,
                "ok": False,
                "output": reason,
                "summary": f"调用工具：{call.name}",
                "status": "invalid_argument",
            }
            return

        # ---- 过闸门：prepare(候选) → 用户确认 → 注入 ----
        # 这三段逻辑原先就长在这里，现在抽到了 app/agent/gate.py。抽出去不是
        # 为了好看：工作流引擎（app/workflow/engine.py）也要执行工具，让它走
        # 同一份代码，才不会出现"引擎那条路忘了确认"这种绕过授权的漏洞。
        outcome = gate.GateOutcome()
        async for event in gate.authorize_tool(
            tool, args, session_id=session_id, outcome=outcome
        ):
            yield event

        if not outcome.proceed:
            store.add_message(session_id, "tool", outcome.log)
            messages.append(self._tool_result_message(call.id, outcome.result_text))
            yield {
                "type": "tool_result",
                "tool": tool.name,
                "ok": False,
                "output": outcome.result_output,
                "summary": outcome.summary,
                "status": outcome.status,
            }
            return

        # ---- 真正执行 ----
        yield {"type": "tool_start", "tool": tool.name, "summary": outcome.summary}
        result = await tool.execute(args, injected=outcome.injected)
        store.add_message(
            session_id,
            "tool",
            f"{outcome.summary} -> {'成功' if result.ok else '失败'}",
        )
        messages.append(self._tool_result_message(call.id, result.as_text()))
        yield {
            "type": "tool_result",
            "tool": tool.name,
            "ok": result.ok,
            "output": result.as_text(),
            "summary": outcome.summary,
            "status": result.status,
        }
