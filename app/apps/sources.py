"""各个发现来源。

每个来源一个函数，各自返回一批 AppRecord，全部**只读**。
不负责去重、也不负责存储——那是 index.py 的事。

关于"增量"的说明（避免含糊其辞）：
  - **文件系统来源**（开始菜单、安装目录）：调用方传进来一个 cache
    （上次扫描的结果，按身份键索引）。文件没变过的直接复用上次的解析结果，
    **不重新读文件**。这是增量的关键。
  - **注册表来源**（App Paths、卸载表、商店应用）：winreg 拿不到"键的修改时间"，
    而枚举几百个键本来就是毫秒级，所以**每次全量重枚举**。
    增量优化的收益只留给昂贵的文件系统遍历，这点不装作有。
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from app.apps import guard
from app.apps.lnk import parse_lnk
from app.apps.model import (
    AppRecord,
    CacheEntry,
    LaunchKind,
    ScanResult,
    Source,
    normalize,
)
from app.apps.uwp import scan_packages

logger = logging.getLogger(__name__)

# 身份键 -> 上次的解析结果 + 那个文件当时的指纹
Cache = dict[str, CacheEntry]


@dataclass
class SourceScan:
    """一个来源的扫描结果，带上耗时和出错信息，供索引记账。"""

    source_name: str
    result: ScanResult = field(default_factory=ScanResult)
    error: str = ""


# ----------------------------------------------------------------------
# 配置读取（配置里还没有这些键时要能正常工作）
# ----------------------------------------------------------------------


def _discovery():
    try:
        from app.settings import get_tools_config

        return get_tools_config().discovery
    except Exception:  # noqa: BLE001 - 配置缺失不该让整个发现功能挂掉
        logger.debug("读不到 discovery 配置，用默认值", exc_info=True)
        return None


def _cfg(name: str, default):
    cfg = _discovery()
    if cfg is None:
        return default
    return getattr(cfg, name, default)


def _unchanged(cache: Cache, identity: str, stamp: tuple[int, int] | None) -> CacheEntry | None:
    """这个文件/目录没变过吗？没变就返回缓存条目（record 可能为 None），变了返回 None。

    比的是**那个文件自己**的指纹，而不是记录的指纹（见 CacheEntry 的说明）。
    返回 CacheEntry 而不是 AppRecord，是因为有些身份键上次就没解析出应用
    （record 为 None），但它们的"没变化"同样值得跳过——不能因为没结果就次次重扫。
    """
    entry = cache.get(identity)
    if entry is None or stamp is None:
        return None
    if (entry.mtime_ns, entry.size) == stamp:
        return entry
    return None


def _cached(cache: Cache, identity: str) -> AppRecord | None:
    """只取上次的记录，不看指纹（注册表来源没有文件指纹这回事）。"""
    entry = cache.get(identity)
    return entry.record if entry else None


# ----------------------------------------------------------------------
# 注册表小工具（全部只读，64 位和 32 位两个视图都要查）
# ----------------------------------------------------------------------


def _views() -> list[int]:
    if sys.platform != "win32":
        return []
    import winreg

    return [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]


def _subkeys(hive, path: str, view: int) -> list[str]:
    import winreg

    out: list[str] = []
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view) as key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                out.append(winreg.EnumKey(key, i))
    except OSError:
        pass
    return out


def _value(hive, path: str, name: str, view: int):
    import winreg

    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _clean_path(raw: object) -> str:
    """注册表里的路径常带引号，清理干净。"""
    text = str(raw or "").strip().strip('"').strip()
    return os.path.expandvars(text)


# ----------------------------------------------------------------------
# 来源一：开始菜单快捷方式
# ----------------------------------------------------------------------


def _start_menu_roots() -> list[tuple[Source, Path]]:
    roots: list[tuple[Source, Path]] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append((
            Source.START_MENU_USER,
            Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        ))
    programdata = os.environ.get("ProgramData")
    if programdata:
        roots.append((
            Source.START_MENU_ALL,
            Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        ))
    return roots


def _record_from_lnk(lnk_path: Path, source: Source, stat) -> AppRecord | None:
    name = lnk_path.stem.strip()
    if not name:
        return None
    if guard.is_excluded_name(name):
        return None
    # 快捷方式本身就可能叫 cmd.exe —— 名字上先拦一道
    if guard.is_blocked_executable(f"{name}.exe"):
        return None

    info = parse_lnk(lnk_path)

    if info.resolved_ok and info.target:
        if not guard.is_worthy_of_index(name, info.target):
            return None
        kind = LaunchKind.EXE
        target = info.target
        resolved = info.target
    else:
        # 解析不出来：退回"把这个快捷方式交给系统打开"。
        # 身份降级为"仅名字"，但仍然可以启动，由用户在授权卡片上把关。
        if guard.is_excluded_path(str(lnk_path)):
            return None
        kind = LaunchKind.LNK
        target = str(lnk_path)
        resolved = info.target

    workdir = info.workdir
    if not workdir and resolved:
        workdir = str(Path(resolved).parent)

    return AppRecord(
        name=name,
        source=source,
        launch_kind=kind,
        target=target,
        identity_key=str(lnk_path),
        aliases=[lnk_path.name] if lnk_path.name != name else [],
        args=info.args,
        workdir=workdir,
        resolved=resolved,
        process_hint=Path(resolved).name if resolved else "",
        fs_mtime_ns=stat.st_mtime_ns,
        fs_size=stat.st_size,
        extra={"lnk": str(lnk_path), "note": info.note},
    )


def scan_start_menu(cache: Cache) -> ScanResult:
    result = ScanResult()
    for source, root in _start_menu_roots():
        if not root.is_dir():
            continue
        try:
            entries = list(root.rglob("*.lnk"))
        except OSError as exc:
            logger.warning("扫描开始菜单失败（%s）：%s", root, exc)
            continue
        for lnk_path in entries:
            try:
                stat = lnk_path.stat()
            except OSError:
                continue
            identity = str(lnk_path)
            stamp = (stat.st_mtime_ns, stat.st_size)
            result.fingerprints[identity] = stamp
            entry = _unchanged(cache, identity, stamp)
            if entry is not None:
                if entry.record is not None:
                    result.records.append(entry.record)   # 没变过，不重新解析
                continue
            result.reparsed += 1
            try:
                record = _record_from_lnk(lnk_path, source, stat)
            except Exception:  # noqa: BLE001 - 一个坏快捷方式不该毁掉整次扫描
                logger.warning("解析快捷方式出错：%s", lnk_path, exc_info=True)
                continue
            if record is not None:
                result.records.append(record)
    return result


# ----------------------------------------------------------------------
# 来源二：注册表 App Paths
# ----------------------------------------------------------------------


def scan_app_paths(cache: Cache) -> ScanResult:
    """按 exe 名注册的路径。指向 WindowsApps 的跳过——
    那个目录权限受限，直接启动一定失败，商店应用另有专门来源。"""
    if sys.platform != "win32":
        return ScanResult()
    import winreg

    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    result = ScanResult()
    seen: set[str] = set()

    for hive, hive_label in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"), (winreg.HKEY_CURRENT_USER, "HKCU")):
        for view in _views():
            for sub in _subkeys(hive, base, view):
                target = _clean_path(_value(hive, f"{base}\\{sub}", "", view))
                if not target or target.casefold() in seen:
                    continue
                seen.add(target.casefold())
                if "\\windowsapps\\" in target.casefold():
                    continue    # 权限受限，直接启动必失败
                name = Path(target).stem
                if not guard.is_worthy_of_index(name, target):
                    continue
                try:
                    if not Path(target).is_file():
                        continue
                except OSError:
                    continue

                # 32 位视图下同一个程序会再出现一次，用 hive+视图做身份键区分
                identity = f"{hive_label}\\{sub}\\{view}"
                old = _cached(cache, identity)
                if old is not None and old.target == target:
                    result.records.append(old)
                    continue
                if old is None:
                    result.reparsed += 1
                result.records.append(AppRecord(
                    name=name,
                    source=Source.APP_PATHS,
                    launch_kind=LaunchKind.EXE,
                    target=target,
                    identity_key=identity,
                    resolved=target,
                    process_hint=Path(target).name,
                    workdir=str(Path(target).parent),
                    extra={"registered_as": sub, "hive": hive_label},
                ))
    return result


# ----------------------------------------------------------------------
# 来源三：卸载注册表里的安装路径
# ----------------------------------------------------------------------


def _find_main_exe(folder: Path, label: str, max_depth: int = 2) -> Path | None:
    """在安装目录里挑出"主程序"。挑不出来就返回 None（宁可不收，也不乱收）。"""
    if not folder.is_dir():
        return None
    target = normalize(label)
    best: tuple[float, int, Path] | None = None
    budget = 400
    try:
        for root, dirs, files in os.walk(folder):
            depth = len(Path(root).relative_to(folder).parts)
            if depth >= max_depth:
                dirs[:] = []
            for filename in files:
                budget -= 1
                if budget <= 0:
                    return best[2] if best else None
                if not filename.casefold().endswith(".exe"):
                    continue
                candidate = Path(root) / filename
                if guard.is_excluded_name(filename, str(candidate)) or guard.is_excluded_path(str(candidate)):
                    continue
                if guard.is_blocked_executable(filename):
                    continue
                ratio = SequenceMatcher(None, target, normalize(candidate.stem)).ratio()
                score = (ratio, -depth, candidate)
                if best is None or score[:2] > best[:2]:
                    best = score
    except OSError:
        return None
    # 名字对不上就先不收，免得把一堆内部组件塞进索引
    if best is None or best[0] < 0.34:
        return None
    return best[2]


def scan_uninstall(cache: Cache) -> ScanResult:
    if sys.platform != "win32":
        return ScanResult()
    import winreg

    bases = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    depth = int(_cfg("scan_depth", 2))
    result = ScanResult()
    seen: set[str] = set()

    for base in bases:
        for view in _views():
            for sub in _subkeys(winreg.HKEY_LOCAL_MACHINE, base, view):
                key_path = f"{base}\\{sub}"
                display = str(_value(winreg.HKEY_LOCAL_MACHINE, key_path, "DisplayName", view) or "").strip()
                location = _clean_path(_value(winreg.HKEY_LOCAL_MACHINE, key_path, "InstallLocation", view))
                if not display or not location:
                    continue
                if guard.is_excluded_name(display, location):
                    continue
                exe = _find_main_exe(Path(location), display, max_depth=max(1, depth))
                if exe is None:
                    continue
                exe_text = str(exe)
                if exe_text.casefold() in seen:
                    continue
                seen.add(exe_text.casefold())
                identity = f"uninstall|{key_path}|{view}"
                old = _cached(cache, identity)
                if old is not None and old.target == exe_text:
                    result.records.append(old)
                    continue
                result.reparsed += 1
                result.records.append(AppRecord(
                    name=display,
                    source=Source.UNINSTALL,
                    launch_kind=LaunchKind.EXE,
                    target=exe_text,
                    identity_key=identity,
                    resolved=exe_text,
                    process_hint=exe.name,
                    workdir=str(exe.parent),
                    extra={"install_location": location},
                ))
    return result


# ----------------------------------------------------------------------
# 来源四：扫描安装目录（只在配置指定的目录 + 几个常见目录，深度有限）
# ----------------------------------------------------------------------


def _scan_roots() -> list[Path]:
    roots: list[Path] = []
    for raw in (_cfg("extra_scan_dirs", []) or []):
        try:
            roots.append(Path(os.path.expandvars(str(raw))).expanduser())
        except (OSError, ValueError):
            continue
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Programs")
    # 注意：只扫这两个目录的**下一层子目录**，绝不递归整个 Program Files
    for sysroot in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if sysroot:
            roots.append(Path(sysroot))
    return [r for r in roots if r.is_dir()]


def scan_install_dirs(cache: Cache) -> ScanResult:
    """在每个安装目录下，给每个软件目录挑一个"主程序"。

    刻意不递归整棵目录树——Program Files 下有几万个 exe，
    全收进来只会把索引变成噪音。这里一条目录最多出一个应用。
    """
    depth = int(_cfg("scan_depth", 2))
    result = ScanResult()

    for root in _scan_roots():
        try:
            children = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            continue
        for folder in children:
            name = folder.name.strip()
            if not name or name.startswith(".") or guard.is_excluded_name(name):
                continue
            identity = f"dir|{folder}"
            try:
                stat = folder.stat()
            except OSError:
                continue
            stamp = (stat.st_mtime_ns, 0)
            result.fingerprints[identity] = stamp
            entry = _unchanged(cache, identity, stamp)
            if entry is not None:
                if entry.record is not None:
                    result.records.append(entry.record)
                continue
            exe = _find_main_exe(folder, name, max_depth=max(1, depth))
            result.reparsed += 1
            if exe is None:
                continue
            result.records.append(AppRecord(
                name=name,
                source=Source.INSTALL_DIR,
                launch_kind=LaunchKind.EXE,
                target=str(exe),
                identity_key=identity,
                resolved=str(exe),
                process_hint=exe.name,
                workdir=str(exe.parent),
                fs_mtime_ns=stat.st_mtime_ns,
                fs_size=0,
                extra={"scanned_from": str(root)},
            ))
    return result


# ----------------------------------------------------------------------
# 来源五：商店应用
# ----------------------------------------------------------------------


def scan_uwp(cache: Cache) -> ScanResult:
    result = ScanResult()
    for app in scan_packages():
        # 商店应用的包名/标识里也混着一堆服务、元数据之类的非应用条目，
        # 和别的来源共用同一套排除规则，免得两边标准不一致。
        if guard.is_excluded_name(app.name, app.package_name):
            continue
        old = _cached(cache, app.aumid)
        if old is not None and old.name == app.name:
            result.records.append(old)
            continue
        result.reparsed += 1
        result.records.append(AppRecord(
            name=app.name,
            source=Source.UWP,
            launch_kind=LaunchKind.AUMID,
            target=app.aumid,
            identity_key=app.aumid,
            process_hint=app.executable,
            extra={"package": app.package_name},
        ))
    return result


# ----------------------------------------------------------------------
# 来源六：tools.yaml 里手工配置的应用（优先级最高）
# ----------------------------------------------------------------------


def scan_manual(cache: Cache) -> ScanResult:
    result = ScanResult()
    try:
        from app.settings import get_tools_config

        apps = get_tools_config().apps
    except Exception:  # noqa: BLE001
        logger.debug("读不到手工配置的应用", exc_info=True)
        return result

    for key, entry in apps.items():
        if entry.path:
            target = os.path.expandvars(entry.path)
            kind = LaunchKind.EXE
        elif entry.startfile:
            target = entry.startfile
            kind = LaunchKind.STARTFILE
        else:
            continue
        result.records.append(AppRecord(
            name=entry.label or key,
            source=Source.MANUAL,
            launch_kind=kind,
            target=target,
            identity_key=f"manual|{key}",
            aliases=list(entry.aliases) + [key],
            resolved=target if kind is LaunchKind.EXE else "",
            process_hint=entry.process or "",
            workdir=str(Path(target).parent) if kind is LaunchKind.EXE else "",
            extra={"config_key": key},
        ))
    return result


# ----------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------

_ALL_SOURCES = (
    ("start_menu", scan_start_menu),
    ("app_paths", scan_app_paths),
    ("uninstall", scan_uninstall),
    ("install_dirs", scan_install_dirs),
    ("uwp", scan_uwp),
    ("manual", scan_manual),
)


def scan_all(cache: Cache | None = None) -> list[SourceScan]:
    """按配置跑一遍所有来源。单个来源出错只影响它自己。"""
    cache = cache or {}
    toggles = _cfg("sources", None)

    def enabled(name: str) -> bool:
        if toggles is None:
            return True
        return bool(getattr(toggles, name, True))

    out: list[SourceScan] = []
    for name, func in _ALL_SOURCES:
        if not enabled(name):
            continue
        scan = SourceScan(source_name=name)
        try:
            scan.result = func(cache)
        except Exception as exc:  # noqa: BLE001 - 一个来源崩了不该拖垮整次刷新
            logger.exception("扫描来源 %s 出错", name)
            scan.error = f"{type(exc).__name__}: {exc}"
        out.append(scan)
    return out
