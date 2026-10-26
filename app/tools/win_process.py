"""枚举 Windows 上正在运行的进程名。

为什么要自己写这几行？
  整个项目只有"确认应用是不是真的打开了"需要用到进程列表。
  为这一件事去装 psutil（一个几百 KB 的第三方库）不划算，
  而调用 tasklist 命令又等于在工具里执行系统命令，不符合本项目的约束。
  所以直接用 ctypes 调 Windows 自带的进程快照 API：零依赖、零外部命令。

用法：
    find_process("Notepad.exe")   ->  [47908]
    list_process_names()          ->  {"notepad.exe": [47908], ...}
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260

if _IS_WINDOWS:
    from ctypes import wintypes

    class _ProcessEntry32(ctypes.Structure):
        """对应 Windows 的 PROCESSENTRY32 结构体。字段顺序不能改。"""

        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * MAX_PATH),
        ]


def list_process_names() -> dict[str, list[int]]:
    """返回 {进程名（小写）: [进程号, ...]}。读不到就返回空字典，不抛异常。"""
    if not _IS_WINDOWS:
        return {}

    result: dict[str, list[int]] = {}
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE or not snapshot:
        logger.warning("拿不到进程快照，无法确认进程状态")
        return result

    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
            return result
        while True:
            name = entry.szExeFile.decode("mbcs", errors="replace").lower()
            result.setdefault(name, []).append(int(entry.th32ProcessID))
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                break
    except Exception:  # noqa: BLE001 - 拿不到就当没有，绝不影响主流程
        logger.exception("枚举进程时出错")
    finally:
        kernel32.CloseHandle(snapshot)

    return result


def find_process(name: str) -> list[int]:
    """按进程名找进程号，不区分大小写。找不到返回空列表。"""
    if not name:
        return []
    return list_process_names().get(name.strip().lower(), [])
