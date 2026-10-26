"""任务的持久化：建表 + 增删改查。

**建表语句内联在这个文件里，没有放进 app/memory/schema.sql。**
这是照着 app/apps/index.py 的做法（那个文件开头写了原因），理由只有一个但很硬：
`memory/store.py` 的 `_db()` 写死了主库，测试没法指向临时数据库；而工作流的状态机、
重试、再规划这些逻辑**必须**能在临时库上反复跑、反复造数据。内联 DDL + 可注入的
`db_path` 让 `scripts/test_workflow_store.py` 能做到这一点。

**代价**：没法给 `sessions` 加外键级联。`session_id` 只是个普通列 + 索引，
删会话时要靠应用层调 `delete_tasks_for_session()` 顺手清理，否则会留孤儿任务行。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from app.workflow.model import Step, StepStatus, Task, TaskStatus, can_transition

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    goal          TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    paused        INTEGER NOT NULL DEFAULT 0,
    provider      TEXT NOT NULL DEFAULT '',
    current_step  INTEGER NOT NULL DEFAULT 0,
    plan_revision INTEGER NOT NULL DEFAULT 0,
    replan_count  INTEGER NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    started_at    TEXT NOT NULL DEFAULT '',
    finished_at   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks (status);

CREATE TABLE IF NOT EXISTS task_steps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    step_index   INTEGER NOT NULL,
    goal         TEXT NOT NULL,
    tool         TEXT NOT NULL DEFAULT '',
    arguments    TEXT NOT NULL DEFAULT '{}',
    expect       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    result       TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL DEFAULT '',
    finished_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_steps_task ON task_steps (task_id, step_index);
"""

# 允许被 update_* 修改的列。**必须白名单**：字段名是直接拼进 SQL 的，
# 不挡住的话，将来某处把用户输入当字段名传进来就成了注入口子。
_TASK_COLUMNS = frozenset({
    "title", "status", "paused", "provider", "current_step",
    "plan_revision", "replan_count", "error", "summary",
    "started_at", "finished_at",
})
_STEP_COLUMNS = frozenset({
    "goal", "tool", "arguments", "expect", "status",
    "attempts", "result", "error", "started_at", "finished_at",
})

# 单条结果的留存上限。工具的原始输出可能很长，全存进去没必要，
# 也会把面板和回灌给模型的上下文撑爆。
MAX_RESULT_CHARS = 2000


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _make_title(goal: str, length: int = 24) -> str:
    clean = " ".join((goal or "").split())
    return clean if len(clean) <= length else clean[:length] + "..."


def _resolve_db_path(db_path: Path | str | None) -> Path:
    if db_path is not None:
        return Path(db_path)
    from app.memory.db import DB_PATH

    return Path(DB_PATH)


@contextmanager
def _db(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """一次数据库操作。正常结束提交，出错回滚。

    每次操作前都 executescript 一遍 DDL，幂等，保证表一定在——和 index.py 一致。
    """
    target = _resolve_db_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_DDL)
        yield conn
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def _truncate(text: str) -> str:
    text = text or ""
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + "…（已截断）"


# ----------------------------------------------------------------------
# 行 <-> 对象
# ----------------------------------------------------------------------


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        session_id=row["session_id"],
        goal=row["goal"],
        title=row["title"] or "",
        status=TaskStatus(row["status"]),
        paused=bool(row["paused"]),
        provider=row["provider"] or "",
        current_step=int(row["current_step"] or 0),
        plan_revision=int(row["plan_revision"] or 0),
        replan_count=int(row["replan_count"] or 0),
        error=row["error"] or "",
        summary=row["summary"] or "",
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
        started_at=row["started_at"] or "",
        finished_at=row["finished_at"] or "",
    )


def _row_to_step(row: sqlite3.Row) -> Step:
    try:
        arguments = json.loads(row["arguments"] or "{}")
        if not isinstance(arguments, dict):
            arguments = {}
    except json.JSONDecodeError:
        arguments = {}
    return Step(
        id=int(row["id"]),
        step_index=int(row["step_index"]),
        goal=row["goal"] or "",
        tool=row["tool"] or "",
        arguments=arguments,
        expect=row["expect"] or "",
        status=StepStatus(row["status"]),
        attempts=int(row["attempts"] or 0),
        result=row["result"] or "",
        error=row["error"] or "",
        started_at=row["started_at"] or "",
        finished_at=row["finished_at"] or "",
    )


# ----------------------------------------------------------------------
# 任务
# ----------------------------------------------------------------------


