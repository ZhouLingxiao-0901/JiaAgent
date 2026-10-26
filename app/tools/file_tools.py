"""写文件工具。

只有"新建文本文件"这一个，而且是会改硬盘的操作，
所以 requires_confirmation = True，必须用户点允许才会执行。

阶段二故意不做的事：删除文件、移动文件、改已有文件的内容。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.tools import policy
from app.tools.base import Tool, ToolResult

# 这些扩展名说明它是可执行的东西，不允许写成文本文件
_FORBIDDEN_SUFFIXES = {".exe", ".dll", ".bat", ".cmd", ".ps1", ".vbs", ".scr", ".msi", ".com"}


class CreateTextFileTool(Tool):
    name = "create_text_file"
    label = "创建文件"
    description = (
        "在用户指定的文件夹里新建一个文本文件并写入内容。"
        "如果同名文件已经存在，默认拒绝写入；确实要覆盖的话，"
        "必须把 overwrite 显式设为 true，用户在确认时也会看到覆盖提示。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件的完整路径，例如 D:\\notes\\hello.txt",
            },
            "content": {
                "type": "string",
                "description": "要写入的文本内容",
            },
            "overwrite": {
                "type": "boolean",
                "description": "文件已存在时是否覆盖。默认 false，也就是拒绝覆盖。",
            },
        },
        "required": ["path", "content"],
    }
    requires_confirmation = True

    def describe_action(self, args: dict[str, Any]) -> str:
        """授权卡片上只显示路径和字数，不把整段内容糊上去。"""
        target = str(args.get("path", ""))
        content = args.get("content") or ""
        overwrite = bool(args.get("overwrite"))
        extra = "，并且会覆盖同名文件" if overwrite else ""
        return f"创建文件：{target}（写入 {len(content)} 个字符{extra}）"

    async def run(self, path: str, content: str, overwrite: bool = False) -> ToolResult:
        # 第一关：路径权限。不允许或踩黑名单会在这里被拦下。
        target_text = policy.check_writable_file(path)
        target = Path(target_text)

        # 第二关：文件类型。不允许写出可执行文件，避免绕过"禁止执行命令"的约束。
        if target.suffix.lower() in _FORBIDDEN_SUFFIXES:
            return ToolResult(
                ok=False,
                error=(
                    f"不允许创建这种类型的文件：{target.suffix}。"
                    "这个工具只能写普通文本文件，不能写可执行文件。"
                ),
            )

        # 第三关：内容大小。
        data = content.encode("utf-8")
        limit = policy.max_file_bytes()
        if len(data) > limit:
            return ToolResult(
                ok=False,
                error=f"内容太大了：{len(data)} 字节，上限是 {limit} 字节。请分几次写，或者换个小一点的内容。",
            )

        # 第四关：不覆盖已有文件。
        if target.exists() and not overwrite:
            return ToolResult(
                ok=False,
                error=(
                    f"这个文件已经存在：{target}。"
                    "为了避免弄丢你原来的内容，我不会直接覆盖。"
                    "如果你确实想覆盖它，请重新发起并明确说明要覆盖。"
                ),
            )

        # 真正写入
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        # 写完再核对一遍：真的落盘了才算成功，不靠"我以为写好了"
        if not target.exists():
            return ToolResult(ok=False, error=f"写入后没有找到文件，可能被系统拦截了：{target}")

        actual_bytes = target.stat().st_size
        action = "覆盖" if overwrite else "创建"
        return ToolResult(
            ok=True,
            output=(
                f"已{action}文件：{target}\n"
                f"  写入 {len(content)} 个字符，{actual_bytes} 字节（UTF-8 编码）\n"
                f"  所在文件夹：{target.parent}"
            ),
        )
