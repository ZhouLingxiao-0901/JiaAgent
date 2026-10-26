"""Jia-WeChat 自己的持久状态。

**为什么不写进 data/jiaagent.db**：和 workflow/store.py 同样的理由——这里
需要能在临时数据库上反复跑测试，而 app/memory/db.py 写死了主库。微信的
去重、游标、会话映射、任务归属和远程授权都是安全相关状态，必须可测。

**存什么、不存什么**
  * 存：入站消息的去重键（不是正文）、长轮询游标、会话映射、任务归属、
    通知进度、远程授权码的 HMAC（不是授权码本身）、账号暂停时间。
  * 不存：Bot token、context_token、二维码、验证码、授权码明文。
    凭证只放在 DPAPI 加密的 credentials 文件里（见 credentials.py）。

**消息是 at-most-once**：收到一条消息先原子 claim，再处理。进程在处理中途
崩溃时，这条消息不会在重启后自动重放——宁可丢一次回复，也不能把同一个
“打开文件/写文件”的任务执行两遍。任务本身的状态由 workflow store 持久化，
重启后的处理规则见 docs/wechat.md。
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_state (
    account_id    TEXT PRIMARY KEY,
    cursor        TEXT NOT NULL DEFAULT '',
    paused_until  REAL NOT NULL DEFAULT 0,
    last_error    TEXT NOT NULL DEFAULT '',
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound (
    message_key   TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    from_user_id  TEXT NOT NULL,
    state         TEXT NOT NULL,
    claimed_at    REAL NOT NULL,
    finished_at   REAL,
    detail        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_inbound_account ON inbound (account_id, claimed_at);

CREATE TABLE IF NOT EXISTS chat_sessions (
    account_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (account_id, user_id)
);

CREATE TABLE IF NOT EXISTS task_links (
    task_id            TEXT PRIMARY KEY,
    account_id         TEXT NOT NULL,
    user_id            TEXT NOT NULL,
    session_id         TEXT NOT NULL,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    last_status        TEXT NOT NULL DEFAULT '',
    plan_notified      INTEGER NOT NULL DEFAULT 0,
    notified_action_id TEXT NOT NULL DEFAULT '',
    terminal_notified  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_task_links_owner
    ON task_links (account_id, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS remote_approvals (
    id           TEXT PRIMARY KEY,
    code_hash    TEXT NOT NULL UNIQUE,
    account_id   TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    action_type  TEXT NOT NULL,
    action_id    TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    used_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_approvals_binding
    ON remote_approvals (task_id, action_type, action_id, used_at);
"""

_TASK_LINK_COLUMNS = frozenset({
    "last_status", "plan_notified", "notified_action_id", "terminal_notified",
})


class WeChatStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self._db() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # 入站去重
    # ------------------------------------------------------------------
    def claim_inbound(
        self, *, message_key: str, account_id: str, from_user_id: str, now: float
    ) -> bool:
        """原子认领一条消息。返回 True 表示这是第一次见到它。

        用 INSERT OR IGNORE + rowcount 做 compare-and-set；两个并发轮询
        同时拿到同一条消息时，只有一个能拿到 True。
        """
        with self._db() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO inbound"
                " (message_key, account_id, from_user_id, state, claimed_at)"
                " VALUES (?, ?, ?, 'claimed', ?)",
                (message_key, account_id, from_user_id, now),
            )
            return cursor.rowcount == 1

    def finish_inbound(
        self, *, message_key: str, state: str, now: float, detail: str = ""
    ) -> None:
        if state not in ("done", "failed", "ignored"):
            raise ValueError("入站消息的结束状态不合法")
        with self._db() as conn:
            conn.execute(
                "UPDATE inbound SET state = ?, finished_at = ?, detail = ?"
                " WHERE message_key = ?",
                (state, now, str(detail or "")[:500], message_key),
            )

    def inbound_state(self, message_key: str) -> str:
        with self._db() as conn:
            row = conn.execute(
                "SELECT state FROM inbound WHERE message_key = ?", (message_key,)
            ).fetchone()
        return str(row["state"]) if row else ""

    # ------------------------------------------------------------------
    # 账号状态 / 游标
    # ------------------------------------------------------------------
    def get_cursor(self, account_id: str) -> str:
        with self._db() as conn:
            row = conn.execute(
                "SELECT cursor FROM account_state WHERE account_id = ?", (account_id,)
            ).fetchone()
        return str(row["cursor"]) if row else ""

    def set_cursor(self, account_id: str, cursor: str, now: float) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO account_state (account_id, updated_at)"
                " VALUES (?, ?)",
                (account_id, now),
            )
            conn.execute(
                "UPDATE account_state SET cursor = ?, updated_at = ? WHERE account_id = ?",
                (str(cursor or ""), now, account_id),
            )

    def set_account_paused(
        self, account_id: str, *, paused_until: float, reason: str, now: float
    ) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO account_state (account_id, updated_at)"
                " VALUES (?, ?)",
                (account_id, now),
            )
            conn.execute(
                "UPDATE account_state SET paused_until = ?, last_error = ?, updated_at = ?"
                " WHERE account_id = ?",
                (float(paused_until), str(reason or "")[:300], now, account_id),
            )

    def clear_account_paused(self, account_id: str, now: float) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE account_state SET paused_until = 0, last_error = '',"
                " updated_at = ? WHERE account_id = ?",
                (now, account_id),
            )

    def account_state(self, account_id: str) -> dict[str, Any]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM account_state WHERE account_id = ?", (account_id,)
            ).fetchone()
        return dict(row) if row else {}

    # ------------------------------------------------------------------
    # 会话映射
    # ------------------------------------------------------------------
    def get_session_id(self, account_id: str, user_id: str) -> str:
        with self._db() as conn:
            row = conn.execute(
                "SELECT session_id FROM chat_sessions"
                " WHERE account_id = ? AND user_id = ?",
                (account_id, user_id),
            ).fetchone()
        return str(row["session_id"]) if row else ""

    def set_session_id(
        self, account_id: str, user_id: str, session_id: str, now: float
    ) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT INTO chat_sessions (account_id, user_id, session_id, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(account_id, user_id) DO UPDATE SET"
                " session_id = excluded.session_id, updated_at = excluded.updated_at",
                (account_id, user_id, session_id, now),
            )

    # ------------------------------------------------------------------
    # 任务归属
    # ------------------------------------------------------------------
    def link_task(
        self,
        *,
        task_id: str,
        account_id: str,
        user_id: str,
        session_id: str,
        now: float,
    ) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO task_links"
                " (task_id, account_id, user_id, session_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, account_id, user_id, session_id, now, now),
            )
            conn.execute(
                "UPDATE task_links SET account_id = ?, user_id = ?, session_id = ?,"
                " updated_at = ? WHERE task_id = ?",
                (account_id, user_id, session_id, now, task_id),
            )

    def get_task_link(self, task_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM task_links WHERE task_id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_task_links(
        self, account_id: str, user_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM task_links WHERE account_id = ? AND user_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (account_id, user_id, max(1, int(limit))),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_watchable_links(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM task_links ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_task_link(self, task_id: str, *, now: float, **fields: Any) -> bool:
        clean: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in _TASK_LINK_COLUMNS:
                raise ValueError(f"不能修改任务通知的这个字段：{key}")
            if key in ("plan_notified", "terminal_notified"):
                value = int(bool(value))
            clean[key] = value
        clean["updated_at"] = now
        assignments = ", ".join(f"{k} = ?" for k in clean)
        with self._db() as conn:
            cursor = conn.execute(
                f"UPDATE task_links SET {assignments} WHERE task_id = ?",
                (*clean.values(), task_id),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # 远程授权码（只存 HMAC）
    # ------------------------------------------------------------------
    def create_approval(
        self,
        *,
        code_hash: str,
        account_id: str,
        user_id: str,
        task_id: str,
        action_type: str,
        action_id: str,
        now: float,
        expires_at: float,
    ) -> str:
        """为某个绑定建一个一次性授权码。

        同一绑定的旧码立即失效：用户手里同时只有一个有效码，避免旧消息
        被翻出来再用一次。
        """
        approval_id = uuid.uuid4().hex
        with self._db() as conn:
            conn.execute(
                "UPDATE remote_approvals SET expires_at = 0"
                " WHERE task_id = ? AND action_type = ? AND action_id = ?"
                " AND used_at IS NULL",
                (task_id, action_type, action_id),
            )
            conn.execute(
                "INSERT INTO remote_approvals"
                " (id, code_hash, account_id, user_id, task_id, action_type,"
                " action_id, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (approval_id, code_hash, account_id, user_id, task_id,
                 action_type, action_id, now, expires_at),
            )
        return approval_id

    def find_active_approval(
        self,
        *,
        task_id: str,
        action_type: str,
        action_id: str,
        now: float,
    ) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM remote_approvals"
                " WHERE task_id = ? AND action_type = ? AND action_id = ?"
                " AND used_at IS NULL AND expires_at > ?"
                " ORDER BY created_at DESC LIMIT 1",
                (task_id, action_type, action_id, now),
            ).fetchone()
        return dict(row) if row else None

    def find_approval_by_hash(
        self,
        code_hash: str,
        *,
        account_id: str,
        user_id: str,
        now: float,
    ) -> dict[str, Any] | None:
        """按码指纹查绑定。只返回给匹配的账号/用户，避免跨用户探测。"""
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM remote_approvals"
                " WHERE code_hash = ? AND account_id = ? AND user_id = ?"
                " AND used_at IS NULL AND expires_at > ?",
                (code_hash, account_id, user_id, now),
            ).fetchone()
        return dict(row) if row else None

    def consume_approval(
        self,
        *,
        code_hash: str,
        account_id: str,
        user_id: str,
        task_id: str,
        action_type: str,
        action_id: str,
        now: float,
    ) -> dict[str, Any] | None:
        """原子消费。必须同时匹配码、账号、用户、任务、动作和有效期。"""
        with self._db() as conn:
            cursor = conn.execute(
                "UPDATE remote_approvals SET used_at = ?"
                " WHERE code_hash = ? AND account_id = ? AND user_id = ?"
                " AND task_id = ? AND action_type = ? AND action_id = ?"
                " AND used_at IS NULL AND expires_at > ?",
                (now, code_hash, account_id, user_id, task_id,
                 action_type, action_id, now),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM remote_approvals WHERE code_hash = ?", (code_hash,)
            ).fetchone()
        return dict(row) if row else None

    def expire_approvals_for_task(self, task_id: str, now: float) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE remote_approvals SET expires_at = 0"
                " WHERE task_id = ? AND used_at IS NULL",
                (task_id,),
            )

    def expire_approvals_for_user(
        self, account_id: str, user_id: str, now: float
    ) -> int:
        """撤销某账号下某个用户所有仍有效的授权码。精确匹配，不误伤他人。"""
        with self._db() as conn:
            cursor = conn.execute(
                "UPDATE remote_approvals SET expires_at = 0"
                " WHERE account_id = ? AND user_id = ?"
                " AND used_at IS NULL AND expires_at > ?",
                (str(account_id), str(user_id), float(now)),
            )
            return int(cursor.rowcount)

    def expire_approvals_for_account(self, account_id: str, now: float) -> int:
        """撤销某账号所有仍有效的授权码（logout 用）。"""
        with self._db() as conn:
            cursor = conn.execute(
                "UPDATE remote_approvals SET expires_at = 0"
                " WHERE account_id = ? AND used_at IS NULL AND expires_at > ?",
                (str(account_id), float(now)),
            )
            return int(cursor.rowcount)

    def cleanup_approvals(self, *, now: float, keep_seconds: float = 7 * 86400) -> int:
        with self._db() as conn:
            cursor = conn.execute(
                "DELETE FROM remote_approvals WHERE expires_at < ?",
                (now - keep_seconds,),
            )
            return cursor.rowcount
