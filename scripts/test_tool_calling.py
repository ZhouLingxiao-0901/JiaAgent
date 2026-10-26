"""测试模型能不能正确选择工具。**只打印，不执行任何工具。**

这一步验证的是"模型适配器 + 工具清单"这一层，
所以不管模型说"要打开记事本"，脚本也绝不会真的去打开。

用法（在项目根目录下）：
    .venv\\Scripts\\python.exe scripts\\test_tool_calling.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.errors import JiaAgentError  # noqa: E402
from app.llm.base import TextDelta, ToolCallsReady  # noqa: E402
from app.llm.registry import create_adapter  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.tools.registry import registry  # noqa: E402

# (说明, 用户说的话, 期望调用的工具名或 None)
CASES: list[tuple[str, str, str | None]] = [
    ("问系统配置", "帮我看看这台电脑的内存还有多少", "get_system_info"),
    ("要开应用", "帮我打开记事本", "open_application"),
    ("要列目录", "D 盘 JiaAgent 文件夹里有什么", "list_directory"),
    ("要写文件", "在 D 盘的 JiaAgent\\logs 里建个 说明.txt，内容写你好", "create_text_file"),
    ("只是闲聊", "你好，你是谁？", None),
]

results: list[tuple[str, bool]] = []


async def main() -> int:
    setup_logging("WARNING")

    try:
        adapter = create_adapter(None)
    except JiaAgentError as exc:
        print(f"[!!] {exc.message}")
        if exc.detail:
            print(f"     {exc.detail}")
        return 1

    tools = registry.openai_tools()
    print("=" * 74)
    print(" 工具调用能力测试（只打印，不执行）")
    print("=" * 74)
    print(f" 模型：{adapter.provider_name} / {adapter.model}")
    print(f" 发给模型的工具清单：{', '.join(t['function']['name'] for t in tools)}")
    print()

    system = "你是 JiaAgent，一个运行在用户 Windows 电脑上的中文助手。需要操作电脑时请调用工具。"

    for note, question, expected in CASES:
        print("-" * 74)
        print(f" {note}：{question}")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ]

        text, calls = "", []
        try:
            async for event in adapter.stream_turn(messages, tools=tools):
                if isinstance(event, TextDelta):
                    text += event.text
                elif isinstance(event, ToolCallsReady):
                    calls = event.calls
        except JiaAgentError as exc:
            print(f"   [!!] 调用失败：{exc.message}")
            if exc.detail:
                print(f"        {exc.detail}")
            results.append((note, False))
            continue

        names = [c.name for c in calls]
        picked = names[0] if names else None
        ok = picked == expected
        results.append((note, ok))

        if text.strip():
            print(f"   模型说的话：{text.strip()[:80]}")
        if calls:
            for call in calls:
                parsed = call.parsed_arguments()
                raw_ok = True
                try:
                    json.loads(call.arguments or "{}")
                except json.JSONDecodeError:
                    raw_ok = False
                print(f"   要求调用：{call.name}")
                print(f"     调用编号：{call.id}")
                print(f"     原始参数：{call.arguments}")
                print(f"     解析结果：{parsed}   {'（JSON 合法）' if raw_ok else '（JSON 不合法！）'}")
                if not raw_ok:
                    ok = results[-1] = (note, False)
                if call.name not in {t.name for t in registry.all()}:
                    print("     [!!] 这个工具不在登记表里")
                    results[-1] = (note, False)
        else:
            print("   （没有要求调用工具）")

        print(f"   期望：{'调用 ' + expected if expected else '不调用工具'}"
              f"　实际：{'调用 ' + picked if picked else '不调用工具'}"
              f"　{'[OK]' if ok else '[!!]'}")

    print()
    print("=" * 74)
    failed = [n for n, ok in results if not ok]
    if failed:
        print(f" 结果：{len(results) - len(failed)}/{len(results)} 通过，不符合预期的是：")
        for name in failed:
            print(f"   - {name}")
        return 1
    print(f" 结果：{len(results)}/{len(results)} 全部通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
