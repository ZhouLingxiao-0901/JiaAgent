"""安全守卫：哪些程序绝对不许启动，哪些条目不许进索引。

这是整个应用发现功能里最要紧的一块。

信任边界不再是"我手写的 8 个名字"，而是：
    **来自可信来源目录、能解析出真实目标、且通过下面这些检查的条目。**

用在哪两处：
  1. sources.py / index.py 建索引时过滤一遍（闸门一：不准进索引）
  2. launcher.py 启动前再查一遍（闸门二：纵深防御）
两处**共用本模块的函数**，绝不各写一份——
一个安全清单被复制成两份，迟早有一份忘了更新。

⚠️ 这个清单只能追加，不能删减。
   模型只能报应用名，永远碰不到这些规则；但请一直保持这个性质。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.apps.model import LaunchKind

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 危险程序黑名单：按可执行文件名比对（不区分大小写）
#
# 这些都是"能间接执行任意命令"的东西。只要有一样能启动，
# "禁止执行任意 Shell 命令"这条规矩就形同虚设，所以一律不许。
# ----------------------------------------------------------------------
_BLOCKED_EXECUTABLES: frozenset[str] = frozenset({
    # 命令行解释器
    "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "wt.exe",
    # 脚本宿主
    "wscript.exe", "cscript.exe", "mshta.exe", "wsl.exe", "bash.exe",
    # 注册表
    "reg.exe", "regedit.exe", "regedt32.exe",
    # 系统管理 / 提权 / 计划任务
    "runas.exe", "schtasks.exe", "at.exe", "sc.exe", "netsh.exe",
    "wmic.exe", "mmc.exe", "gpedit.msc", "secpol.msc",
    # 加载并执行任意代码
    "rundll32.exe", "regsvr32.exe", "installutil.exe", "msbuild.exe",
    "msiexec.exe", "pcalua.exe", "forfiles.exe", "ieexec.exe",
    # 下载 / 传输
    "bitsadmin.exe", "certutil.exe", "curl.exe", "wget.exe", "ftp.exe",
    "telnet.exe", "nc.exe", "ncat.exe", "ssh.exe", "scp.exe",
    # 语言运行时（可以被拿来跑任意代码）
    "python.exe", "pythonw.exe", "py.exe", "node.exe", "npm.cmd",
    "java.exe", "javaw.exe", "ruby.exe", "perl.exe", "php.exe",
    # 磁盘 / 权限 / 进程
    "diskpart.exe", "format.com", "fsutil.exe", "takeown.exe",
    "icacls.exe", "cacls.exe", "attrib.exe", "subst.exe", "taskkill.exe",
    "bootrec.exe", "bcdedit.exe", "vssadmin.exe", "wbadmin.exe",
    # 其它容易被当成"万能入口"的
    "explorer.exe.scr", "msconfig.exe", "compmgmt.msc", "devmgmt.msc",
    "diskmgmt.msc", "eventvwr.msc", "services.msc", "taskschd.msc",
})

# ----------------------------------------------------------------------
# 排除规则：卸载程序、更新器、安装器、崩溃上报……都不该进索引。
#
# 理由有两个：一是这些启动起来不是用户想要的结果（点错就卸载了），
# 二是它们大多需要交互或管理员权限，让模型去碰只会制造麻烦。
# ----------------------------------------------------------------------
_EXCLUDED_KEYWORDS: tuple[str, ...] = (
    "unins", "uninstall", "卸载", "install", "setup", "installdir",
    "redist", "common files",
    "update", "updater", "upgrade", "升级", "patch", "hotfix",
    "repair", "修复", "maintenance", "维护",
    "crashpad", "crashreport", "crashhandler", "crashsender",
    "telemetry", "diagnostic", "werfault", "metadata",
    "readme", "license", "eula", "changelog",
    "vcredist", "dxsetup", "dotnetfx", ".net framework",
    "helper", "assistant_service", "daemon", "service", "svc",
    "注册", "激活", "activator", "keygen",
)

# 这些目录下的东西一律不要
_EXCLUDED_PATH_PARTS: tuple[str, ...] = (
    "\\temp\\", "\\tmp\\", "\\update\\", "\\updater\\",
    "\\installer\\", "\\uninstall\\", "\\cache\\", "\\crashpad\\",
    "\\回收站\\", "\\recycle",
)

# 只允许启动这些类型的目标；脚本和安装包一律拒绝
_ALLOWED_SUFFIXES: frozenset[str] = frozenset({".exe", ".lnk"})

# 明确不允许当作直接启动目标的扩展名（写法上留一份，便于阅读和测试）
_FORBIDDEN_SUFFIXES: frozenset[str] = frozenset({
    ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".hta", ".scr",
    ".msi", ".msp", ".reg", ".inf", ".cpl", ".jar",
    ".py", ".pyw", ".rb", ".pl", ".sh", ".com",
})


def _config_extra_blocked() -> set[str]:
    """从配置里读用户额外追加的黑名单。

    读不到就返回空——内置黑名单仍然生效，这里只是"允许你再加几个"。
    所以配置出问题不会让安全性变差，最多是少拦几个你自己加的名字。
    """
    try:
        from app.settings import get_tools_config

        extra = get_tools_config().security.blocked_executables or []
        return {str(n).strip().casefold() for n in extra if str(n).strip()}
    except Exception:  # noqa: BLE001 - 配置缺失/未更新时不该让整个功能挂掉
        logger.debug("读取额外的黑名单配置失败，按空处理", exc_info=True)
        return set()


def blocked_executables() -> set[str]:
    """完整的黑名单：内置的 + 你在配置里追加的。只增不减。"""
    return set(_BLOCKED_EXECUTABLES) | _config_extra_blocked()


def is_blocked_executable(target: str) -> bool:
    """这个目标是不是危险程序？只看可执行文件名。"""
    if not target:
        return False
    name = Path(target.strip().strip('"')).name.casefold()
    return name in blocked_executables()


def is_excluded_name(*texts: str) -> bool:
    """名字里带卸载/更新/安装之类的关键词吗？"""
    haystack = " ".join(t for t in texts if t).casefold()
    if not haystack:
        return False
    return any(word in haystack for word in _EXCLUDED_KEYWORDS)


def is_excluded_path(path: str) -> bool:
    """路径落在临时目录、更新目录之类的地方吗？"""
    if not path:
        return False
    low = path.casefold().replace("/", "\\")
    return any(part in low for part in _EXCLUDED_PATH_PARTS)


def check_target(target: str, kind: LaunchKind) -> tuple[bool, str]:
    """启动前的总检查。返回（是否放行, 不放行的原因）。

    商店应用（AUMID）和系统标识（startfile）没有文件路径，只检查黑名单。
    """
    if kind in (LaunchKind.AUMID, LaunchKind.STARTFILE):
        # 这类目标形如 "包族名!AppId" 或 "mspaint"，没有文件名可比对，
        # 它们的可信度来自索引本身（只能由 UWP 枚举产生）。
        return True, ""

    if not target:
        return False, "这个条目没有可启动的目标"

    path = Path(target.strip().strip('"'))
    name = path.name.casefold()

    if name in blocked_executables():
        return False, (
            f"出于安全考虑，不允许启动 {path.name}。"
            "这类程序可以间接执行任意命令，绕过了本项目「禁止执行任意 Shell 命令」的限制。"
            "如果你确实需要它，请自己手动打开。"
        )

    suffix = path.suffix.casefold()
    if suffix in _FORBIDDEN_SUFFIXES:
        return False, (
            f"不允许直接启动 {suffix} 这种类型的文件。只允许启动 .exe 和快捷方式。"
        )
    if suffix not in _ALLOWED_SUFFIXES:
        return False, f"不认识的文件类型：{suffix or '（没有扩展名）'}"

    if is_excluded_path(target):
        return False, "这个程序在临时目录或更新目录里，不属于正常安装的应用。"

    return True, ""


def is_worthy_of_index(name: str, target: str) -> bool:
    """值不值得收进索引（闸门一）。"""
    if not name.strip() or not target.strip():
        return False
    # 检查**整条路径**，不只是文件名。像 C:\Program Files\Common Files\ 下的
    # 共享组件，文件名看着无害，但路径本身就说明它不是"用户应用"，要挡住。
    if is_excluded_name(name, target):
        return False
    if is_excluded_path(target):
        return False
    if is_blocked_executable(target):
        return False
    # 只收"真的是个可执行程序"的目标。快捷方式可能指向文档、网页、文件夹，
    # 那些不是"应用"，收进来只会把索引弄脏。
    # 这一条同时挡住了"解析出来的路径是乱码"的情况（乱码通常没有 .exe 结尾）。
    suffix = Path(target.strip().strip('"')).suffix.casefold()
    if suffix not in _ALLOWED_SUFFIXES:
        return False
    return True
