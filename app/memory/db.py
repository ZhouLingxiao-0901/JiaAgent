"""SQLite 的连接和初始化。

为什么"每次操作都新开一个连接"？
因为 SQLite 的连接默认不能跨线程使用，而 FastAPI 是多线程的。
打开一个本地 SQLite 文件非常快，阶段一完全够用。
真到了性能瓶颈，再换连接池，要改的也只有这一个文件。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.settings import DATA_DIR

DB_PATH = DATA_DIR / "jiaagent.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect() -> sqlite3.Connection:
    """新建一个数据库连接。用完由调用方负责 close()。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # 让查询结果可以按列名取值，例如 row["role"]
    conn.row_factory = sqlite3.Row
    # 打开外键约束，删会话时它的消息才会跟着一起删掉
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """建表。重复执行也没关系，schema.sql 里都是 IF NOT EXISTS。"""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect()
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()
