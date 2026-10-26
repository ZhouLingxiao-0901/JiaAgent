"""会话与消息的读写。

上层代码只调用这里的函数，不直接写 SQL。
以后就算换成别的数据库，要改的也只有这一个文件。
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from app.errors import StorageError
from app.memory.db import connect

logger = logging.getLogger(__name__)

DEFAULT_TITLE = "新对话"


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """一次数据库操作。正常结束自动提交，出错自动回滚并转成 StorageError。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        logger.error("数据库操作失败：%s", exc)
        raise StorageError("数据库操作失败", detail=str(exc)) from exc
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _make_title(text: str, length: int = 20) -> str:
    """把第一条用户消息压成一行，当作会话标题。"""
    clean = " ".join(text.split())
    if len(clean) <= length:
        return clean
    return clean[:length] + "..."


# ----------------------------------------------------------------------
# 会话
# ----------------------------------------------------------------------
def create_session(title: str = DEFAULT_TITLE, model: str = "") -> dict:
    session_id = uuid.uuid4().hex
    now = _now()
    with _db() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, model, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, title, model, now, now),
        )
    logger.info("新建会话 %s", session_id)
    return {
        "id": session_id,
        "title": title,
        "model": model,
        "created_at": now,
        "updated_at": now,
    }


def session_exists(session_id: str) -> bool:
    with _db() as conn:
        row = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row is not None


def ensure_session(session_id: str | None, model: str = "") -> str:
    """给了会话 ID 就用它，没给就新建一个。

    如果前端传来一个数据库里不存在的旧 ID，这里会明确报错，
    而不是悄悄创建一堆空会话。
    """
    if not session_id:
        return str(create_session(model=model)["id"])
    if not session_exists(session_id):
        raise StorageError(
            f"会话不存在:{session_id}",
            detail="可能是浏览器里存的旧会话，点左边「新对话」重新开始即可。",
        )
    return session_id


def list_sessions(limit: int = 50) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, title, model, created_at, updated_at"
            " FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_session(session_id: str) -> bool:
    with _db() as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return cursor.rowcount > 0


# ----------------------------------------------------------------------
# 消息
# ----------------------------------------------------------------------
def add_message(
    session_id: str,
    role: str,
    content: str,
    model: str | None = None,
) -> int:
    """追加一条消息，并顺带更新会话的活动时间和标题。"""
    now = _now()
    with _db() as conn:
        cursor = conn.execute(
            "INSERT INTO messages (session_id, role, content, model, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, model, now),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        # 第一条用户消息自动变成会话标题
        if role == "user":
            conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ? AND (title IS NULL OR title = ?)",
                (_make_title(content), session_id, DEFAULT_TITLE),
            )
    return int(cursor.lastrowid or 0)


def get_messages(
    session_id: str, limit: int = 20, include_tool: bool = False
) -> list[dict]:
    """取最近的 limit 条对话，按时间正序返回（老的在前）。

    默认只取 user / assistant：system 提示词每次临时拼，不入库；
    工具执行记录也不回灌给模型，免得把旧状态当成现在还有效。
    include_tool=True 时把 tool 记录也取出来，供网页回显历史用。
    """
    roles = ("user", "assistant", "tool") if include_tool else ("user", "assistant")
    placeholders = ", ".join("?" for _ in roles)
    with _db() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM (
                SELECT id, role, content, model, created_at
                FROM messages
                WHERE session_id = ? AND role IN ({placeholders})
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (session_id, *roles, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def count_messages(session_id: str) -> int:
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
    return int(row["n"]) if row else 0