def create_task(
    session_id: str,
    goal: str,
    *,
    title: str = "",
    provider: str = "",
    db_path: Path | str | None = None,
) -> Task:
    now = _now()
    task = Task(
        id=uuid.uuid4().hex,
        session_id=session_id,
        goal=goal,
        title=title or _make_title(goal),
        status=TaskStatus.PENDING,
        provider=provider,
        created_at=now,
        updated_at=now,
    )
    with _db(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, session_id, goal, title, status, paused, provider,"
            " current_step, plan_revision, replan_count, error, summary,"
            " created_at, updated_at, started_at, finished_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task.id, task.session_id, task.goal, task.title, task.status.value,
             int(task.paused), task.provider, task.current_step, task.plan_revision,
             task.replan_count, task.error, task.summary,
             task.created_at, task.updated_at, task.started_at, task.finished_at),
        )
    logger.info("新建任务 %s：%s", task.id, task.title)
    return task


def get_task(
    task_id: str, *, with_steps: bool = True, db_path: Path | str | None = None
) -> Task | None:
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        task = _row_to_task(row)
        if with_steps:
            rows = conn.execute(
                "SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_index, id",
                (task_id,),
            ).fetchall()
            task.steps = [_row_to_step(r) for r in rows]
    return task


def list_tasks(
    *,
    session_id: str | None = None,
    status: TaskStatus | str | None = None,
    limit: int = 50,
    with_steps: bool = False,
    db_path: Path | str | None = None,
) -> list[Task]:
    sql = "SELECT * FROM tasks"
    params: list[Any] = []
    where: list[str] = []
    if session_id:
        where.append("session_id = ?")
        params.append(session_id)
    if status:
        where.append("status = ?")
        params.append(status.value if isinstance(status, TaskStatus) else str(status))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    params.append(max(1, int(limit)))

    with _db(db_path) as conn:
        tasks = [_row_to_task(r) for r in conn.execute(sql, params).fetchall()]
        if with_steps:
            for task in tasks:
                rows = conn.execute(
                    "SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_index, id",
                    (task.id,),
                ).fetchall()
                task.steps = [_row_to_step(r) for r in rows]
    return tasks


def update_task(
    task_id: str, *, db_path: Path | str | None = None, **fields: Any
) -> bool:
    """改任务的字段。字段名会被白名单挡一遍。

    状态变更走 can_transition() 检查：非法转移直接拒绝并返回 False，
    不会悄悄写进去。这是为了防止"已经成功的任务又被改回执行中"。
    """
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in _TASK_COLUMNS:
            raise ValueError(f"不能修改任务的这个字段：{key}")
        if key == "status":
            value = value.value if isinstance(value, TaskStatus) else str(value)
        elif key == "paused":
            value = int(bool(value))
        clean[key] = value

    if "status" in clean:
        current = get_task(task_id, with_steps=False, db_path=db_path)
        if current is None:
            return False
        target = TaskStatus(clean["status"])
        if target is not current.status and not can_transition(current.status, target):
            logger.warning(
                "拒绝非法的任务状态转移：%s -> %s（任务 %s）",
                current.status.value, target.value, task_id,
            )
            return False

    clean["updated_at"] = _now()
    assignments = ", ".join(f"{k} = ?" for k in clean)
    with _db(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",
            (*clean.values(), task_id),
        )
    return cursor.rowcount > 0


def delete_task(task_id: str, *, db_path: Path | str | None = None) -> bool:
    with _db(db_path) as conn:
        conn.execute("DELETE FROM task_steps WHERE task_id = ?", (task_id,))
        cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return cursor.rowcount > 0


def delete_tasks_for_session(
    session_id: str, *, db_path: Path | str | None = None
) -> int:
    """删掉某个会话下的所有任务。删会话时顺手调一下，免得留孤儿。"""
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE session_id = ?", (session_id,)
        ).fetchall()
        ids = [r["id"] for r in rows]
        for task_id in ids:
            conn.execute("DELETE FROM task_steps WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE session_id = ?", (session_id,))
    return len(ids)


# ----------------------------------------------------------------------
# 步骤
# ----------------------------------------------------------------------


def add_step(
    task_id: str,
    goal: str,
    *,
    tool: str = "",
    arguments: dict[str, Any] | None = None,
    expect: str = "",
    step_index: int | None = None,
    db_path: Path | str | None = None,
) -> int:
    """追加一个步骤，返回它的数据库编号。不传 step_index 就接在最后。"""
    with _db(db_path) as conn:
        if step_index is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(step_index), -1) AS m FROM task_steps WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            step_index = int(row["m"]) + 1
        cursor = conn.execute(
            "INSERT INTO task_steps (task_id, step_index, goal, tool, arguments,"
            " expect, status, attempts) VALUES (?,?,?,?,?,?,?,0)",
            (task_id, step_index, goal, tool,
             json.dumps(arguments or {}, ensure_ascii=False),
             expect, StepStatus.PENDING.value),
        )
    return int(cursor.lastrowid or 0)


