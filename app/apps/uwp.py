"""商店应用（UWP）的发现。

没有 pywin32 就没有官方的 Shell 接口，但注册表里存的信息已经够用了。
本机实测：336 个包里能推导出启动标识的一百多个。

推导规则（实测得出，不是猜的）：
    包族名 = "名字_发行者ID"（从包全名里去掉版本和架构）
    启动标识 = f"{包族名}!{AppId}"

    而 AppId 就是包的**顶层子键名**：
      - 主流应用（计算器、记事本、画图…）顶层子键是 "App"
      - 另一些包的顶层子键直接就是 AppId（如 "Claude"、"winget"、"Microsoft.Xbox.App"）
      - 如果顶层子键叫 "Applications"，那它是容器，
        里面的每个子项名字**本身就是完整启动标识**，直接用

    实测分布：Applications 111 个，App 27 个，其余零散二十来个。

推导不出启动标识的包一律跳过——**绝不编造**。

显示名：
  - "Applications" 的子项自带 DisplayName，实测 748 个可直接用
  - 主流系统应用的名字是 ms-resource: 资源引用，读不出来，
    用下面的对照表补中文名；再不行就把包名拆成人话（尽力而为）

启动：os.startfile("shell:AppsFolder\\" + 启动标识")，交给系统。
验证：多数 UWP 应用跑在系统宿主进程里，进程名归不到自己，
所以启动后常常只能如实说"无法确认"——这不是缺陷，是诚实的回答。
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_PACKAGES_KEY = (
    r"Software\Classes\Local Settings\Software\Microsoft\Windows"
    r"\CurrentVersion\AppModel\Repository\Packages"
)

# 实测里明显不是"可启动应用"的子键
_NON_APP_SUBKEYS = frozenset({"ProtocolShim", "AppexNews"})

# 框架 / 资源 / 运行时类包，名字里带这些的一律不要
_NOISE_KEYWORDS = (
    "vclibs", "ui.xaml", "windowsappruntime", "runtime", ".net.native",
    "host", "proxy", "extension", "sdk", "test", "template",
    "installer", "languagepack", "resources", "sigma", "winget",
    "appinstaller", "services", "helppane",
)

# 常见系统应用的中文名。这些包的 DisplayName 是 ms-resource: 读不出来，
# 靠这张表补上。表之外的会退化成从包名拆出来的英文，不影响启动。
_KNOWN_NAMES: dict[str, str] = {
    "Microsoft.WindowsCalculator": "计算器",
    "Microsoft.WindowsNotepad": "记事本",
    "Microsoft.Paint": "画图",
    "Microsoft.Windows.Photos": "照片",
    "Microsoft.WindowsStore": "Microsoft Store 应用商店",
    "Microsoft.ScreenSketch": "截图工具",
    "Microsoft.WindowsCamera": "相机",
    "Microsoft.WindowsTerminal": "Windows 终端",
    "Microsoft.WindowsAlarms": "时钟",
    "Microsoft.WindowsSoundRecorder": "录音机",
    "Microsoft.WindowsMaps": "地图",
    "Microsoft.MicrosoftStickyNotes": "便笺",
    "Microsoft.WindowsFeedbackHub": "反馈中心",
    "Microsoft.GetHelp": "获取帮助",
    "Microsoft.ZuneMusic": "媒体播放器",
    "Microsoft.ZuneVideo": "电影和电视",
    "Microsoft.MicrosoftOfficeHub": "Office",
    "Microsoft.OutlookforWindows": "新版 Outlook",
    "Microsoft.YourPhone": "手机连接",
    "MicrosoftWindows.CrossDevice": "手机连接",
    "Microsoft.GamingApp": "Xbox",
    "Microsoft.WindowsCommunicationsApps": "邮件和日历",
    "Microsoft.Windows.DevHome": "开发主页",
}


@dataclass
class UwpApp:
    """一个商店应用。"""

    aumid: str        # 启动标识
    name: str         # 显示名
    package_name: str  # 包名（推导启动标识用的那个）
    executable: str = ""


def build_aumid(package_family: str, app_id: str) -> str:
    return f"{package_family}!{app_id}"


def package_family_of(package_full_name: str) -> str:
    """把包全名压成"包族名"。

    Microsoft.WindowsCalculator_11.2607.0.0_x64__8wekyb3d8bbwe
      -> Microsoft.WindowsCalculator_8wekyb3d8bbwe

    推不出来就返回空字符串（调用方会跳过这个包）。
    """
    if "__" not in package_full_name:
        return ""
    left, _, publisher = package_full_name.partition("__")
    publisher = publisher.strip()
    if not publisher:
        return ""
    parts = left.rsplit("_", 2)
    name = parts[0] if len(parts) == 3 else left
    name = name.strip()
    return f"{name}_{publisher}" if name else ""


def _humanize(package_name: str) -> str:
    """从包名拆出一个勉强能看的显示名（兜底用）。"""
    text = package_name
    for prefix in ("MicrosoftWindows.", "MicrosoftCorporationII.", "Microsoft."):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return spaced.strip() or package_name


def _usable_name(raw: str) -> str:
    """DisplayName 能不能直接拿来当名字用？

    两种读不出人话的要挡掉，交给调用方走兜底：
      - "ms-resource:..."  资源引用，还没查表
      - "@{...}"           资源字符串没解析成功留下的壳
    """
    text = (raw or "").strip()
    low = text.casefold()
    if not text or low.startswith("ms-resource:") or low.startswith("@{"):
        return ""
    return text


def _subkeys(path: str) -> list[str]:
    if sys.platform != "win32":
        return []
    import winreg

    out: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                out.append(winreg.EnumKey(key, i))
    except OSError:
        pass
    return out


def _values(path: str) -> dict[str, object]:
    if sys.platform != "win32":
        return {}
    import winreg

    out: dict[str, object] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
            for i in range(winreg.QueryInfoKey(key)[1]):
                name, value, _ = winreg.EnumValue(key, i)
                out[name] = value
    except OSError:
        pass
    return out


def _app_entries(package: str, family: str, top_keys: list[str]) -> list[tuple[str, str]]:
    """列出这个包里的启动标识，返回 [(启动标识, 该条目自己的名字), ...]。"""
    entries: list[tuple[str, str]] = []

    if "Applications" in top_keys:
        container = f"{_PACKAGES_KEY}\\{package}\\Applications"
        for child in _subkeys(container):
            # 容器里的子项名字本身就是完整启动标识
            own = _usable_name(str(_values(f"{container}\\{child}").get("DisplayName") or ""))
            entries.append((child, own))
        return entries

    for key in top_keys:
        if key in _NON_APP_SUBKEYS:
            continue
        entries.append((build_aumid(family, key), ""))
    return entries


def scan_packages() -> list[UwpApp]:
    """枚举所有能推导出启动标识的商店应用。只读注册表。"""
    if sys.platform != "win32":
        return []

    apps: list[UwpApp] = []
    seen: set[str] = set()

    for package in _subkeys(_PACKAGES_KEY):
        family = package_family_of(package)
        if not family:
            continue

        package_name = family.rsplit("_", 1)[0]
        lowered = package_name.casefold()
        if any(word in lowered for word in _NOISE_KEYWORDS):
            continue

        top_keys = _subkeys(f"{_PACKAGES_KEY}\\{package}")
        if not top_keys:
            continue

        raw_display = str(_values(f"{_PACKAGES_KEY}\\{package}").get("DisplayName") or "")
        package_name_display = _usable_name(raw_display) or _KNOWN_NAMES.get(package_name, "")

        entries = _app_entries(package, family, top_keys)
        if not entries:
            continue

        # 同一个包里可能有多个入口，名字重复的话加上标识后缀区分
        names: list[str] = []
        for aumid, own in entries:
            if own:
                names.append(own)
            elif package_name_display:
                names.append(package_name_display)
            else:
                app_id = aumid.split("!", 1)[-1]
                names.append(f"{_humanize(package_name)} ({app_id})")

        duplicated = len(names) > 1 and len(set(names)) < len(names)
        for (aumid, _), name in zip(entries, names):
            if aumid in seen:
                continue
            seen.add(aumid)
            if duplicated:
                name = f"{name} · {aumid.split('!', 1)[-1]}"
            apps.append(UwpApp(aumid=aumid, name=name, package_name=package_name))

    logger.info("商店应用：从 %d 个包里推导出 %d 个可启动应用", len(_subkeys(_PACKAGES_KEY)), len(apps))
    return apps
