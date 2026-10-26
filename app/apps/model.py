"""应用索引的数据结构。

这个模块只定义"数据长什么样"——不读文件、不碰注册表、不做任何判断。
怎么发现、怎么存、怎么匹配，分别在别的文件里。

核心是一条 AppRecord：一个"可以被启动的应用"。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ----------------------------------------------------------------------
# 文本归一化
# ----------------------------------------------------------------------


def normalize(text: str) -> str:
    """把名字归一化成方便比较的形式。

    做四件事：
      1. NFKC 归一化——把全角字符变成半角（"ＡＢＣ" -> "abc"）
      2. casefold——统一大小写（比 lower() 更彻底）
      3. 把下划线、连字符当成空格（"vs_code" 和 "vs code" 应当能对上）
      4. 去掉结尾的 .exe / .lnk，合并多余空格

    匹配用的都是归一化之后的形式，这样用户怎么写都能对得上。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\.(exe|lnk|com)$", "", text.strip())
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ----------------------------------------------------------------------
# 枚举
# ----------------------------------------------------------------------


class Source(str, Enum):
    """这个应用是从哪儿发现的。会显示在授权卡片和日志里。"""

    START_MENU_USER = "start_menu_user"   # 当前用户的开始菜单
    START_MENU_ALL = "start_menu_all"     # 所有用户的开始菜单
    APP_PATHS = "app_paths"               # 注册表 App Paths
    UNINSTALL = "uninstall"               # 卸载注册表里的安装路径
    INSTALL_DIR = "install_dir"           # 扫描安装目录找到的
    UWP = "uwp"                           # 商店应用
    MANUAL = "manual"                     # 你在 tools.yaml 里手写的

    @property
    def label(self) -> str:
        return _SOURCE_LABELS[self]


_SOURCE_LABELS: dict[Source, str] = {
    Source.START_MENU_USER: "开始菜单（当前用户）",
    Source.START_MENU_ALL: "开始菜单（所有用户）",
    Source.APP_PATHS: "注册表应用路径",
    Source.UNINSTALL: "注册表安装记录",
    Source.INSTALL_DIR: "安装目录",
    Source.UWP: "商店应用",
    Source.MANUAL: "手工配置",
}


class LaunchKind(str, Enum):
    """怎么启动它。"""

    EXE = "exe"            # 直接启动一个可执行文件
    LNK = "lnk"            # 交给系统打开一个快捷方式
    AUMID = "aumid"        # 商店应用，用启动标识（shell:AppsFolder\...）
    STARTFILE = "startfile"  # 交给系统按协议/标识打开（例如 mspaint）

    @property
    def label(self) -> str:
        return {
            LaunchKind.EXE: "可执行文件",
            LaunchKind.LNK: "快捷方式",
            LaunchKind.AUMID: "商店应用",
            LaunchKind.STARTFILE: "系统标识",
        }[self]


class LaunchStatus(str, Enum):
    """启动的结果。三态，必须分清楚，不许含糊。"""

    CONFIRMED = "confirmed"       # 有证据确认它真的起来了
    UNCONFIRMED = "unconfirmed"   # 发出请求了，但拿不到证据，不能打包票
    FAILED = "failed"             # 确实失败了，或者被守卫拦下了

    @property
    def ok(self) -> bool:
        return self is not LaunchStatus.FAILED


# ----------------------------------------------------------------------
# 索引里的一行
# ----------------------------------------------------------------------


@dataclass
class AppRecord:
    """一个可以被启动的应用。

    注意 target 和 resolved 的区别：
      - target 是"启动时交给系统的东西"（可能是 .lnk，也可能是启动标识）
      - resolved 是"尽力解析出来的真实程序路径"，只用于识别和展示，
        可能解析不出来（那就是空字符串）
    """

    name: str                                   # 显示名，例如"记事本"
    source: Source
    launch_kind: LaunchKind
    target: str                                 # exe 路径 / .lnk 路径 / 启动标识
    identity_key: str = ""                      # 去重用的身份键
    aliases: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    workdir: str = ""
    resolved: str = ""                          # 解析出的真实 exe，可能为空
    process_hint: str = ""                      # 进程名，用于启动后核实
    fs_mtime_ns: int = 0                        # 文件来源的变更检测用
    fs_size: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    # ---- 派生字段 ----

    @property
    def id(self) -> str:
        """稳定且唯一的编号。同一个应用每次扫描都必须得到同一个 id。"""
        key = f"{self.source.value}|{self.identity_key or self.target}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    @property
    def name_norm(self) -> str:
        return normalize(self.name)

    @property
    def alias_norms(self) -> list[str]:
        return [n for n in (normalize(a) for a in self.aliases) if n]

    @property
    def identity_norm(self) -> str:
        """用身份键的尾部当搜索词，例如路径的 exe 文件名。"""
        if self.identity_key:
            tail = re.split(r"[\\/]", self.identity_key)[-1]
            return normalize(tail)
        return ""

    def display_target(self) -> str:
        """给人看的目标。解析不出来时如实说明。"""
        if self.resolved:
            return self.resolved
        if self.launch_kind is LaunchKind.AUMID:
            return f"商店应用标识：{self.target}"
        if not self.target:
            return "（目标无法解析）"
        return f"{self.target}（目标无法解析）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source.value,
            "source_label": self.source.label,
            "launch_kind": self.launch_kind.value,
            "launch_label": self.launch_kind.label,
            "target": self.target,
            "resolved": self.resolved,
            "display_target": self.display_target(),
            "aliases": list(self.aliases),
            "enabled": self.enabled,
        }


@dataclass
class ScanResult:
    """一次扫描的结果。

    reparsed 是"这次真正重新解析了多少个"，给增量测试用来证明
    没有把整个索引重头解析一遍。
    """

    records: list[AppRecord] = field(default_factory=list)
    reparsed: int = 0
    # 文件系统来源把"每个文件/目录当时的指纹"记在这里（包括那些没解析出
    # 应用的目录），供下次增量扫描判断有没有变化。注册表来源没有文件指纹，
    # 这个字典就是空的。
    fingerprints: dict[str, tuple[int, int]] = field(default_factory=dict)


@dataclass
class CacheEntry:
    """增量扫描的缓存条目：上次解析出来的记录 + 那个文件当时的指纹。

    mtime/size 存的是**文件自己的**，不是记录带来的。这两者必须分开：
    去重时同一个 exe 可能由好几个文件发现，最后只留下一条记录，
    而那条记录只记得自己那个文件的指纹——被合并掉的那些文件
    拿它的指纹来比会永远对不上，于是次次都被当成"变过了"重新解析。
    """

    record: "AppRecord"
    mtime_ns: int = 0
    size: int = 0


@dataclass
class Candidate:
    """一个匹配候选。score 越大越像，reason 说明为什么像。"""

    record: AppRecord
    score: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = self.record.to_dict()
        d["score"] = round(self.score, 3)
        d["reason"] = self.reason
        return d


@dataclass
class LaunchOutcome:
    """一次启动尝试的结果。三态，不许编造成功。"""

    status: LaunchStatus
    message: str
    pid: int | None = None

    @property
    def ok(self) -> bool:
        return self.status.ok

    def as_text(self) -> str:
        return self.message
