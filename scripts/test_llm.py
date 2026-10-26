"""测试模型调用（这一步会真的联网、真的花钱，但一次只有几厘钱）。

用法（在项目根目录下）：
    .venv\\Scripts\\python.exe scripts\\test_llm.py
    .venv\\Scripts\\python.exe scripts\\test_llm.py "帮我写一首五言绝句"
    .venv\\Scripts\\python.exe scripts\\test_llm.py "你好" qwen

第二个参数是 models.yaml 里的模型名字，不填就用默认模型。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.core import Agent  # noqa: E402
from app.errors import JiaAgentError  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.memory import store  # noqa: E402
from app.settings import ensure_dirs  # noqa: E402


async def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "你好，请用一句话介绍你自己"
    provider = sys.argv[2] if len(sys.argv) > 2 else None

    ensure_dirs()
    setup_logging("WARNING")

    try:
        agent = Agent(provider_name=provider)
    except JiaAgentError as exc:
        print(f"[!!] {exc.message}")
        if exc.detail:
            print(f"     {exc.detail}")
        return 1

    session_id = str(store.create_session(title="命令行测试")["id"])
    print(f"模型：{agent.provider_name} / {agent.model}")
    print(f"我　：{prompt}")
    print("模型：", end="", flush=True)

    try:
        async for piece in agent.stream_reply(session_id, prompt):
            print(piece, end="", flush=True)
    except JiaAgentError as exc:
        print(f"\n[!!] {exc.message}")
        if exc.detail:
            print(f"     {exc.detail}")
        store.delete_session(session_id)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\n[!!] 出现未预期的错误：{type(exc).__name__}: {exc}")
        store.delete_session(session_id)
        return 1

    print()
    saved = store.get_messages(session_id, limit=10)
    print(f"\n[OK] 对话已写入数据库，本次共存下 {len(saved)} 条消息")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