def add_steps(
    task_id: str,
    steps: list[dict[str, Any]],
    *,
    start_index: int = 0,
    db_path: Path | str | None = None,
) -> list[int]:
    """批量加步骤。每一步是一个 dict：goal / tool / arguments / expect。"""
    ids: list[int] = []
    for offset, step in enumerate(steps):
        ids.append(add_step(
            task_id,
            str(step.get("goal", "")).strip() or "（这一步没说明要做什么）",
            tool=str(step.get("tool", "") or ""),
            arguments=step.get("arguments") if isinstance(step.get("arguments"), dict) else {},
            expect=str(step.get("expect", "") or ""),
            step_index=start_index + offset,
            db_path=db_path,
        ))
    return ids


def list_steps(task_id: str, *, db_path: Path | str | None = None) -> list[Step]:
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_index, id",
            (task_id,),
        ).fetchall()
    return [_row_to_step(r) for r in rows]


def update_step(
    step_id: int, *, db_path: Path | str | None = None, **fields: Any
) -> bool:
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in _STEP_COLUMNS:
            raise ValueError(f"不能修改步骤的这个字段：{key}")
        if key == "status":
            value = value.value if isinstance(value, StepStatus) else str(value)
        elif key == "arguments" and isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        elif key in ("result", "error"):
            value = _truncate(str(value or ""))
        clean[key] = value

    assignments = ", ".join(f"{k} = ?" for k in clean)
    with _db(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE task_steps SET {assignments} WHERE id = ?",
            (*clean.values(), step_id),
        )
    return cursor.rowcount > 0


def mark_step_running(step_id: int, *, db_path: Path | str | None = None) -> bool:
    """开始跑一步：状态置为执行中、尝试次数 +1、记开始时间。"""
    with _db(db_path) as conn:
        cursor = conn.execute(
            "UPDATE task_steps SET status = ?, attempts = attempts + 1,"
            " started_at = ?, finished_at = '', error = '', result = ''"
            " WHERE id = ?",
            (StepStatus.RUNNING.value, _now(), step_id),
        )
    return cursor.rowcount > 0


def replace_pending_steps(
    task_id: str,
    new_steps: list[dict[str, Any]],
    *,
    from_index: int,
    db_path: Path | str | None = None,
) -> list[int]:
    """再规划：把 from_index 及之后**还没执行**的步骤换成新的。

    已经跑过的步骤一概不动——历史要留着，用户才能在面板上看到到底发生过什么。
    """
    with _db(db_path) as conn:
        conn.execute(
            "DELETE FROM task_steps WHERE task_id = ? AND step_index >= ?"
            " AND status IN (?, ?)",
            (task_id, from_index, StepStatus.PENDING.value, StepStatus.SKIPPED.value),
        )
    return add_steps(task_id, new_steps, start_index=from_index, db_path=db_path)


def next_step(task_id: str, *, db_path: Path | str | None = None) -> Step | None:
    """下一个该跑的步骤（按顺序取第一个还没跑完的）。没有就返回 None。"""
    with _db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM task_steps WHERE task_id = ? AND status IN (?, ?)"
            " ORDER BY step_index, id LIMIT 1",
            (task_id, StepStatus.PENDING.value, StepStatus.RUNNING.value),
        ).fetchone()
    return _row_to_step(row) if row else None


# ----------------------------------------------------------------------
# 启动时对账
# ----------------------------------------------------------------------


def mark_interrupted_as_failed(
    *, reason: str = "", db_path: Path | str | None = None
) -> int:
    """把"服务重启前还在跑"的任务标成失败。

    pending 待办和执行中的协程都只活在内存里，进程一重启就没了。这时候数据库里
    还留着 running / waiting_confirmation 的行——**绝不能自动接着跑**：那些步骤
    可能有非幂等的副作用（开应用、写文件），重跑一遍可能出事。所以老实标成失败，
    让用户自己决定要不要重新发起。

    返回被标记的条数。
    """
    message = reason or "服务重启前任务未完成，请重新发起"
    now = _now()
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status IN (?, ?)",
            (TaskStatus.RUNNING.value, TaskStatus.WAITING_CONFIRMATION.value),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tasks SET status = ?, paused = 0, error = ?, finished_at = ?,"
                " updated_at = ? WHERE id = ?",
                (TaskStatus.FAILED.value, message, now, now, row["id"]),
            )
            conn.execute(
                "UPDATE task_steps SET status = ?, error = ?, finished_at = ?"
                " WHERE task_id = ? AND status IN (?, ?, ?)",
                (StepStatus.CANCELLED.value, "服务重启，这一步没跑完", now, row["id"],
                 StepStatus.RUNNING.value, StepStatus.WAITING_CONFIRMATION.value,
                 StepStatus.PENDING.value),
            )
    if rows:
        logger.warning("启动对账：%d 个任务因服务重启被标记为失败", len(rows))
    return len(rows)
