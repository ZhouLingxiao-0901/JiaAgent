"""把任务状态和授权请求翻译成微信消息。

**为什么要轮询**：工作流本来就设计成不绑在一次 HTTP 请求上，网页也是轮询
（见 app/api/tasks.py 开头）。微信这边用同一套事实来源（workflow store），
不另造事件总线，也不需要侵入现有引擎。

**不去执行任何东西**：这里只读任务状态、发消息、生成授权码。用户回
`/approve` 之后，真正动手的是 service → bridge → manager/pending.store，
工具执行仍然要过 gate.py。

**不重复发**：每个任务在 task_links 里记录通知进度：
  * plan_notified —— 计划消息只发一次；
  * notified_action_id —— 每个待授权动作只发一次；
  * last_status —— 状态变化才发；
  * terminal_notified —— 终态只发一次。
发送失败（notifier 返回 False）时不更新进度，下一轮再试。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.wechat.bridge import JiaRuntimeBridge, RemoteAction
from app.wechat.remote_auth import (
    ACTION_CHOICE,
    ACTION_CONFIRM,
    ACTION_PLAN,
    RemoteAuth,
    format_code,
)
from app.wechat.store import WeChatStore
from app.workflow.model import Task, TaskStatus

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 2.0
MAX_LINK_SCAN = 100
MAX_MESSAGE_CHARS = 1_800

Notifier = Callable[[str, str, str], Awaitable[bool]]


def _clip(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    clean = str(text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


def plan_notice(task: Task, code: str) -> str:
    lines = [
        f"任务 {task.id[:8]} 的计划（{len(task.steps)} 步）：",
    ]
    for index, step in enumerate(task.steps[:10], 1):
        suffix = f"（{step.tool}）" if step.tool else ""
        lines.append(f"{index}. {step.goal[:80]}{suffix}")
    if len(task.steps) > 10:
        lines.append(f"…还有 {len(task.steps) - 10} 步")
    lines.extend([
        "",
        "批准后才会开始执行。",
        f"批准：/approve {format_code(code)}",
        f"拒绝：/reject {format_code(code)}",
        "授权码 10 分钟内有效，只能用一次。",
    ])
    return _clip("\n".join(lines))


def action_notice(task: Task, action: RemoteAction, code: str) -> str:
    shown = format_code(code)
    if action.kind == "choose":
        lines = [
            f"任务 {task.id[:8]} 需要你选一个：",
            action.question or action.summary,
        ]
        for index, option in enumerate(action.options[:8], 1):
            label = (
                option.get("name")
                or option.get("label")
                or option.get("title")
                or option.get("target")
                or option.get("id")
                or "（未命名）"
            )
            lines.append(f"{index}. {str(label)[:70]}")
        lines.extend([
            "",
            f"选择：/choose {shown} 序号（例如 /choose {shown} 1）",
            f"拒绝：/reject {shown}",
            "授权码 10 分钟内有效，只能用一次。",
        ])
        return _clip("\n".join(lines))

    lines = [
        f"任务 {task.id[:8]} 需要你确认这一步：",
        action.summary or action.tool_label or "（没有摘要）",
        "",
        "确认只代表允许这一步，执行前仍会经过原有闸门。",
        f"允许：/approve {shown}",
        f"拒绝：/reject {shown}",
        "授权码 10 分钟内有效，只能用一次。",
    ]
    return _clip("\n".join(lines))


def plan_without_approval_notice(task: Task) -> str:
    return _clip(
        f"任务 {task.id[:8]} 的计划已生成，但当前微信配对没有 approve 权限，"
        "不能在这里批准。\n请到电脑端查看并确认；任务不会因此被取消。"
    )


def action_without_approval_notice(task: Task, action: RemoteAction) -> str:
    summary = action.summary or action.tool_label or "（没有摘要）"
    return _clip(
        f"任务 {task.id[:8]} 有一步需要确认：\n{summary[:300]}\n"
        "当前微信配对没有 approve 权限，不能在这里批准。\n"
        "请到电脑端处理；任务不会因此被取消。"
    )


def status_notice(task: Task) -> str:
    if task.paused:
        return f"任务 {task.id[:8]} 已暂停（当前这一步做完后生效）。\n恢复：/resume {task.id[:8]}"
    if task.status is TaskStatus.RUNNING:
        return f"任务 {task.id[:8]} 开始执行了。\n查看进度：/status {task.id[:8]}"
    if task.status is TaskStatus.PENDING:
        return f"任务 {task.id[:8]} 还在准备中。\n查看进度：/status {task.id[:8]}"
    return f"任务 {task.id[:8]}：{task.status.label}"


def terminal_notice(task: Task) -> str:
    short = task.id[:8]
    if task.status is TaskStatus.SUCCEEDED:
        body = task.summary or task.title or task.goal
        return _clip(f"任务 {short} 完成了。\n{body[:600]}")
    if task.status is TaskStatus.CANCELLED:
        return f"任务 {short} 已取消。"
    reason = task.error or task.summary or "没有给出原因"
    return _clip(f"任务 {short} 失败了。\n{reason[:600]}")


class TaskWatcher:
    def __init__(
        self,
        *,
        bridge: JiaRuntimeBridge,
        store: WeChatStore,
        remote_auth: RemoteAuth,
        notifier: Notifier,
        allowlist: Any,
        is_connected: Callable[[], bool] | None = None,
        interval: float = DEFAULT_INTERVAL,
        clock: Callable[[], float] = time.time,
        scan_limit: int = MAX_LINK_SCAN,
    ) -> None:
        self.bridge = bridge
        self.store = store
        self.remote_auth = remote_auth
        self.notifier = notifier
        self.allowlist = allowlist
        self.is_connected = is_connected or (lambda: True)
        self.interval = max(0.2, float(interval))
        self.clock = clock
        self.scan_limit = max(1, int(scan_limit))
        self._send_failures = 0
        self._send_resume_at = 0.0
        self._last_failure_log = 0.0

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 通知失败不能拖垮整个监听
                logger.exception("微信任务通知轮询出错")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> int:
        """扫一遍。返回本轮发出的消息条数（测试和日志用）。"""
        sent = 0
        now = self.clock()
        # 监听断线/暂停期间不发任务通知，避免对已失败的连接反复重试。
        if not self.is_connected():
            return 0
        # 连续发送失败后的退避窗口：不扫、不发、不刷日志。
        if now < self._send_resume_at:
            return 0
        for link in self.store.list_watchable_links(limit=self.scan_limit):
            task_id = str(link.get("task_id", ""))
            account_id = str(link.get("account_id", ""))
            user_id = str(link.get("user_id", ""))
            if not task_id or not account_id or not user_id:
                continue
            # 统一出口的第一道检查：取消失败/取消配对后不再发送任何任务内容。
            # 撤销未使用授权码，但保留 task_links（不静默删除任务归属）。
            if not self.allowlist.is_allowed(account_id, user_id, "task"):
                self.remote_auth.revoke_task(task_id, now)
                continue
            task = self.bridge.get_task(task_id)
            if task is None:
                continue

            if task.is_terminal:
                if int(link.get("terminal_notified") or 0):
                    continue
                self.remote_auth.revoke_task(task_id, now)
                ok = await self._send(account_id, user_id, terminal_notice(task))
                if ok:
                    self.store.update_task_link(
                        task_id, now=now, terminal_notified=1,
                        last_status=task.status.value,
                    )
                    sent += 1
                continue

            if (
                task.status is TaskStatus.PENDING
                and task.steps
                and not int(link.get("plan_notified") or 0)
            ):
                # 只有 approve 才能拿到可用的授权码；没有 approve 时仍可
                # 收到“有计划”的提示，但绝不下发码，也不能在这里批准。
                if not self.allowlist.is_allowed(account_id, user_id, "approve"):
                    self.remote_auth.revoke_task(task_id, now)
                    key = "pending:no_approve"
                    if str(link.get("last_status") or "") != key:
                        ok = await self._send(
                            account_id, user_id,
                            plan_without_approval_notice(task),
                        )
                        if ok:
                            self.store.update_task_link(
                                task_id, now=now, last_status=key
                            )
                            sent += 1
                    continue
                code, _binding = self.remote_auth.issue(
                    account_id=account_id,
                    user_id=user_id,
                    task_id=task_id,
                    action_type=ACTION_PLAN,
                    now=now,
                )
                ok = await self._send(account_id, user_id, plan_notice(task, code))
                if ok:
                    self.store.update_task_link(
                        task_id, now=now, plan_notified=1,
                        last_status=task.status.value,
                    )
                    sent += 1
                continue

            action = self.bridge.current_action(task)
            if action is not None:
                if str(link.get("notified_action_id") or "") == action.action_id:
                    continue
                if not self.allowlist.is_allowed(account_id, user_id, "approve"):
                    self.remote_auth.revoke_task(task_id, now)
                    key = f"action_no_approve:{action.action_id}"
                    if str(link.get("last_status") or "") != key:
                        ok = await self._send(
                            account_id, user_id,
                            action_without_approval_notice(task, action),
                        )
                        if ok:
                            self.store.update_task_link(
                                task_id, now=now, last_status=key
                            )
                            sent += 1
                    continue
                action_type = (
                    ACTION_CHOICE if action.kind == "choose" else ACTION_CONFIRM
                )
                code, _binding = self.remote_auth.issue(
                    account_id=account_id,
                    user_id=user_id,
                    task_id=task_id,
                    action_type=action_type,
                    action_id=action.action_id,
                    now=now,
                )
                ok = await self._send(
                    account_id, user_id, action_notice(task, action, code)
                )
                if ok:
                    self.store.update_task_link(
                        task_id, now=now,
                        notified_action_id=action.action_id,
                        last_status=TaskStatus.WAITING_CONFIRMATION.value,
                    )
                    sent += 1
                continue

            # 没有待授权动作时，只在状态/暂停位变化时发一条。
            status_key = task.status.value + (":paused" if task.paused else "")
            if status_key != str(link.get("last_status") or ""):
                if task.status is TaskStatus.WAITING_CONFIRMATION:
                    # 待办刚登记、还没拿到 action_id 的短暂窗口。下一轮再说，
                    # 避免发一条“等待确认”却没有任何可操作码的消息。
                    continue
                ok = await self._send(account_id, user_id, status_notice(task))
                if ok:
                    self.store.update_task_link(
                        task_id, now=now, last_status=status_key
                    )
                    sent += 1
        return sent

    async def _send(self, account_id: str, user_id: str, text: str) -> bool:
        try:
            ok = bool(await self.notifier(account_id, user_id, text))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            self._send_failures = 0
            self._send_resume_at = 0.0
            return True
        self._note_send_failure()
        return False

    def _note_send_failure(self) -> None:
        self._send_failures += 1
        if self._send_failures < 3:
            return
        delay = min(60.0, 2.0 ** min(self._send_failures - 3, 5))
        now = self.clock()
        self._send_resume_at = now + delay
        # 断网时不要每个 tick 都刷日志；最多每 60 秒一条。
        if now - self._last_failure_log >= 60.0:
            self._last_failure_log = now
            logger.warning(
                "微信任务通知连续发送失败，已退避 %.0f 秒（已脱敏）", delay
            )
