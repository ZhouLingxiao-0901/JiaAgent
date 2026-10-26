"""微信模块和 Jia 现有聊天/任务体系的桥。

**这不是第二个 Agent**。这个文件只做四件事：

  1. 把微信用户映射到一个 Jia 会话（持久化在 wechat 自己的库里）；
  2. 普通聊天交给现有的 `Agent.run_turn()`，但没有启用工具；
  3. `/task` 交给现有的 `workflow_service.create_and_start()`；
  4. 把批准/拒绝/暂停/恢复/取消交给现有的 `manager`、`pending.store`。

它**没有** `tool.execute()` 调用，**没有** subprocess，**没有**鼠标键盘。
微信端的“批准”最终只是调用 `manager.approve()` 或 `pending.store.answer()`，
工具能不能执行仍然由 app/agent/gate.py 决定。

**普通聊天不启用工具**：微信端没有能力在聊天回合里安全地展示和确认任意
工具调用。第一版把工具操作全部收进工作流：工作流有任务编号、持久状态和
一次性授权码，可以审计。聊天里模型想调工具也不会执行。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.agent import pending as pending_module
from app.agent.core import Agent
from app.errors import JiaAgentError
from app.memory import store as memory
from app.settings import get_tools_config
from app.wechat.errors import WeChatError
from app.wechat.store import WeChatStore
from app.workflow import service as workflow_service
from app.workflow import store as workflow_store
from app.workflow.manager import manager as task_manager
from app.workflow.model import Task, TaskStatus

logger = logging.getLogger(__name__)


class BridgeError(WeChatError):
    """桥接层的可预期错误（任务不存在、状态不允许、模型未配置等）。"""


@dataclass(frozen=True)
class RemoteAction:
    action_id: str
    kind: str                 # confirm / choose
    tool_label: str
    summary: str
    question: str
    options: list[dict[str, Any]]
    session_id: str


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class JiaRuntimeBridge:
    def __init__(
        self,
        *,
        store: WeChatStore,
        agent_factory: Callable[..., Any] = Agent,
        workflow: Any = workflow_service,
        workflow_store_: Any = workflow_store,
        manager: Any = task_manager,
        pending_store: Any = pending_module.store,
        memory_api: Any = memory,
        workflow_enabled: Callable[[], bool] = lambda: get_tools_config().workflow.enabled,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.agent_factory = agent_factory
        self.workflow = workflow
        self.workflow_store = workflow_store_
        self.manager = manager
        self.pending_store = pending_store
        self.memory = memory_api
        self.workflow_enabled = workflow_enabled
        self.clock = clock

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    def _session_for(
        self, account_id: str, user_id: str, *, model: str = ""
    ) -> str:
        session_id = self.store.get_session_id(account_id, user_id)
        if session_id and not self.memory.session_exists(session_id):
            logger.warning("微信用户映射的会话已不存在，重新建一个")
            session_id = ""
        if not session_id:
            created = self.memory.create_session(title="微信对话", model=model)
            session_id = str(created["id"])
            self.store.set_session_id(
                account_id, user_id, session_id, self.clock()
            )
            return session_id
        if model:
            # 只更新会话记录的模型名，不新建会话
            self.memory.ensure_session(session_id, model=model)
        return session_id

    # ------------------------------------------------------------------
    # 普通聊天
    # ------------------------------------------------------------------
    async def chat(self, account_id: str, user_id: str, text: str) -> str:
        """跑一轮纯文字聊天，返回完整回复。

        `tools_enabled=False` 是有意的：普通聊天不走远程工具确认。
        如果模型仍然产出了 need_confirm（理论上不可能），这里会把它按
        拒绝处理并报错——绝不让聊天回合绕过工作流的授权设计。
        """
        clean = str(text or "").strip()
        if not clean:
            raise BridgeError("消息是空的")
        agent = self.agent_factory(provider_name=None, tools_enabled=False)
        session_id = self._session_for(
            account_id, user_id, model=getattr(agent, "provider_name", "")
        )

        parts: list[str] = []
        final_text = ""
        async for event in agent.run_turn(session_id, clean):
            kind = event.get("type")
            if kind == "delta":
                parts.append(str(event.get("text", "")))
            elif kind == "final":
                final_text = str(event.get("text", ""))
            elif kind == "need_confirm":
                action = event.get("action") or {}
                action_id = str(action.get("id", ""))
                if action_id:
                    self.pending_store.answer(action_id, False)
                raise BridgeError(
                    "这次操作需要在电脑端确认。微信普通聊天不能批准工具操作，"
                    "所以没有执行。"
                )
        result = final_text.strip() or "".join(parts).strip()
        return result or "我这边没有生成回复，请再试一次。"

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------
    async def create_task(self, account_id: str, user_id: str, goal: str) -> Task:
        clean = str(goal or "").strip()
        if not clean:
            raise BridgeError("任务目标不能为空")
        if not self.workflow_enabled():
            raise BridgeError(
                "自主工作流现在是关着的。请先在电脑端打开后再从微信派任务。"
            )
        session_id = self._session_for(account_id, user_id)
        task = await self.workflow.create_and_start(
            goal=clean, session_id=session_id, provider=None
        )
        self.store.link_task(
            task_id=task.id,
            account_id=account_id,
            user_id=user_id,
            session_id=session_id,
            now=self.clock(),
        )
        logger.info("微信用户创建了任务 %s（%d 字）", task.id[:8], len(clean))
        return task

    def get_task(self, task_id: str) -> Task | None:
        return self.workflow_store.get_task(task_id, with_steps=True)

    def list_linked_tasks(
        self, account_id: str, user_id: str, *, limit: int = 20
    ) -> list[Task]:
        tasks: list[Task] = []
        for link in self.store.list_task_links(account_id, user_id, limit=limit):
            task = self.workflow_store.get_task(
                str(link["task_id"]), with_steps=False
            )
            if task is not None:
                tasks.append(task)
        return tasks

    def resolve_task(
        self, account_id: str, user_id: str, prefix: str
    ) -> Task | None:
        """按短编号找**这个用户自己的**任务。空的 prefix = 最近一个。

        只查 task_links，别人创建的 32 位任务号即使被说对了也拿不到。
        """
        text = str(prefix or "").strip().casefold()
        links = self.store.list_task_links(account_id, user_id, limit=200)
        if not text:
            for link in links:
                task = self.workflow_store.get_task(
                    str(link["task_id"]), with_steps=True
                )
                if task is not None:
                    return task
            return None
        if len(text) > 32 or any(ch not in "0123456789abcdef" for ch in text):
            raise BridgeError("任务编号只能由 0-9、a-f 组成")
        matches = [
            str(link["task_id"]) for link in links
            if str(link["task_id"]).casefold().startswith(text)
        ]
        unique = sorted(set(matches))
        if not unique:
            return None
        if len(unique) > 1:
            raise BridgeError("这个编号对应多个任务，请多写几位")
        return self.workflow_store.get_task(unique[0], with_steps=True)

    def is_linked(self, task_id: str, account_id: str, user_id: str) -> bool:
        link = self.store.get_task_link(task_id)
        return bool(
            link
            and str(link["account_id"]) == account_id
            and str(link["user_id"]) == user_id
        )

    # ------------------------------------------------------------------
    # 任务控制（行为和 /api/tasks 一致）
    # ------------------------------------------------------------------
    async def approve_plan(self, task: Task) -> bool:
        if task.is_terminal:
            raise BridgeError("这个任务已经结束了，不能再批准")
        await self.manager.approve(task.id)
        started = await self.workflow.start_if_idle(task, skip_approval=True)
        logger.info("微信端批准了任务 %s 的计划（补执行体：%s）", task.id[:8], started)
        return bool(started)

    async def reject_plan(self, task: Task) -> None:
        if task.is_terminal:
            raise BridgeError("这个任务已经结束了")
        if self.manager.is_running(task.id):
            await self.manager.cancel(task.id)
        else:
            self.workflow_store.update_task(
                task.id,
                status=TaskStatus.CANCELLED,
                paused=False,
                error="你在微信里拒绝了这个任务",
                finished_at=_now_iso(),
            )
        logger.info("微信端拒绝了任务 %s 的计划", task.id[:8])

    async def pause_task(self, task: Task) -> None:
        if task.is_terminal:
            raise BridgeError("这个任务已经结束了")
        if not self.manager.is_running(task.id):
            raise BridgeError("这个任务现在没在执行")
        await self.manager.pause(task.id)

    async def resume_task(self, task: Task) -> None:
        if task.is_terminal:
            raise BridgeError("这个任务已经结束了")
        if not self.manager.is_running(task.id):
            raise BridgeError("这个任务现在没在执行")
        await self.manager.resume(task.id)

    async def cancel_task(self, task: Task) -> None:
        if task.is_terminal:
            raise BridgeError("这个任务已经结束了")
        if self.manager.is_running(task.id):
            await self.manager.cancel(task.id)
        else:
            self.workflow_store.update_task(
                task.id,
                status=TaskStatus.CANCELLED,
                paused=False,
                error="你在微信里取消了这个任务",
                finished_at=_now_iso(),
            )
        logger.info("微信端取消了任务 %s", task.id[:8])

    # ------------------------------------------------------------------
    # 待授权动作
    # ------------------------------------------------------------------
    def current_action(self, task: Task) -> RemoteAction | None:
        """这个任务当前正在等的待授权动作，没有就返回 None。"""
        control = self.manager.control_of(task.id)
        action_id = str(getattr(control, "current_action_id", "") or "")
        if not action_id:
            return None
        action = self.pending_store.get(action_id)
        if action is None or getattr(action, "answered", False):
            return None
        # 防御：待办必须属于这个任务的会话，否则不暴露给微信
        if str(getattr(action, "session_id", "")) != task.session_id:
            logger.warning("任务 %s 的待办会话不匹配，已忽略", task.id[:8])
            return None
        return RemoteAction(
            action_id=action.id,
            kind=str(getattr(action, "kind", "confirm")),
            tool_label=str(getattr(action, "tool_label", "") or ""),
            summary=str(getattr(action, "summary", "") or ""),
            question=str(getattr(action, "question", "") or ""),
            options=list(getattr(action, "options", []) or []),
            session_id=action.session_id,
        )

    def answer_action(self, action_id: str, approved: bool) -> bool:
        action = self.pending_store.get(action_id)
        if action is None or getattr(action, "answered", False):
            return False
        result = self.pending_store.answer(action_id, bool(approved))
        return result is not None

    def choose_action(self, action_id: str, option_id: str) -> bool:
        action = self.pending_store.get(action_id)
        if action is None or getattr(action, "answered", False):
            return False
        return self.pending_store.choose(action_id, option_id) is not None

    # ------------------------------------------------------------------
    # 状态文本
    # ------------------------------------------------------------------
    def status_text(self, task: Task, *, with_steps: bool = True) -> str:
        short = task.id[:8]
        lines = [
            f"任务 {short}：{task.title or task.goal[:30]}",
            f"状态：{task.status.label}" + ("（已暂停）" if task.paused else ""),
        ]
        if task.steps:
            done = sum(1 for s in task.steps if s.status.is_terminal)
            lines.append(f"进度：{done}/{len(task.steps)} 步已结束")
        if with_steps and task.steps:
            for step in task.steps[:8]:
                lines.append(
                    f"  {step.step_index + 1}. [{step.status.label}] {step.goal[:60]}"
                )
            if len(task.steps) > 8:
                lines.append(f"  …还有 {len(task.steps) - 8} 步")
        if task.status is TaskStatus.FAILED and task.error:
            lines.append(f"原因：{task.error[:300]}")
        if task.status is TaskStatus.SUCCEEDED and task.summary:
            lines.append(f"结果：{task.summary[:600]}")
        return "\n".join(lines)


def error_text(exc: BaseException) -> str:
    """把已经结构化的业务错误转成能发给用户的一句话。

    只对 JiaAgentError 用它的 message；其他异常一律不把 str(exc) 发出去，
    避免路径、URL 查询串或密钥片段意外泄露。
    """
    if isinstance(exc, BridgeError):
        return str(exc)
    if isinstance(exc, JiaAgentError):
        return exc.message
    return "处理这条消息时出错了，详情见电脑端日志。"
