"""直接测试四个工具，不经过模型、不经过网页。

默认只跑"安全"的部分：只读工具会真的执行，会改电脑的两个工具
只跑参数校验和权限拦截这些失败路径（不会留下任何痕迹）。

要真正试一次"打开记事本"和"创建文件"，加上 --allow：
    .venv\\Scripts\\python.exe scripts\\test_tools.py --allow

加上 --allow 之后，记事本窗口会真的弹出来，这是正常的。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logging_setup import setup_logging  # noqa: E402
from app.tools.base import ToolResult  # noqa: E402
from app.tools.registry import registry  # noqa: E402
from app.tools.win_process import find_process  # noqa: E402

WRITE_TARGET = str(Path(__file__).resolve().parent.parent / "logs" / ("工具测试_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"))
WRITE_CONTENT = "这是由 create_text_file 工具创建的文件。\n如果能看到这段中文，说明编码正常。"

results: list[tuple[str, bool, str]] = []

def record(note: str, ok: bool, detail: str) -> None:
    results.append((note, ok, detail))
    print(f"  {'[OK]' if ok else '[!!]'} {note}")
    for line in (detail or "").splitlines():
        print(f"       {line}")
    print()

async def run_tool(name: str, args: dict, expect_ok: bool, note: str) -> ToolResult:
    """跑一个工具，并核对"成功/失败"是否符合预期。"""
    tool = registry.get(name)
    if tool is None:
        record(note, False, f"找不到工具 {name}")
        return ToolResult(ok=False, error="工具不存在")
    result = await tool.execute(args)
    ok = result.ok == expect_ok
    detail = result.as_text()
    if len(detail) > 400:
        detail = detail[:400] + "\n...（已截断）"
    record(f"{note}  ->  {'成功' if result.ok else '失败'}", ok, detail)
    return result

def banner(title: str) -> None:
    print("=" * 74)
    print(f" {title}")
    print("=" * 74)

async def main() -> int:
    setup_logging("ERROR")
    allow = "--allow" in sys.argv

    banner("工具测试")
    print(f" 已注册的工具：{', '.join(t.name for t in registry.all())}")
    for tool in registry.all():
        mark = "需要授权" if tool.requires_confirmation else "免授权"
        print(f"   - {tool.name:<18} {mark}")
    mode = "完整测试（会真的打开记事本、创建文件）" if allow else "安全模式（只跑只读和拦截路径）"
    print(f" 模式：{mode}")
    print()

    banner("一、get_system_info（只读，免授权）")
    await run_tool("get_system_info", {}, True, "读取系统信息")

    banner("二、list_directory（只读，免授权）")
    await run_tool("list_directory", {"path": "D:\\JiaAgent"}, True, "列项目目录")
    await run_tool("list_directory", {"path": "C:\\Windows"}, False, "列系统目录（应被拒绝）")
    await run_tool("list_directory", {"path": "D:\\JiaAgent\\.env"}, False, "列密钥文件（应被拒绝）")
    await run_tool("list_directory", {"path": "D:\\不存在的目录"}, False, "不存在的目录（应失败）")
    await run_tool("list_directory", {}, False, "不传 path（应被参数校验拦下）")

    banner("三、create_text_file 的拦截逻辑（本身不会写任何文件）")
    await run_tool(
        "create_text_file",
        {"path": "C:\\Windows\\坏文件.txt", "content": "x"},
        False, "写进系统目录（应被拒绝）",
    )
    await run_tool(
        "create_text_file",
        {"path": "D:\\JiaAgent\\data\\坏文件.txt", "content": "x"},
        False, "写进黑名单目录（应被拒绝）",
    )
    await run_tool(
        "create_text_file",
        {"path": "D:\\JiaAgent\\logs\\偷偷.exe", "content": "x"},
        False, "伪装成可执行文件（应被拒绝）",
    )
    await run_tool(
        "create_text_file",
        {"path": "D:\\JiaAgent\\logs\\缺内容.txt"},
        False, "不给 content（应被参数校验拦下）",
    )
    await run_tool(
        "create_text_file",
        {"path": "D:\\JiaAgent\\logs\\a.txt", "content": "x", "乱写参数": 1},
        False, "多传一个不认识的参数（应被拦下）",
    )
    await run_tool(
        "create_text_file",
        {"path": "D:\\JiaAgent\\logs\\超大.txt", "content": "x" * 300000},
        False, "内容超过 200KB 上限（应被拒绝）",
    )

    banner("四、open_application 的拦截逻辑（不会真的打开任何东西）")
    await run_tool("open_application", {"app": "不存在的应用"}, False, "不认识的程序（应被拒绝）")
    await run_tool("open_application", {"app": "cmd"}, False, "命令行工具（应被拒绝）")
    await run_tool("open_application", {}, False, "不传 app（应被参数校验拦下）")

    banner("五、进程核实机制（只读，不启动任何程序）")
    running = find_process("explorer.exe")
    record("找一个确定在运行的进程 explorer.exe", bool(running), f"找到进程号 {running}")
    missing = find_process("这个进程肯定不存在.exe")
    record("找一个肯定不存在的进程", not missing, f"返回 {missing}，说明不会误报")
    record("传空进程名", not find_process(""), "返回空列表，不会误判")

    if allow:
        banner("六、真实操作（--allow 才会跑）")
        await run_tool(
            "create_text_file",
            {"path": WRITE_TARGET, "content": WRITE_CONTENT},
            True, f"创建文件 {WRITE_TARGET}",
        )
        created = Path(WRITE_TARGET)
        if created.exists():
            actual = created.read_text(encoding="utf-8")
            same = actual == WRITE_CONTENT
            record(
                "  回读文件内容核对",
                same,
                f"磁盘上的内容{'与写入完全一致' if same else '与写入不一致！'}，共 {len(actual)} 个字符",
            )
        else:
            record("  回读文件内容核对", False, "文件不存在")

        await run_tool(
            "create_text_file",
            {"path": WRITE_TARGET, "content": "换个内容"},
            False, "再写一次同名文件（应拒绝覆盖）",
        )
        await run_tool("open_application", {"app": "记事本"}, True, "打开记事本（窗口应该弹出来）")

        # 收掉本脚本自己刚创建的那个测试文件。
        # 注意：删的是它自己刚写的东西，不是你的文件。
        created.unlink(missing_ok=True)
        print(f"       （已清理本次的测试文件：{created}）")
    else:
        print("（跳过真实操作。要试的话加 --allow）")
        print()

    print("=" * 74)
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print(f" 结果：{len(results) - len(failed)}/{len(results)} 通过，失败项：")
        for name in failed:
            print(f"   - {name}")
        return 1
    print(f" 结果：{len(results)}/{len(results)} 全部通过 [OK]")
    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
