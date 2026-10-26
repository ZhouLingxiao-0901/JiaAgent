"""只读工具：看系统信息、看目录列表。

这两个工具没有副作用，所以 requires_confirmation = False，
不需要用户点确认就能执行。但路径仍然要过权限检查。
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.tools import policy
from app.tools.base import Tool, ToolResult, ToolError

# ----------------------------------------------------------------------
# 读物理内存：用 Windows 自带的 API，省得为这一件事多装一个 psutil
# ----------------------------------------------------------------------
class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _read_memory() -> tuple[float, float, int] | None:
    """返回（总内存 GB, 可用 GB, 已用百分比）。读不到就返回 None。"""
    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return (
            status.ullTotalPhys / 1024 ** 3,
            status.ullAvailPhys / 1024 ** 3,
            int(status.dwMemoryLoad),
        )
    except Exception:  # noqa: BLE001 - 非 Windows 或 API 不可用时不要崩
        return None


def _list_drives() -> list[str]:
    """列出这台机器上有哪些盘符。"""
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        return [
            f"{letter}:\\"
            for i, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            if bitmask >> i & 1
        ]
    except Exception:  # noqa: BLE001
        return [f"{letter}:\\" for letter in "CD" if Path(f"{letter}:\\").exists()]


def _format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024 or unit == "TB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


# ----------------------------------------------------------------------
# 工具一：系统信息
# ----------------------------------------------------------------------
class GetSystemInfoTool(Tool):
    name = "get_system_info"
    label = "查看系统信息"
    description = (
        "获取用户这台 Windows 电脑的基本信息：系统版本、CPU、内存、磁盘剩余空间、"
        "当前时间等。当用户问「我电脑什么配置」「还有多少内存」「磁盘够不够」时用它。"
    )
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    requires_confirmation = False   # 纯读取，不改任何东西

    async def run(self) -> ToolResult:
        lines: list[str] = []

        lines.append("【系统】")
        lines.append(f"  操作系统：{platform.system()} {platform.release()} {platform.version()}")
        lines.append(f"  机器名　：{platform.node()}")
        lines.append(f"  当前用户：{os.environ.get('USERNAME', '未知')}")
        lines.append(f"  当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        lines.append("")
        lines.append("【处理器】")
        cpu = platform.processor() or "未知"
        lines.append(f"  型号　　：{cpu}")
        lines.append(f"  核心数　：{os.cpu_count()} 个逻辑核心")

        memory = _read_memory()
        lines.append("")
        lines.append("【内存】")
        if memory:
            total, avail, used_pct = memory
            lines.append(f"  总容量　：{total:.1f} GB")
            lines.append(f"  可用　　：{avail:.1f} GB")
            lines.append(f"  已用　　：{used_pct}%")
        else:
            lines.append("  读取失败（这台机器上读不到内存信息）")

        lines.append("")
        lines.append("【磁盘】")
        for drive in _list_drives():
            try:

                total, used, free = shutil.disk_usage(drive)
                lines.append(
                    f"  {drive:<4} 总 {_format_size(total)}，"
                    f"已用 {_format_size(used)}，剩余 {_format_size(free)}"
                )
            except OSError as exc:
                lines.append(f"  {drive:<4} 读不到（{exc}）")

        lines.append("")
        lines.append("【运行环境】")
        lines.append(f"  Python　：{platform.python_version()}")
        lines.append(f"  当前目录：{os.getcwd()}")

        return ToolResult(ok=True, output="\n".join(lines))


# ----------------------------------------------------------------------
# 工具二：列目录
# ----------------------------------------------------------------------
class ListDirectoryTool(Tool):
    name = "list_directory"
    label = "查看文件夹"
    description = (
        "列出用户指定的文件夹里有哪些文件和子文件夹，包含类型、大小和修改时间。"
        "只能查看允许访问的目录。当用户问「某个文件夹里有什么」时用它。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要查看的文件夹路径，例如 D:\\JiaAgent 或 D:/notes",
            }
        },
        "required": ["path"],
    }
    requires_confirmation = False   # 只看文件名，不读内容、不改东西

    async def run(self, path: str) -> ToolResult:
        # 权限检查在这一步完成。不允许的路径会抛 PathNotAllowed，
        # 由 execute() 转成失败结果，工具本体碰不到非法路径。
        target = Path(policy.check_path(path, must_be_dir=True))

        limit = policy.max_list_entries()
        try:
            entries = list(os.scandir(target))
        except PermissionError as exc:
            raise ToolError(f"没有权限打开这个文件夹：{target}（{exc}）") from exc
        except OSError as exc:
            raise ToolError(f"打不开这个文件夹：{target}（{exc}）") from exc

        dirs = sorted((e for e in entries if e.is_dir()), key=lambda e: e.name.lower())
        files = sorted((e for e in entries if not e.is_dir()), key=lambda e: e.name.lower())
        total = len(dirs) + len(files)

        lines = [f"文件夹：{target}", f"共 {total} 项（{len(dirs)} 个文件夹，{len(files)} 个文件）", ""]

        shown = 0
        for entry in dirs + files:
            if shown >= limit:
                break
            try:
                stat = entry.stat()
                modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                if entry.is_dir():
                    lines.append(f"  [文件夹] {entry.name}")
                else:
                    lines.append(f"  [文件]   {entry.name}  ({_format_size(stat.st_size)}, {modified})")
            except OSError:
                lines.append(f"  [未知]   {entry.name}（读不到详细信息）")
            shown += 1

        if total > shown:
            lines.append("")
            lines.append(f"（只显示了前 {shown} 项，一共 {total} 项）")

        return ToolResult(ok=True, output="\n".join(lines))
