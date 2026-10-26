"""测试记忆模块（SQLite 读写）。不联网，不需要 API Key。

用法（在项目根目录下）：
    .venv\\Scripts\\python.exe scripts\\test_memory.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logging_setup import setup_logging  # noqa: E402
from app.memory import store  # noqa: E402
from app.memory.db import DB_PATH, init_db  # noqa: E402
from app.settings import ensure_dirs  # noqa: E402


def main() -> int:
    ensure_dirs()
    setup_logging("WARNING")

    print("数据库文件：", DB_PATH)
    print("正在建表 ...")
    init_db()
    print("[OK] 建表完成\n")

    session = store.create_session(title="测试会话")
    session_id = session["id"]
    print("新建会话：", session_id)

    store.add_message(session_id, "user", "你好，我叫小明")
    store.add_message(session_id, "assistant", "你好小明，很高兴认识你")
    store.add_message(session_id, "user", "记住我喜欢喝美式咖啡")

    rows = store.get_messages(session_id, limit=10)
    print(f"\n读回 {len(rows)} 条消息（按时间正序）：")
    for row in rows:
        print(f"  [{row['role']:<9}] {row['content']}")

    sessions = store.list_sessions(limit=5)
    print(f"\n最近的会话（共 {len(sessions)} 个）：")
    for item in sessions:
        print(f"  {item['id'][:8]}...  标题={item['title']}  更新时间={item['updated_at']}")

    store.delete_session(session_id)
    remaining = store.count_messages(session_id)
    print(f"\n已清理测试会话，它的剩余消息数：{remaining}")
    print("（应该是 0，说明外键级联删除也正常工作）")

    if remaining != 0 or len(rows) != 3:
        print("\n[!!] 结果不符合预期，请把上面的输出发给我")
        return 1

    print("\n[OK] 记忆模块工作正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
