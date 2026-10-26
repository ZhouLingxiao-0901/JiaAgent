"""待授权动作：模型想动手了，但还没得到你同意。

流程是这样的：

  1. 智能体遇到需要授权的工具，先在这里登记一个"待办"
  2. 通过 SSE 把待办推给网页，网页弹出确认卡片
  3. 同时，智能体的协程在这里 await 等着，不会往下走
  4. 你点"允许"或"拒绝"，接口层调 answer()
  5. 等待的协程被唤醒，按你的决定继续

为什么必须等，而不是先执行了再通知你？
  因为"打开应用"和"写文件"会真的改变你的电脑。授权必须是一个
  真实的、由人做出的动作。如果让模型自己决定，那就等于没有授权。

写明白的限制：
  待办只存在内存里。如果网页关了、或者服务重启了，待办就没了，
  那次对话会停在原地。这是有意选的取舍：宁可不做，也不偷偷做。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300.0   # 秒。等不到回应就按"拒绝"处理。


@dataclass
class PendingAction:
    """一个等待用户答复的动作。两种，用 kind 区分：

      kind == "confirm" —— 等你点"允许 / 拒绝"
      kind == "choose"  —— 等你从 options 里挑一个
    """

    id: str
    session_id: str
    tool_name: str
    tool_label: str
    arguments: dict[str, Any]
    summary: str
    created_at: str
    kind: str = "confirm"
    question: str = ""
    options: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 这些是内部状态，不对外暴露
        self.event = asyncio.Event()
        self.approved = False
        self.answered = False
        self.chosen = ""

    def to_public_dict(self) -> dict[str, Any]:
        """给网页看的信息。里面没有任何内部字段。"""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "tool": self.tool_name,
            "tool_label": self.tool_label,
            "arguments": self.arguments,
            "summary": self.summary,
            "created_at": self.created_at,
            "kind": self.kind,
            "question": self.question,
            "options": self.options,
        }


class PendingStore:
    """所有还没答复的待授权动作。全局一个就够。"""

    def __init__(self) -> None:
        self._items: dict[str, PendingAction] = {}

    def create(
        self,
        *,
        session_id: str,
        tool_name: str,
        tool_label: str,
        arguments: dict[str, Any],
        summary: str,
    ) -> PendingAction:
        action = PendingAction(
            id=uuid.uuid4().hex,
            session_id=session_id,
            tool_name=tool_name,
            tool_label=tool_label,
            arguments=arguments,
            summary=summary,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._items[action.id] = action
        logger.info("登记待授权动作 %s：%s", action.id, summary)
        return action

    def create_choice(
        self,
        *,
        session_id: str,
        tool_name: str,
        tool_label: str,
        question: str,
        options: list[dict[str, Any]],
        summary: str = "",
    ) -> PendingAction:
        """登记一个"请用户从候选里挑一个"的待办。"""
        action = PendingAction(
            id=uuid.uuid4().hex,
            session_id=session_id,
            tool_name=tool_name,
            tool_label=tool_label,
            arguments={},
            summary=summary or question,
            created_at=datetime.now().isoformat(timespec="seconds"),
            kind="choose",
            question=question,
            options=list(options),
        )
        self._items[action.id] = action
        logger.info("登记选择请求 %s：%s（%d 个候选）", action.id, question, len(options))
        return action

    def get(self, action_id: str) -> PendingAction | None:
        return self._items.get(action_id)

    def choose(self, action_id: str, option_id: str) -> PendingAction | None:
        """用户在网页上点了某个候选。

        **只接受这个待办自己列出来的编号**。别的编号一律当无效请求拒掉。
        这条检查是这道功能的安全底线：候选项编号是服务端发出去的一次性
        令牌，模型既没见过也编不出没被提供过的选项，所以"用户选了什么"
        这件事没法被伪造。
        """
        action = self._items.get(action_id)
        if action is None:
            logger.warning("收到一个不认识的选择请求编号：%s", action_id)
            return None

        valid = {str(o.get("id", "")) for o in action.options}
        if option_id not in valid:
            logger.warning(
                "选择请求 %s 收到一个不在候选里的编号，已拒绝：%s", action_id, option_id
            )
            return None

        if action.answered:
            logger.info("选择请求 %s 已经答复过了，忽略这次", action_id)
            return action

        action.chosen = option_id
        action.answered = True
        action.event.set()
        logger.info("选择请求 %s 用户选中了：%s", action_id, option_id)
        return action

    def answer(self, action_id: str, approved: bool) -> PendingAction | None:
        """用户点了允许或拒绝。找不到这个待办就返回 None。"""
        action = self._items.get(action_id)
        if action is None:
            logger.warning("收到一个不认识的待授权动作编号：%s", action_id)
            return None
        if action.answered:
            logger.info("待授权动作 %s 已经答复过了，忽略这次", action_id)
            return action
        action.approved = approved
        action.answered = True
        action.event.set()
        logger.info("待授权动作 %s 被用户%s", action_id, "允许" if approved else "拒绝")
        return action

    async def _await_answer(self, action: PendingAction, timeout: float) -> None:
        """等一个答复，等不到就按"没答复"收场。答复完就把它从登记处撤掉。"""
        try:
            await asyncio.wait_for(action.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("待办 %s 等了 %.0f 秒没人回应", action.id, timeout)
            action.answered = True
        finally:
            self._items.pop(action.id, None)

    async def wait(self, action: PendingAction, timeout: float = DEFAULT_TIMEOUT) -> bool:
        """等用户点允许/拒绝。超时、或者根本没人回应，一律按拒绝处理。"""
        await self._await_answer(action, timeout)
        return action.approved

    async def wait_choice(
        self, action: PendingAction, timeout: float = DEFAULT_TIMEOUT
    ) -> str:
        """等用户挑一个。超时或没人选，返回空字符串（当作"没选"）。"""
        await self._await_answer(action, timeout)
        return action.chosen

    def cancel_all(self) -> int:
        """把所有还没答复的确认按"拒绝"收场。

        急停时调用：已经弹出的确认卡片不该在急停之后还能被批准。
        返回被取消的数量。
        """
        cancelled = 0
        for action in list(self._items.values()):
            if not action.answered:
                action.approved = False
                action.answered = True
                action.event.set()
                cancelled += 1
            self._items.pop(action.id, None)
        return cancelled

    def pending_for(self, session_id: str) -> list[PendingAction]:
        """某个会话里还没答复的动作。网页刷新后可以用它把卡片重新画出来。"""
        return [
            action for action in self._items.values()
            if action.session_id == session_id and not action.answered
        ]

    def count(self) -> int:
        return len(self._items)


# 全局唯一的待授权动作登记处
store = PendingStore()
