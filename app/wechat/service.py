"""微信消息的业务入口：鉴权、去重、命令路由、回复。

**顺序不能变**：

    解析（严格、失败关闭）
      -> 允许名单（精确 user_id + scope；不看昵称）
      -> 入站去重（原子 claim，重复投递不执行第二次）
      -> 速率限制
      -> 命令 / 普通聊天

未授权消息在允许名单那一步就结束，**不会**进入 Agent、工作流或任何工具。
普通聊天禁用工具；任务只能通过 `/task` 创建，并走现有工作流和 gate.py。

**关于并发**：长轮询线程不能被模型调用堵住。普通聊天放到后台任务里，
每个用户串行；命令（创建任务、批准、暂停）都是快速的状态操作，就地处理。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.wechat import protocol
from app.wechat.bridge import (
    BridgeError,
    JiaRuntimeBridge,
    error_text,
)
from app.wechat.errors import ProtocolError, RemoteApprovalError
from app.wechat.redact import redact_id, safe_exception
from app.wechat.remote_auth import (
    ACTION_CHOICE,
    ACTION_CONFIRM,
    ACTION_PLAN,
    RemoteAuth,
)
from app.wechat.store import WeChatStore
from app.wechat.task_watch import action_notice, plan_notice
from app.workflow.model import Task, TaskStatus

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 1_800
MAX_REPLY_CHUNKS = 4
_PREFIX = "（消息太长，后面的内容省略了）"


class Outbound(Protocol):
    async def send(
        self, *, to_user_id: str, text: str, context_token: str
    ) -> None:
        ...


class ApiOutbound:
    """把 Outbound 端口接到 WeChatApi.send_text 上。不重试发送。"""

    def __init__(self, api: Any) -> None:
        self.api = api

    async def send(
        self, *, to_user_id: str, text: str, context_token: str
    ) -> None:
        await self.api.send_text(
            to_user_id=to_user_id,
            text=text,
            context_token=context_token,
        )


class _Window:
    """简单的滑动窗口计数。只在内存里，重启即清零。"""

    def __init__(self, *, limit: int, window: float, clock: Callable[[], float]) -> None:
        self.limit = max(1, int(limit))
        self.window = max(1.0, float(window))
        self.clock = clock
        self._events: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = self.clock()
        bucket = self._events.setdefault(key, deque())
        while bucket and now - bucket[0] >= self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        # 顺手清掉已经空掉的 key，避免长期运行后无限增长
        if len(self._events) > 512:
            for stale in [k for k, v in self._events.items() if not v]:
                self._events.pop(stale, None)
        return True


def split_text(text: str) -> list[str]:
    """按微信单条消息的安全长度切分。优先在换行处切。"""
    clean = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not clean:
        return []
    chunks: list[str] = []
    current = ""
    for line in clean.split("\n"):
        while len(line) > MAX_REPLY_CHARS:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:MAX_REPLY_CHARS])
            line = line[MAX_REPLY_CHARS:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= MAX_REPLY_CHARS:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    if not chunks:
        return []
    if len(chunks) > MAX_REPLY_CHUNKS:
        chunks = chunks[:MAX_REPLY_CHUNKS]
        chunks[-1] = chunks[-1][: max(0, MAX_REPLY_CHARS - len(_PREFIX))] + _PREFIX
    return chunks


_CMD_ALIASES = {
    "task": "task", "任务": "task",
    "status": "status", "状态": "status",
    "tasks": "tasks", "任务列表": "tasks",
    "approve": "approve", "allow": "approve", "允许": "approve",
    "reject": "reject", "deny": "reject", "拒绝": "reject",
    "choose": "choose", "选择": "choose",
    "pause": "pause", "暂停": "pause",
    "resume": "resume", "继续": "resume",
    "cancel": "cancel", "取消": "cancel",
    "help": "help", "帮助": "help",
}

_CN_APPROVE = re.compile(r"^(允许|拒绝|选择)\s+(.+)$", re.DOTALL)


def _parse_command(text: str) -> tuple[str, str]:
    """返回 (标准命令名, 参数原文)。不是命令就返回空串。"""
    clean = str(text or "").strip()
    if not clean:
        return "", ""
    if clean.startswith("/"):
        body = clean[1:].strip()
        if not body:
            return "unknown", ""
        head, _, rest = body.partition(" ")
        return _CMD_ALIASES.get(head.casefold(), "unknown"), rest.strip()
    matched = _CN_APPROVE.match(clean)
    if matched:
        return _CMD_ALIASES.get(matched.group(1), ""), matched.group(2).strip()
    return "", ""


def _first_token(text: str) -> str:
    parts = str(text or "").split()
    return parts[0] if parts else ""


class WeChatService:
    def __init__(
        self,
        *,
        account_id: str,
        allowlist: Any,
        store: WeChatStore,
        bridge: JiaRuntimeBridge,
        remote_auth: RemoteAuth,
        outbound: Outbound,
        clock: Callable[[], float] = time.time,
        chat_concurrency: int = 2,
        max_queued_chats: int = 16,
        rate_limit: int = 30,
        rate_window: float = 60.0,
        approval_attempt_limit: int = 20,
        approval_attempt_window: float = 300.0,
        context_cache_size: int = 200,
    ) -> None:
        self.account_id = account_id
        self.allowlist = allowlist
        self.store = store
        self.bridge = bridge
        self.remote_auth = remote_auth
        self.outbound = outbound
        self.clock = clock

        if chat_concurrency < 1:
            raise ValueError("chat_concurrency 至少是 1")
        self.max_queued_chats = max(1, int(max_queued_chats))
        self._chat_sem = asyncio.Semaphore(int(chat_concurrency))
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

        self.rate = _Window(limit=rate_limit, window=rate_window, clock=clock)
        self.approval_rate = _Window(
            limit=approval_attempt_limit,
            window=approval_attempt_window,
            clock=clock,
        )
        self._rate_warned: dict[str, float] = {}

        # context_token 只留在内存：不发日志、不落库。重启后任务通知不带它，
        # 这是协议允许的（参见 docs/wechat.md 的已知限制）。
        self._context_tokens: OrderedDict[str, str] = OrderedDict()
        self._context_cache_size = max(1, int(context_cache_size))

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def stop(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_background(self) -> None:
        """等当前所有后台聊天结束。主要给测试和优雅停机用。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    async def handle_update(self, raw: Any) -> str:
        """处理 getUpdates 里的一条原始消息。

        返回值只用于日志/测试：parse_error / unauthorized / duplicate /
        rate_limited / chat / command / error。
        """
        try:
            message = protocol.parse_inbound_message(raw)
        except ProtocolError as exc:
            logger.info("忽略不支持的微信消息：%s", exc)
            return "parse_error"

        from_id = message.from_user_id
        if not self.allowlist.is_allowed(self.account_id, from_id, "chat"):
            logger.warning("拒绝未配对的微信发送者 %s", redact_id(from_id))
            return "unauthorized"

        now = self.clock()
        claimed = self.store.claim_inbound(
            message_key=message.message_key,
            account_id=self.account_id,
            from_user_id=from_id,
            now=now,
        )
        if not claimed:
            logger.info("忽略重复投递的微信消息（发送者 %s）", redact_id(from_id))
            return "duplicate"

        if message.context_token:
            key = self._context_key(from_id)
            self._context_tokens[key] = message.context_token
            self._context_tokens.move_to_end(key)
            while len(self._context_tokens) > self._context_cache_size:
                self._context_tokens.popitem(last=False)

        if not self.rate.allow(from_id):
            logger.warning("微信发送者 %s 触发了速率限制", redact_id(from_id))
            last = self._rate_warned.get(from_id, 0.0)
            if now - last >= 60.0:
                self._rate_warned[from_id] = now
                await self._send_safe(
                    from_id, "消息太频繁了，请稍后再试。", message.context_token
                )
            self.store.finish_inbound(
                message_key=message.message_key, state="ignored", now=now,
                detail="rate_limited",
            )
            return "rate_limited"

        command, arg_text = _parse_command(message.text)
        if command:
            await self._handle_command(
                command, arg_text, from_id, message.context_token,
                message.message_key,
            )
            return "command"

        if len(self._tasks) >= self.max_queued_chats:
            await self._send_safe(
                from_id, "排队等待处理的消息太多了，请稍后再发。",
                message.context_token,
            )
            self.store.finish_inbound(
                message_key=message.message_key, state="ignored", now=now,
                detail="chat_queue_full",
            )
            return "rate_limited"

        task = asyncio.create_task(self._chat_job(
            from_id, message.text, message.context_token, message.message_key
        ))
        self._track(task)
        return "chat"

    # ------------------------------------------------------------------
    # 普通聊天（后台）
    # ------------------------------------------------------------------
    async def _chat_job(
        self, user_id: str, text: str, context_token: str, message_key: str
    ) -> None:
        try:
            async with self._chat_sem:
                lock = self._user_locks.setdefault(user_id, asyncio.Lock())
                async with lock:
                    try:
                        reply = await self.bridge.chat(self.account_id, user_id, text)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "微信聊天失败（发送者 %s）：%s",
                            redact_id(user_id), safe_exception(exc),
                        )
                        await self._send_safe(
                            user_id, error_text(exc), context_token
                        )
                        self.store.finish_inbound(
                            message_key=message_key, state="failed",
                            now=self.clock(), detail="chat_error",
                        )
                        return
                    await self._send_safe(user_id, reply, context_token)
                    self.store.finish_inbound(
                        message_key=message_key, state="done", now=self.clock()
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("微信聊天后台任务出错（发送者已脱敏）")
            self.store.finish_inbound(
                message_key=message_key, state="failed", now=self.clock(),
                detail="internal_error",
            )

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    async def _handle_command(
        self,
        command: str,
        arg_text: str,
        user_id: str,
        context_token: str,
        message_key: str,
    ) -> None:
        detail = "ok"
        try:
            if command == "help":
                await self._help(user_id, context_token)
            elif command == "task":
                if not self._scope(user_id, "task"):
                    await self._no_scope(user_id, context_token)
                else:
                    await self._cmd_task(user_id, arg_text, context_token)
            elif command == "status":
                await self._cmd_status(user_id, arg_text, context_token)
            elif command == "tasks":
                await self._cmd_tasks(user_id, context_token)
            elif command == "pause":
                if not self._scope(user_id, "task"):
                    await self._no_scope(user_id, context_token)
                else:
                    await self._cmd_control("pause", user_id, arg_text, context_token)
            elif command == "resume":
                if not self._scope(user_id, "task"):
                    await self._no_scope(user_id, context_token)
                else:
                    await self._cmd_control("resume", user_id, arg_text, context_token)
            elif command == "cancel":
                if not self._scope(user_id, "task"):
                    await self._no_scope(user_id, context_token)
                else:
                    await self._cmd_control("cancel", user_id, arg_text, context_token)
            elif command == "approve":
                await self._cmd_approve(user_id, arg_text, context_token)
            elif command == "reject":
                await self._cmd_reject(user_id, arg_text, context_token)
            elif command == "choose":
                await self._cmd_choose(user_id, arg_text, context_token)
            else:
                await self._help(user_id, context_token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            detail = "command_error"
            logger.warning(
                "处理微信命令失败（用户 %s）：%s",
                redact_id(user_id), safe_exception(exc),
            )
            await self._send_safe(user_id, error_text(exc), context_token)
        finally:
            self.store.finish_inbound(
                message_key=message_key,
                state="failed" if detail == "command_error" else "done",
                now=self.clock(),
                detail=detail,
            )

    def _scope(self, user_id: str, scope: str) -> bool:
        return self.allowlist.is_allowed(self.account_id, user_id, scope)

    async def _no_scope(self, user_id: str, context_token: str) -> None:
        await self._send_safe(
            user_id, "这个微信账号没有被授予这项权限。", context_token
        )

    async def _resolve_or_say(
        self, user_id: str, arg_text: str, context_token: str
    ) -> Task | None:
        prefix = _first_token(arg_text)
        try:
            task = self.bridge.resolve_task(self.account_id, user_id, prefix)
        except BridgeError as exc:
            await self._send_safe(user_id, str(exc), context_token)
            return None
        if task is None:
            suffix = f" {prefix}" if prefix else ""
            await self._send_safe(
                user_id, f"找不到任务{suffix}。发送 /tasks 查看最近任务。",
                context_token,
            )
            return None
        return task

    async def _cmd_task(
        self, user_id: str, arg_text: str, context_token: str
    ) -> None:
        goal = str(arg_text or "").strip()
        if not goal:
            await self._send_safe(
                user_id, "用法：/task 要做的事（用你自己的话说）", context_token
            )
            return
        if len(goal) > 4_000:
            await self._send_safe(user_id, "任务描述太长了，请压缩到 4000 字以内。", context_token)
            return
        task = await self.bridge.create_task(self.account_id, user_id, goal)
        await self._send_safe(
            user_id,
            f"任务 {task.id[:8]} 已收到，正在拆计划。\n"
            f"拆好后我会把计划和一次性授权码发给你。\n"
            f"查看进度：/status {task.id[:8]}",
            context_token,
        )

    async def _cmd_status(
        self, user_id: str, arg_text: str, context_token: str
    ) -> None:
        task = await self._resolve_or_say(user_id, arg_text, context_token)
        if task is None:
            return
        text = self.bridge.status_text(task)
        action = None if task.is_terminal else self.bridge.current_action(task)
        can_approve = self._scope(user_id, "approve")
        if task.status is TaskStatus.PENDING and task.steps:
            if can_approve:
                code, _ = self.remote_auth.issue(
                    account_id=self.account_id,
                    user_id=user_id,
                    task_id=task.id,
                    action_type=ACTION_PLAN,
                    now=self.clock(),
                )
                text += "\n\n" + plan_notice(task, code)
                text += "\n（每次查看待批准任务都会发一个新码，旧码随即失效。）"
            else:
                text += (
                    "\n\n当前微信配对没有 approve 权限，不能在这里批准。"
                    "请到电脑端处理；任务不会因此被取消。"
                )
        elif action is not None:
            if can_approve:
                action_type = (
                    ACTION_CHOICE if action.kind == "choose" else ACTION_CONFIRM
                )
                code, _ = self.remote_auth.issue(
                    account_id=self.account_id,
                    user_id=user_id,
                    task_id=task.id,
                    action_type=action_type,
                    action_id=action.action_id,
                    now=self.clock(),
                )
                text += "\n\n" + action_notice(task, action, code)
                text += "\n（每次查看都会发一个新码，旧码随即失效。）"
            else:
                text += (
                    "\n\n当前微信配对没有 approve 权限，不能在这里批准。"
                    "请到电脑端处理；任务不会因此被取消。"
                )
        await self._send_safe(user_id, text, context_token)

    async def _cmd_tasks(self, user_id: str, context_token: str) -> None:
        tasks = self.bridge.list_linked_tasks(self.account_id, user_id, limit=15)
        if not tasks:
            await self._send_safe(
                user_id, "还没有任务。用 /task 要做的事 来派一个。", context_token
            )
            return
        lines = ["最近的任务："]
        for task in tasks:
            paused = "（暂停）" if task.paused else ""
            lines.append(
                f"{task.id[:8]}  [{task.status.label}]{paused}  "
                f"{(task.title or task.goal)[:40]}"
            )
        lines.append("\n查看详情：/status 任务号")
        await self._send_safe(user_id, "\n".join(lines), context_token)

    async def _cmd_control(
        self, action: str, user_id: str, arg_text: str, context_token: str
    ) -> None:
        task = await self._resolve_or_say(user_id, arg_text, context_token)
        if task is None:
            return
        if action == "pause":
            await self.bridge.pause_task(task)
            await self._send_safe(
                user_id,
                f"任务 {task.id[:8]} 收到暂停请求，会在当前这一步做完后生效。",
                context_token,
            )
        elif action == "resume":
            await self.bridge.resume_task(task)
            await self._send_safe(
                user_id, f"任务 {task.id[:8]} 已恢复。", context_token
            )
        else:
            await self.bridge.cancel_task(task)
            # 取消成功后立刻撤销这个任务还没用掉的计划/确认码。
            # 不能等 TaskWatcher 下一轮扫到终态才撤销：那段时间里用户
            # 手里可能还有可用码，旧消息也可能被翻出来。
            self.remote_auth.revoke_task(task.id, self.clock())
            await self._send_safe(
                user_id,
                f"任务 {task.id[:8]} 已取消。"
                "未开始的步骤不会再执行；如果取消时某一步正在运行，"
                "底层操作无法保证立即中断。",
                context_token,
            )

    # ------------------------------------------------------------------
    # 远程授权
    # ------------------------------------------------------------------
    def _approval_attempt_allowed(self, user_id: str) -> bool:
        return self.approval_rate.allow(user_id)

    async def _approval_failed(
        self, user_id: str, context_token: str, *, throttle: bool = True
    ) -> None:
        if throttle:
            message = "授权码无效、已过期或已经用过。发送 /status 任务号 可以获取新码。"
        else:
            message = "尝试次数太多了，请过几分钟再试。"
        await self._send_safe(user_id, message, context_token)

    async def _cmd_approve(
        self, user_id: str, arg_text: str, context_token: str
    ) -> None:
        if not self._scope(user_id, "approve"):
            await self._no_scope(user_id, context_token)
            return
        if not self._approval_attempt_allowed(user_id):
            await self._approval_failed(user_id, context_token, throttle=False)
            return
        code = _first_token(arg_text)
        if not code:
            await self._send_safe(
                user_id, "用法：/approve 授权码", context_token
            )
            return
        try:
            binding = self.remote_auth.resolve(
                code, account_id=self.account_id, user_id=user_id,
                now=self.clock(),
            )
        except RemoteApprovalError:
            await self._approval_failed(user_id, context_token)
            return

        task = self.bridge.get_task(binding.task_id)
        if task is None or not self.bridge.is_linked(
            binding.task_id, self.account_id, user_id
        ):
            await self._approval_failed(user_id, context_token)
            return

        if binding.action_type == ACTION_CHOICE:
            await self._send_safe(
                user_id,
                "这个授权需要用 /choose 授权码 序号 来选。",
                context_token,
            )
            return

        try:
            self.remote_auth.consume(
                code, account_id=self.account_id, user_id=user_id,
                binding=binding, now=self.clock(),
            )
        except RemoteApprovalError:
            await self._approval_failed(user_id, context_token)
            return

        if binding.action_type == ACTION_PLAN:
            if task.status is not TaskStatus.PENDING:
                await self._send_safe(
                    user_id,
                    f"任务 {task.id[:8]} 的计划已经不在等待批准（当前：{task.status.label}）。",
                    context_token,
                )
                return
            await self.bridge.approve_plan(task)
            await self._send_safe(
                user_id,
                f"任务 {task.id[:8]} 已批准，开始执行。\n"
                f"查看进度：/status {task.id[:8]}",
                context_token,
            )
            return

        action = self.bridge.current_action(task)
        if (
            action is None
            or action.action_id != binding.action_id
            or action.kind != "confirm"
        ):
            await self._send_safe(
                user_id,
                "这次确认已经失效（任务状态或待确认的操作变了）。",
                context_token,
            )
            return
        ok = self.bridge.answer_action(action.action_id, True)
        if not ok:
            await self._send_safe(
                user_id, "这个操作已经处理过了，没有重复执行。", context_token
            )
            return
        await self._send_safe(
            user_id,
            f"已允许任务 {task.id[:8]} 的这一步。\n"
            f"操作：{action.summary[:200]}\n"
            "真正执行前仍会经过原有闸门。",
            context_token,
        )

    async def _cmd_reject(
        self, user_id: str, arg_text: str, context_token: str
    ) -> None:
        if not self._scope(user_id, "approve"):
            await self._no_scope(user_id, context_token)
            return
        if not self._approval_attempt_allowed(user_id):
            await self._approval_failed(user_id, context_token, throttle=False)
            return
        code = _first_token(arg_text)
        if not code:
            await self._send_safe(
                user_id, "用法：/reject 授权码", context_token
            )
            return
        try:
            binding = self.remote_auth.resolve(
                code, account_id=self.account_id, user_id=user_id,
                now=self.clock(),
            )
        except RemoteApprovalError:
            await self._approval_failed(user_id, context_token)
            return
        task = self.bridge.get_task(binding.task_id)
        if task is None or not self.bridge.is_linked(
            binding.task_id, self.account_id, user_id
        ):
            await self._approval_failed(user_id, context_token)
            return
        try:
            self.remote_auth.consume(
                code, account_id=self.account_id, user_id=user_id,
                binding=binding, now=self.clock(),
            )
        except RemoteApprovalError:
            await self._approval_failed(user_id, context_token)
            return

        if binding.action_type == ACTION_PLAN:
            await self.bridge.reject_plan(task)
            # 拒绝计划等于取消任务，和其他取消路径一样立刻作废所有未用码。
            self.remote_auth.revoke_task(task.id, self.clock())
            await self._send_safe(
                user_id, f"任务 {task.id[:8]} 已拒绝并取消。", context_token
            )
            return

        action = self.bridge.current_action(task)
        if action is None or action.action_id != binding.action_id:
            await self._send_safe(
                user_id, "这次确认已经失效（任务状态变了）。", context_token
            )
            return
        self.bridge.answer_action(action.action_id, False)
        await self._send_safe(
            user_id,
            f"已拒绝任务 {task.id[:8]} 的这一步，不会执行。",
            context_token,
        )

    async def _cmd_choose(
        self, user_id: str, arg_text: str, context_token: str
    ) -> None:
        if not self._scope(user_id, "approve"):
            await self._no_scope(user_id, context_token)
            return
        if not self._approval_attempt_allowed(user_id):
            await self._approval_failed(user_id, context_token, throttle=False)
            return
        parts = str(arg_text or "").split()
        if len(parts) < 2:
            await self._send_safe(
                user_id, "用法：/choose 授权码 序号", context_token
            )
            return
        code, index_text = parts[0], parts[1]
        if not index_text.isdigit():
            await self._send_safe(
                user_id, "序号必须是数字，例如 /choose ABCDE-FGHJK 1", context_token
            )
            return
        index = int(index_text)
        try:
            binding = self.remote_auth.resolve(
                code, account_id=self.account_id, user_id=user_id,
                now=self.clock(),
            )
        except RemoteApprovalError:
            await self._approval_failed(user_id, context_token)
            return
        if binding.action_type != ACTION_CHOICE:
            await self._send_safe(
                user_id, "这个授权不是选择题，请用 /approve 或 /reject。",
                context_token,
            )
            return
        task = self.bridge.get_task(binding.task_id)
        if task is None or not self.bridge.is_linked(
            binding.task_id, self.account_id, user_id
        ):
            await self._approval_failed(user_id, context_token)
            return
        action = self.bridge.current_action(task)
        if (
            action is None
            or action.action_id != binding.action_id
            or action.kind != "choose"
        ):
            await self._send_safe(
                user_id, "这次选择已经失效（任务状态变了）。", context_token
            )
            return
        if not 1 <= index <= len(action.options):
            await self._send_safe(
                user_id,
                f"序号超出范围，应该是 1 到 {len(action.options)}。",
                context_token,
            )
            return
        option = action.options[index - 1]
        option_id = str(option.get("id", ""))
        if not option_id:
            await self._send_safe(
                user_id, "这个候选项没有有效编号，请在电脑端选择。", context_token
            )
            return
        try:
            self.remote_auth.consume(
                code, account_id=self.account_id, user_id=user_id,
                binding=binding, now=self.clock(),
            )
        except RemoteApprovalError:
            await self._approval_failed(user_id, context_token)
            return
        if not self.bridge.choose_action(action.action_id, option_id):
            await self._send_safe(
                user_id, "这个选择已经处理过了，没有重复执行。", context_token
            )
            return
        label = (
            option.get("name") or option.get("label") or option.get("title")
            or option.get("target") or option_id
        )
        await self._send_safe(
            user_id,
            f"已为任务 {task.id[:8]} 选择了：{str(label)[:120]}\n"
            "真正执行前仍会经过原有闸门。",
            context_token,
        )

    # ------------------------------------------------------------------
    # 帮助
    # ------------------------------------------------------------------
    async def _help(self, user_id: str, context_token: str) -> None:
        text = (
            "我是佳。微信入口第一版只支持文字。\n\n"
            "/task 要做的事 — 派一个任务\n"
            "/status [任务号] — 查看进度；待批准时会发新的一次性授权码\n"
            "/tasks — 最近任务\n"
            "/pause /resume /cancel [任务号] — 控制任务\n"
            "/approve 授权码 — 批准计划或允许一步\n"
            "/reject 授权码 — 拒绝\n"
            "/choose 授权码 序号 — 从候选里选一个\n"
            "/help — 这条帮助\n\n"
            "安全边界：只有电脑端显式配对的账号能用；普通聊天不会执行工具；"
            "任务和电脑操作仍走 Jia 原有工作流和授权闸门。"
        )
        await self._send_safe(user_id, text, context_token)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def _context_key(self, user_id: str) -> str:
        return f"{self.account_id}\x00{user_id}"

    async def notify(self, account_id: str, user_id: str, text: str) -> bool:
        """任务通知的统一出口。

        返回 False 表示“可重试的发送失败”；返回 True 表示已经处理完
        （包括主动丢弃）。账号不匹配或没有当前有效的 task 授权时，这是
        永久性拒绝，必须返回 True，否则 TaskWatcher 会每两秒重试到永远。
        """
        if account_id != self.account_id:
            logger.warning("拒绝跨账号发送任务通知")
            return True
        if not self._scope(user_id, "task"):
            logger.info(
                "丢弃发给未授权用户的任务通知（用户 %s）", redact_id(user_id)
            )
            return True
        context_token = self._context_tokens.get(self._context_key(user_id), "")
        try:
            await self.outbound.send(
                to_user_id=user_id, text=text[:MAX_REPLY_CHARS],
                context_token=context_token,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "发送微信任务通知失败（用户 %s）：%s",
                redact_id(user_id), safe_exception(exc),
            )
            return False

    async def _send_safe(
        self, user_id: str, text: str, context_token: str
    ) -> None:
        """发一条用户可见的回复。发送失败只记日志，不让整个流程崩。

        不在失败后重发：sendmessage 没有可验证的幂等语义，重发可能让用户
        收到两条一样的消息。宁可少一条，也不重复。
        """
        # 统一出口复核 chat 权限：命令处理到一半时用户被 unpair，在途回复
        # 也不能再发出去。这里不复用 notify() 的 task 检查：普通聊天只需要 chat。
        if not self._scope(user_id, "chat"):
            logger.info(
                "丢弃发给已取消配对用户的回复（用户 %s）", redact_id(user_id)
            )
            return
        chunks = split_text(text)
        for chunk in chunks:
            try:
                await self.outbound.send(
                    to_user_id=user_id, text=chunk,
                    context_token=context_token,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "发送微信回复失败（用户 %s）：%s",
                    redact_id(user_id), safe_exception(exc),
                )
                return

    # ------------------------------------------------------------------
    # 后台任务登记
    # ------------------------------------------------------------------
    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)

        def _done(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.error(
                    "微信后台任务异常结束：%s", safe_exception(exc)
                )

        task.add_done_callback(_done)
