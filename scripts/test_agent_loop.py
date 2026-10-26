"""端到端测试：智能体循环（阶段二第 5 步）。

直接连真实模型，跑完"用户说话 -> 模型决定调工具 -> 执行 -> 回报"的完整链路。

四组用例：
  1. 查系统信息（不需要授权，直接执行）
  2. 查文件夹    （不需要授权，直接执行）
  3. 打开记事本  （需要授权，模拟"允许"，真的会打开记事本）
  4. 打开计算器  （需要授权，模拟"拒绝"，什么都不该发生）

用法：
  .venv\\Scripts\\python.exe scripts\\test_agent_loop.py

注意第 3 组会真的打开一个记事本窗口，这是故意的——我们要看到
真实副作用发生，而不是听模型嘴上说"打开了"。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import pending  # noqa: E402
from app.agent.core import Agent  # noqa: E402
from app.memory import store  # noqa: E402


def _fmt(ev: dict) -> str:
    t = ev["type"]
    if t == "delta":
        return ev["text"]
    if t == "tool_start":
        return f"\n  >>> 执行：{ev['summary']}\n"
    if t == "tool_result":
        mark = "成功" if ev["ok"] else "失败"
        return f"  [工具{mark}] {ev['output']}\n"
    if t == "need_confirm":
        a = ev["action"]
        return f"\n  !!! 请求授权：{a['summary']}（编号 {a['id'][:8]}）\n"
    if t == "final":
        return ""
    return f"  ({t})"


async def run_case(agent: Agent, session_id: str, text: str, decision: bool | None) -> None:
    print(f"\n{'=' * 64}")
    print(f"用户：{text}")
    print(f"{'-' * 64}")
    async for ev in agent.run_turn(session_id, text):
        print(_fmt(ev), end="", flush=True)
        if ev["type"] == "need_confirm" and decision is not None:
            await asyncio.sleep(0.1)   # 假装用户看了一秒，再点按钮
            pending.store.answer(ev["action"]["id"], decision)
            print(f"  （用户点了：{'允许' if decision else '拒绝'}）\n")
    print()


async def main() -> int:
    agent = Agent()

    sessions: list[str] = []
    for i in range(4):
        sid = store.ensure_session(None)
        sessions.append(sid)

    try:
        await run_case(agent, sessions[0], "帮我看看这台电脑的系统配置", None)
        await run_case(agent, sessions[1], "D 盘的 JiaAgent 文件夹里有什么？", None)
        await run_case(agent, sessions[2], "帮我打开记事本", True)
        await run_case(agent, sessions[3], "帮我打开计算器", False)
    finally:
        for sid in sessions:
            store.delete_session(sid)
        print("（测试会话已清理，不会污染历史记录）")

    print("测试跑完了。请重点核对两点：")
    print("  1. 记事本窗口真的弹出来了（说明「允许」后真实执行了）")
    print("  2. 计算器没有弹出（说明「拒绝」后什么都没做）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
