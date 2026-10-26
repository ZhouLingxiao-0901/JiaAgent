"""微信端的一次性远程授权码。

**它解决什么问题**：任务在电脑上跑，用户可能不在电脑前。工作流等计划批准
或工具确认时，微信要能把“批准/拒绝”送回去。但“微信发一句话就能批准”不能
变成第二个绕过 gate.py 的入口。

所以这里的授权码是：

  * **绑定** account_id + user_id + task_id + action_type + action_id；
  * **一次性**：消费后立刻失效，并发的第二次消费必须失败；
  * **有时限**：默认 10 分钟；
  * **不可伪造**：正文从不落库，只存 HMAC-SHA256 指纹；
  * **只批准不执行**：消费成功后，调用方只能去调 `manager.approve()` 或
    `pending.store.answer()/choose()`，绝不能直接调用工具。

真正的工具执行仍然只发生在 app/agent/gate.py 放行之后。这个模块不 import
任何工具，也没有执行能力。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.wechat.errors import RemoteApprovalError
from app.wechat.store import WeChatStore

# 去掉 0/O、1/I 这类容易看错的字符。10 位约 50 bit，配合“一次性 + 10 分钟
# + 账号/任务绑定 + 尝试次数限制”足够。
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 10
DEFAULT_TTL_SECONDS = 600.0

ACTION_PLAN = "plan"
ACTION_CONFIRM = "confirm"
ACTION_CHOICE = "choice"
_VALID_ACTIONS = frozenset({ACTION_PLAN, ACTION_CONFIRM, ACTION_CHOICE})


def format_code(code: str) -> str:
    """显示用：前 5 位和后 5 位之间加一个连字符。"""
    raw = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    if len(raw) != CODE_LENGTH:
        return raw
    return f"{raw[:5]}-{raw[5:]}"


def normalize_code(code: str) -> str:
    raw = "".join(
        ch for ch in str(code or "").upper() if ch.isalnum()
    )
    if len(raw) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in raw):
        raise RemoteApprovalError("授权码无效、已过期或不属于这个操作")
    return raw


@dataclass(frozen=True)
class ApprovalBinding:
    approval_id: str
    task_id: str
    action_type: str
    action_id: str
    account_id: str
    user_id: str
    expires_at: float


class RemoteAuth:
    def __init__(
        self,
        store: WeChatStore,
        secret: bytes,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("远程授权密钥至少 32 字节")
        self.store = store
        self._secret = bytes(secret)
        self.ttl_seconds = float(ttl_seconds)
        self.clock = clock

    def _hash(self, code: str) -> str:
        return hmac.new(
            self._secret, b"jia-wechat-approval\0" + code.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def _new_code(self) -> str:
        return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))

    def issue(
        self,
        *,
        account_id: str,
        user_id: str,
        task_id: str,
        action_type: str,
        action_id: str = "",
        now: float | None = None,
    ) -> tuple[str, ApprovalBinding]:
        if action_type not in _VALID_ACTIONS:
            raise ValueError("未知的授权类型")
        if not task_id:
            raise ValueError("授权必须绑定任务")
        if action_type in (ACTION_CONFIRM, ACTION_CHOICE) and not action_id:
            raise ValueError("确认/选择授权必须绑定具体动作")
        if action_type == ACTION_PLAN and action_id:
            raise ValueError("计划授权不绑定具体动作")

        current = self.clock() if now is None else float(now)
        expires_at = current + self.ttl_seconds
        for _ in range(5):
            code = self._new_code()
            digest = self._hash(code)
            try:
                approval_id = self.store.create_approval(
                    code_hash=digest,
                    account_id=account_id,
                    user_id=user_id,
                    task_id=task_id,
                    action_type=action_type,
                    action_id=action_id,
                    now=current,
                    expires_at=expires_at,
                )
            except sqlite3.IntegrityError:
                # 只可能是 code_hash 唯一键碰撞；换一个码重试。
                # 其他数据库错误必须冒泡，不能假装“再试一次”就吞掉。
                continue
            return code, ApprovalBinding(
                approval_id=approval_id,
                task_id=task_id,
                action_type=action_type,
                action_id=action_id,
                account_id=account_id,
                user_id=user_id,
                expires_at=expires_at,
            )
        raise RemoteApprovalError("暂时无法生成授权码，请稍后再试")

    def resolve(
        self, code: str, *, account_id: str, user_id: str, now: float | None = None
    ) -> ApprovalBinding:
        """查这个码属于哪个绑定。**不消费**，用于先确认任务/动作。"""
        raw = normalize_code(code)
        current = self.clock() if now is None else float(now)
        row = self.store.find_approval_by_hash(
            self._hash(raw), account_id=account_id, user_id=user_id, now=current
        )
        if row is None:
            raise RemoteApprovalError("授权码无效、已过期或不属于这个操作")
        return _row_to_binding(row)

    def consume(
        self,
        code: str,
        *,
        account_id: str,
        user_id: str,
        binding: ApprovalBinding,
        now: float | None = None,
    ) -> ApprovalBinding:
        """原子消费。必须和 `resolve()` 得到的绑定完全一致。"""
        raw = normalize_code(code)
        current = self.clock() if now is None else float(now)
        row = self.store.consume_approval(
            code_hash=self._hash(raw),
            account_id=account_id,
            user_id=user_id,
            task_id=binding.task_id,
            action_type=binding.action_type,
            action_id=binding.action_id,
            now=current,
        )
        if row is None:
            # 过期、已用、或者在这两步之间被换掉/撤销了。统一一句，不泄露细节。
            raise RemoteApprovalError("授权码无效、已过期或不属于这个操作")
        return _row_to_binding(row)

    def revoke_task(self, task_id: str, now: float | None = None) -> None:
        current = self.clock() if now is None else float(now)
        self.store.expire_approvals_for_task(task_id, current)


def _row_to_binding(row: dict[str, Any]) -> ApprovalBinding:
    return ApprovalBinding(
        approval_id=str(row.get("id", "")),
        task_id=str(row.get("task_id", "")),
        action_type=str(row.get("action_type", "")),
        action_id=str(row.get("action_id", "") or ""),
        account_id=str(row.get("account_id", "")),
        user_id=str(row.get("user_id", "")),
        expires_at=float(row.get("expires_at", 0) or 0),
    )
