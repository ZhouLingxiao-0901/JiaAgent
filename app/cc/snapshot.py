"""受限快照：只复制明确授权、非敏感、非链接的普通文本文件。

安全边界分三层：

  1. 路径与敏感规则复用 `read_policy.ReadAccessPolicy`，但使用 **cc 自己的**
     allowed_roots / blocked_paths，绝不回退到 filesystem 或 permissions；
  2. 目录遍历跳过所有符号链接、目录联接和 reparse point，不跟随；
  3. 复制前后都核对大小/mtime/dev/ino；复制过程中再核对打开的文件句柄。
     任何一个文件在窗口内变化，整次快照失败，不留下半成品。

本模块只读源文件、只写自己新建的临时快照目录。不执行任何文件内容。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat as statmod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.cc import protocol
from app.cc.protocol import ManifestEntry, TaskDocument
from app.errors import JiaAgentError
from app.settings import CcConfig
from app.tools import read_policy as rp

_MAX_VISITED_DIRS = 20_000
_MAX_TASK_DOC_BYTES = 2_000_000
_COPY_CHUNK_BYTES = 1024 * 1024
_REPARSE_POINT = getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

# CC 交接在通用读取规则之外的额外排除。这些不是"读取失败"，而是根本不应
# 进入交给第三方工具的工作区：日志、常见二进制/压缩包/办公文档。
_CC_EXCLUDED_DIRS = frozenset({"logs", "log", ".logs"})
_CC_BINARY_SUFFIXES = frozenset({
    ".bin", ".exe", ".dll", ".sys", ".so", ".dylib", ".class", ".jar",
    ".war", ".ear", ".pyc", ".pyo", ".o", ".obj", ".a", ".lib", ".pdb",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tif", ".tiff",
    ".pdf", ".zip", ".gz", ".tar", ".7z", ".rar", ".bz2", ".xz",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".ttf", ".otf", ".woff", ".woff2",
})


def _cc_exclusion_reason(
    path: Path,
    *,
    relative: str = "",
    is_directory: bool = False,
) -> str:
    """CC 快照的额外排除项。返回空串表示不在这一层被排除。

    `relative` 是相对于**授权根**的路径，而不是绝对路径。这样可以避免
    `D:\\logs\\project` 里的合法项目因为绝对路径祖先叫 logs 而被整包误杀。
    只有目录组件参与目录名排除；文件本身叫 log 不应被当成日志目录。
    """
    rel_parts: tuple[str, ...] = ()
    if relative:
        rel_parts = tuple(
            part for part in Path(relative).parts if part not in ("", ".")
        )
    dir_parts = rel_parts if is_directory else rel_parts[:-1]
    for part in dir_parts:
        if part.casefold() in _CC_EXCLUDED_DIRS:
            return "excluded"
    name = path.name.casefold()
    if name.endswith(".log") or ".log." in name:
        return "excluded"
    if path.suffix.casefold() in _CC_BINARY_SUFFIXES:
        return "binary"
    return ""


def _relative_to_root(path: Path, root: Path) -> str:
    """返回相对授权根的路径；根目录本身返回空串。"""
    try:
        relative = os.path.relpath(str(path), str(root))
    except ValueError:
        return path.name
    relative = relative.replace(os.sep, "/")
    return "" if relative == "." else relative


def relpath_key(value: str) -> str:
    """工作区相对路径的比较键。

    Windows 文件系统不区分大小写，仅大小写不同的路径会写到同一个位置；
    POSIX 区分大小写，不能无条件 casefold，否则会误伤合法的 a/A 两个文件。
    """
    return value.casefold() if os.name == "nt" else value


# ----------------------------------------------------------------------
# 错误
# ----------------------------------------------------------------------
class CcDisabled(JiaAgentError):
    code = "cc_disabled"
    http_status = 403


class CcLimitExceeded(JiaAgentError):
    code = "cc_limit_exceeded"
    http_status = 413


class CcSnapshotChanged(JiaAgentError):
    code = "cc_snapshot_changed"
    http_status = 409


class CcSnapshotFailed(JiaAgentError):
    code = "cc_snapshot_failed"
    http_status = 500


class CcPackageInvalid(JiaAgentError):
    code = "cc_package_invalid"
    http_status = 409


class CcResultChanged(JiaAgentError):
    code = "cc_result_changed"
    http_status = 409


class CcResultTooLarge(JiaAgentError):
    code = "cc_result_too_large"
    http_status = 413


# ----------------------------------------------------------------------
# 配置与候选
# ----------------------------------------------------------------------
def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(high, value))


@dataclass(frozen=True)
class CcPolicy:
    """cc 配置的硬夹取版本。坏值不会放宽，只会退回安全默认值。"""

    enabled: bool
    max_files: int
    max_total_bytes: int
    max_file_bytes: int
    max_result_bytes: int
    max_paths: int
    max_goal_chars: int
    deadline_hours: float
    read: rp.ReadAccessPolicy

    @classmethod
    def from_config(cls, cfg: CcConfig) -> "CcPolicy":
        max_total = _clamp_int(cfg.max_total_bytes, 1, 20_000_000, 2_000_000)
        max_file = _clamp_int(cfg.max_file_bytes, 1, 5_000_000, 200_000)
        # 单文件上限不能大于总量上限，否则口径会自相矛盾。
        max_file = min(max_file, max_total)
        max_result = _clamp_int(cfg.max_result_bytes, 1, 5_000_000, 100_000)
        max_paths = _clamp_int(cfg.max_paths, 1, 100, 20)
        max_goal = _clamp_int(cfg.max_goal_chars, 100, 20_000, 4_000)
        hours = float(cfg.deadline_hours) if isinstance(cfg.deadline_hours, (int, float)) else 24.0
        hours = max(0.1, min(168.0, hours))

        read = rp.ReadAccessPolicy(
            allowed_roots=cfg.allowed_roots,
            blocked_paths=cfg.blocked_paths,
            max_file_bytes=max_file,
            max_search_files=1,
            max_search_results=1,
            max_search_file_bytes=max_file,
            max_path_chars=4096,
            config_label="cc",
            # CC 交接不能静默跳过写错的根目录，改用剩下的根。
            strict_roots=True,
        )
        return cls(
            # 只接受真正的布尔 true；duck-typed 配置里的字符串 "false"
            # 不能被 bool() 转成启用状态。
            enabled=cfg.enabled is True,
            max_files=_clamp_int(cfg.max_files, 1, 1_000, 200),
            max_total_bytes=max_total,
            max_file_bytes=max_file,
            max_result_bytes=max_result,
            max_paths=max_paths,
            max_goal_chars=max_goal,
            deadline_hours=hours,
            read=read,
        )

    def ensure_enabled(self) -> None:
        if not self.enabled:
            raise CcDisabled(
                "Claude Code 交接功能未启用",
                detail=(
                    "请在 config/tools.yaml 的 cc.enabled 中设为 true，并明确配置 "
                    "cc.allowed_roots，然后重启服务。默认关闭，且不会回退到其他白名单。"
                ),
            )

    def ensure_can_create(self) -> None:
        """创建交接任务前检查开关和 CC 自己的 allowed_roots。

        必须在创建交接根目录之前调用；空根或坏规则失败关闭，绝不回退到
        filesystem / permissions 的白名单。
        """
        self.ensure_enabled()
        self.read.ensure_ready()
        for root in self.read.roots:
            try:
                if not root.is_dir():
                    raise rp.NoAllowedRoots(
                        f"cc.allowed_roots 中的目录不存在或不可访问：{root}",
                        detail=(
                            "交接任务只能从明确存在且可访问的目录创建。"
                            "请修正 config/tools.yaml 的 cc.allowed_roots 后重试；"
                            "不会回退到 filesystem 或 permissions 的白名单。"
                        ),
                    )
            except rp.NoAllowedRoots:
                raise
            except OSError as exc:
                raise rp.NoAllowedRoots(
                    f"无法确认 cc.allowed_roots 目录是否可用：{root}（{exc}）"
                ) from exc


@dataclass(frozen=True)
class CcCandidate:
    """确认前固定下来的源文件身份；不包含正文。"""

    source: str
    relpath: str
    size: int
    mtime_ns: int
    dev: int
    ino: int

    def as_injected(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "relpath": self.relpath,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "dev": self.dev,
            "ino": self.ino,
        }

    @classmethod
    def from_injected(cls, data: Any) -> "CcCandidate":
        if not isinstance(data, dict):
            raise CcSnapshotChanged("快照文件信息无效，请重新发起任务")
        keys = {"source", "relpath", "size", "mtime_ns", "dev", "ino"}
        if set(data) != keys:
            raise CcSnapshotChanged("快照文件信息无效，请重新发起任务")
        source = data.get("source")
        if not isinstance(source, str) or not os.path.isabs(source):
            raise CcSnapshotChanged("快照文件来源不是绝对路径，已停止")
        relpath = protocol.safe_relative_path(data.get("relpath"), field="快照文件路径")
        values: dict[str, int] = {}
        for key in ("size", "mtime_ns", "dev", "ino"):
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CcSnapshotChanged("快照文件信息无效，请重新发起任务")
            values[key] = value
        return cls(source=source, relpath=relpath, **values)


@dataclass(frozen=True)
class CcCollection:
    candidates: tuple[CcCandidate, ...]
    allowed_paths: tuple[str, ...]
    skipped: dict[str, int]
    total_bytes: int


# ----------------------------------------------------------------------
# 路径辅助
# ----------------------------------------------------------------------
def path_is_link_like(
    path: Path,
    *,
    error: type[JiaAgentError] = CcPackageInvalid,
) -> bool:
    """符号链接、目录联接、任何 reparse point 都按链接处理。

    与 `read_policy.is_link_like` 的判断一致，只是这个版本接收 Path，
    用于用户在 prepare 阶段明确给出的顶层路径。

    路径不存在是正常情况（例如还没有 result.json），返回 False；其他
    OSError 意味着“无法确认是否安全”，必须失败关闭，由调用方指定合适
    的错误类型。
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise error(f"无法确认路径是否为链接：{path}（{exc}）") from exc
    if statmod.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _workspace_path(workspace: Path, relpath: str) -> Path:
    """把已校验的相对路径映射到 workspace 内，并再做一次包含检查。"""
    protocol.safe_relative_path(relpath, field="工作区路径")
    target = workspace.joinpath(*relpath.split("/"))
    try:
        root = os.path.normcase(os.path.abspath(os.path.realpath(workspace)))
        child = os.path.normcase(os.path.abspath(os.path.realpath(target.parent)))
        if os.path.commonpath([root, child]) != root:
            raise CcSnapshotFailed("工作区路径超出了交接目录，已停止")
    except ValueError as exc:
        raise CcSnapshotFailed("工作区路径不合法，已停止") from exc
    return target


def _root_label(policy: rp.ReadAccessPolicy, root: Path) -> str:
    for index, item in enumerate(policy.roots, start=1):
        if os.path.normcase(str(item)) == os.path.normcase(str(root)):
            return f"root_{index}"
    raise CcSnapshotFailed("无法确定文件属于哪个允许目录，已停止")


def _stat_matches(candidate: CcCandidate, st: os.stat_result) -> bool:
    return (
        int(st.st_size) == candidate.size
        and int(st.st_mtime_ns) == candidate.mtime_ns
        and int(st.st_dev) == candidate.dev
        and int(st.st_ino) == candidate.ino
    )


# ----------------------------------------------------------------------
# 收集候选文件（只 stat，不读正文）
# ----------------------------------------------------------------------
def _add_candidate(
    *,
    candidate: CcCandidate,
    seen: dict[str, str],
    candidates: list[CcCandidate],
    total: int,
    policy: CcPolicy,
) -> int:
    key = relpath_key(candidate.relpath)
    previous = seen.get(key)
    if previous is not None:
        if os.path.normcase(previous) != os.path.normcase(candidate.source):
            raise CcSnapshotFailed(
                f"两个不同的源文件映射到了同一个工作区路径：{candidate.relpath}"
            )
        return total
    if len(candidates) + 1 > policy.max_files:
        raise CcLimitExceeded(
            f"文件数超过上限 {policy.max_files}；请缩小目录范围或调整配置后重试"
        )
    new_total = total + candidate.size
    if new_total > policy.max_total_bytes:
        raise CcLimitExceeded(
            f"文件总大小超过上限 {policy.max_total_bytes} 字节；"
            "请缩小范围或调整配置后重试"
        )
    seen[key] = candidate.source
    candidates.append(candidate)
    return new_total


def collect_candidates(
    policy: CcPolicy, raw_paths: list[str]
) -> CcCollection:
    """校验路径并把要复制的文件列出来。只做 stat 和目录枚举，不读正文。"""
    policy.ensure_can_create()
    read = policy.read
    candidates: list[CcCandidate] = []
    allowed_paths: list[str] = []
    skipped: dict[str, int] = {}
    seen: dict[str, str] = {}
    total = 0
    visited_dirs = 0

    def bump(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    def consider_file(path: Path, relpath: str) -> None:
        nonlocal total
        try:
            target = read.resolve_file(str(path), max_bytes=policy.max_file_bytes)
        except rp.SensitiveFile:
            bump("sensitive")
            return
        except rp.FileTooLarge as exc:
            raise CcLimitExceeded(
                f"单个文件超过上限 {policy.max_file_bytes} 字节：{path}",
                detail="请缩小范围，或先把不需要的大文件移出目录。",
            ) from exc
        except rp.PathNotAllowed:
            # 目录遍历时已经用 prune_reason 过滤过；走到这里说明规则之间
            # 有无法证明安全的空隙，失败关闭。
            raise
        except JiaAgentError:
            raise
        candidate = CcCandidate(
            source=str(target.path),
            relpath=relpath,
            size=int(target.stat.st_size),
            mtime_ns=int(target.stat.st_mtime_ns),
            dev=int(target.stat.st_dev),
            ino=int(target.stat.st_ino),
        )
        total = _add_candidate(
            candidate=candidate,
            seen=seen,
            candidates=candidates,
            total=total,
            policy=policy,
        )

    def walk(
        directory: Path,
        rel_prefix: str,
        *,
        source_prefix: str = "",
        depth: int = 0,
    ) -> None:
        nonlocal visited_dirs
        if depth > 100:
            raise CcLimitExceeded("目录层级太深，已停止；请缩小分析范围")
        visited_dirs += 1
        if visited_dirs > _MAX_VISITED_DIRS:
            raise CcLimitExceeded("目录数量太多，已停止；请缩小分析范围")
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
        except PermissionError as exc:
            raise rp.ReadFailed(f"没有权限查看这个文件夹：{directory}") from exc
        except OSError as exc:
            raise rp.ReadFailed(f"无法列出这个文件夹：{directory}（{exc}）") from exc

        for entry in entries:
            if rp.is_link_like(entry):
                bump("link")
                continue
            path = Path(entry.path)
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as exc:
                raise rp.ReadFailed(f"无法读取目录项：{path}（{exc}）") from exc

            relative = f"{rel_prefix}/{entry.name}"
            source_relative = (
                f"{source_prefix}/{entry.name}" if source_prefix else entry.name
            )
            extra = _cc_exclusion_reason(
                path, relative=source_relative, is_directory=is_dir
            )
            if extra:
                bump(extra)
                continue
            if is_dir:
                reason = read.prune_reason(path)
                if reason:
                    bump(reason)
                    continue
                walk(
                    path,
                    relative,
                    source_prefix=source_relative,
                    depth=depth + 1,
                )
            elif is_file:
                reason = read.prune_reason(path)
                if reason:
                    bump(reason)
                    continue
                consider_file(path, relative)
            else:
                bump("not_regular")

    for raw in raw_paths:
        if not isinstance(raw, str) or not raw.strip():
            raise CcSnapshotFailed("路径必须是非空字符串")
        text = raw.strip()

        # 顶层路径如果本身是链接/联接，直接拒绝；不跟随它去解析目标。
        if text.startswith("~") or os.path.isabs(text):
            if path_is_link_like(
                Path(os.path.expanduser(text)), error=rp.ReadFailed
            ):
                raise rp.UnsafePath(
                    "不允许把符号链接或目录联接作为 CC 交接的顶层路径"
                )

        try:
            target = read.resolve_dir(text)
            is_directory = True
        except rp.ReadPathNotDir:
            target = read.resolve_file(text, max_bytes=policy.max_file_bytes)
            is_directory = False

        # 排除规则只看相对授权根的路径，不看绝对路径的祖先目录。
        source_relative = _relative_to_root(target.path, target.root)
        extra = _cc_exclusion_reason(
            target.path, relative=source_relative, is_directory=is_directory
        )
        if extra:
            raise rp.UnsafePath(
                f"这个路径属于 CC 交接的排除范围（{extra}）：{target.path}。"
                "日志、二进制文件、压缩包和办公文档不会复制到工作区。"
            )

        label = _root_label(read, target.root)
        if is_directory:
            relative_root = f"{label}/{target.path.name}"
            protocol.safe_relative_path(relative_root, field="授权目录")
            before = len(candidates)
            walk(
                target.path,
                relative_root,
                source_prefix=source_relative,
            )
            if len(candidates) == before:
                # 空目录本身不是错误；但如果所有路径都没产生文件，最后会统一报错。
                pass
        else:
            relative_root = f"{label}/{target.path.name}"
            protocol.safe_relative_path(relative_root, field="授权文件")
            consider_file(target.path, relative_root)

        if relative_root not in allowed_paths:
            allowed_paths.append(relative_root)

    if not candidates:
        raise CcSnapshotFailed(
            "没有可复制的文件：路径为空、全是敏感/缓存文件，或目录里没有普通文本文件"
        )

    return CcCollection(
        candidates=tuple(candidates),
        allowed_paths=tuple(allowed_paths),
        skipped=dict(sorted(skipped.items())),
        total_bytes=total,
    )


# ----------------------------------------------------------------------
# 复制
# ----------------------------------------------------------------------
def _copy_verified(
    policy: CcPolicy,
    candidate: CcCandidate,
    destination: Path,
) -> ManifestEntry:
    """复制一个文件，并在打开前、复制中、复制后核对身份和大小。"""
    try:
        target = policy.read.resolve_file(
            candidate.source, max_bytes=policy.max_file_bytes
        )
    except JiaAgentError as exc:
        raise CcSnapshotChanged(
            f"源文件在授权后无法再次通过校验：{candidate.relpath}（{exc.message}）"
        ) from exc

    if not _stat_matches(candidate, target.stat):
        raise CcSnapshotChanged(
            f"源文件在授权后发生了变化：{candidate.relpath}；已停止，不生成交接包"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    scan_overlap = rp.private_key_scan_overlap()
    scan_tail = b""
    try:
        with open(target.path, "rb") as source_fh:
            before = os.fstat(source_fh.fileno())
            if (
                (before.st_dev, before.st_ino) != (target.stat.st_dev, target.stat.st_ino)
                or before.st_size != target.stat.st_size
                or before.st_mtime_ns != target.stat.st_mtime_ns
            ):
                raise CcSnapshotChanged(
                    f"打开的文件与授权时不是同一个：{candidate.relpath}；已停止"
                )
            with open(destination, "xb") as out_fh:
                while True:
                    chunk = source_fh.read(_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > candidate.size:
                        raise CcSnapshotChanged(
                            f"源文件在复制过程中变大了：{candidate.relpath}；已停止"
                        )
                    # 复制阶段才读正文；保留上一块尾部，检测跨块私钥标记。
                    scan_data = scan_tail + chunk
                    rp.reject_private_key_bytes(scan_data)
                    scan_tail = (
                        scan_data[-scan_overlap:] if scan_overlap else b""
                    )
                    digest.update(chunk)
                    out_fh.write(chunk)
            after = os.fstat(source_fh.fileno())
    except (CcSnapshotChanged,):
        raise
    except FileNotFoundError as exc:
        raise CcSnapshotChanged(
            f"源文件在复制时消失了：{candidate.relpath}；已停止"
        ) from exc
    except PermissionError as exc:
        raise rp.ReadFailed(
            f"没有权限复制这个文件：{candidate.relpath}"
        ) from exc
    except OSError as exc:
        raise CcSnapshotFailed(
            f"复制文件失败：{candidate.relpath}（{exc}）"
        ) from exc

    if written != candidate.size:
        raise CcSnapshotChanged(
            f"源文件在复制过程中被截断：{candidate.relpath}；已停止"
        )
    if (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise CcSnapshotChanged(
            f"源文件在复制过程中发生了变化：{candidate.relpath}；已停止"
        )

    # 关闭句柄后再看一次路径，挡住复制结束到返回之间被替换的情况。
    try:
        now = os.stat(target.path)
    except OSError as exc:
        raise CcSnapshotChanged(
            f"源文件在复制后无法再次确认：{candidate.relpath}；已停止"
        ) from exc
    if (
        (now.st_dev, now.st_ino) != (candidate.dev, candidate.ino)
        or now.st_size != candidate.size
        or now.st_mtime_ns != candidate.mtime_ns
    ):
        raise CcSnapshotChanged(
            f"源文件在复制结束后发生了变化：{candidate.relpath}；已停止"
        )

    try:
        dest_stat = destination.stat()
    except OSError as exc:
        raise CcSnapshotFailed(
            f"无法确认复制结果：{candidate.relpath}（{exc}）"
        ) from exc
    if dest_stat.st_size != candidate.size:
        raise CcSnapshotFailed(
            f"复制后的文件大小不一致：{candidate.relpath}"
        )

    return ManifestEntry(
        path=candidate.relpath,
        size=candidate.size,
        sha256=digest.hexdigest(),
    )


def copy_workspace(
    policy: CcPolicy,
    collection: CcCollection,
    workspace: Path,
) -> tuple[ManifestEntry, ...]:
    """把候选文件复制到 workspace，返回 manifest。任何异常由调用方清理临时目录。"""
    # 任何目录/文件创建之前先确认 CC 自己的授权仍然有效；直接调用这个
    # 函数时也不能先建 workspace 再发现没有可用根。
    policy.ensure_can_create()
    # 在创建 workspace 之前检查目标路径冲突：Windows 下仅大小写不同也
    # 会落到同一个文件；不能等复制到一半才发现并覆盖。
    seen_keys: set[str] = set()
    for candidate in collection.candidates:
        key = relpath_key(candidate.relpath)
        if key in seen_keys:
            raise CcSnapshotFailed("文件清单里有重复路径，已停止")
        seen_keys.add(key)
    workspace.mkdir(parents=True, exist_ok=False)
    # 先整体再 stat 一遍，尽量在写任何副本之前发现变化。
    for candidate in collection.candidates:
        try:
            target = policy.read.resolve_file(
                candidate.source, max_bytes=policy.max_file_bytes
            )
        except JiaAgentError as exc:
            raise CcSnapshotChanged(
                f"源文件在准备快照时无法再次通过校验：{candidate.relpath}"
            ) from exc
        if not _stat_matches(candidate, target.stat):
            raise CcSnapshotChanged(
                f"源文件在你确认之后、复制之前发生了变化：{candidate.relpath}；"
                "已停止，不生成交接包"
            )

    entries: list[ManifestEntry] = []
    for candidate in collection.candidates:
        destination = _workspace_path(workspace, candidate.relpath)
        if destination.exists():
            raise CcSnapshotFailed(f"工作区里已有同名文件：{candidate.relpath}")
        entries.append(_copy_verified(policy, candidate, destination))
    return tuple(entries)


# ----------------------------------------------------------------------
# 打包文件读写
# ----------------------------------------------------------------------
def write_json(path: Path, data: Any) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _open_for_read(path: Path, *, label: str) -> int:
    """打开文件；可用平台尽量同时使用 O_NOFOLLOW。

    Windows 没有可靠的 O_NOFOLLOW 语义，因此调用方仍必须在打开后再次
    lstat 路径并比较文件身份。这里只减少窗口，不能当成原子保证。
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except FileNotFoundError as exc:
        raise CcPackageInvalid(f"找不到 {label}") from exc
    except OSError as exc:
        raise CcPackageInvalid(f"无法打开 {label}（{exc}）") from exc


def _stat_is_link_like(st: os.stat_result) -> bool:
    if statmod.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _matches_expected_fingerprint(
    st: os.stat_result, expected: dict[str, Any]
) -> bool:
    """比较 lstat/fstat 结果与用户确认时记录的指纹。"""
    try:
        return (
            int(st.st_dev) == int(expected["dev"])
            and int(st.st_ino) == int(expected["ino"])
            and int(st.st_size) == int(expected["size"])
            and int(st.st_mtime_ns) == int(expected["mtime_ns"])
        )
    except (KeyError, TypeError, ValueError):
        # 指纹本身异常时也不能继续读取，按不一致处理。
        return False


def read_json_capped(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    expected_fingerprint: dict[str, Any] | None = None,
) -> Any:
    """读取服务端自己生成、但可能被改过的 JSON 文件。

    链接、非普通文件、二进制、超限、检查后/读取中变化都失败关闭。

    这不是原子打开：先 lstat、再 os.open、再对已打开句柄 fstat，最后
    重新 lstat 路径确认它仍然指向同一个普通文件。Windows 上无法像
    Linux 的 O_NOFOLLOW 那样完全消除“检查到打开”之间的替换窗口；
    这个顺序只能把窗口缩小并检测到大多数替换。硬链接、原地改写后
    恢复 mtime 等情况仍可能绕过检测，属于残余风险。

    当 expected_fingerprint 非空时，初始 lstat、打开后的 fstat、读取后的
    fstat 以及最终路径 lstat 都必须与用户确认时的指纹一致；不一致会抛
    CcResultChanged，避免把确认后替换的内容当成确认过的结果返回。这仍然
    不是原子打开，无法消除上述残余竞态。
    """
    if path_is_link_like(path):
        raise CcPackageInvalid(f"{label} 是链接或目录联接，已拒绝")
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise CcPackageInvalid(f"找不到 {label}") from exc
    except OSError as exc:
        raise CcPackageInvalid(f"无法查看 {label}（{exc}）") from exc
    if expected_fingerprint is not None and not _matches_expected_fingerprint(
        st, expected_fingerprint
    ):
        raise CcResultChanged(
            f"{label} 与用户确认时的文件身份或元数据不一致，已停止"
        )
    if _stat_is_link_like(st):
        raise CcPackageInvalid(f"{label} 是链接或目录联接，已拒绝")
    if not statmod.S_ISREG(st.st_mode):
        raise CcPackageInvalid(f"{label} 不是普通文件")
    if st.st_size > max_bytes:
        raise CcPackageInvalid(f"{label} 超过大小上限 {max_bytes} 字节")

    fd = _open_for_read(path, label=label)
    try:
        try:
            fh = os.fdopen(fd, "rb")
        except Exception:
            os.close(fd)
            raise
        with fh:
            before = os.fstat(fh.fileno())
            if not statmod.S_ISREG(before.st_mode):
                raise CcPackageInvalid(f"{label} 不是普通文件")
            if (before.st_dev, before.st_ino) != (st.st_dev, st.st_ino):
                raise CcPackageInvalid(f"{label} 在检查后发生了变化")
            if (
                before.st_size != st.st_size
                or before.st_mtime_ns != st.st_mtime_ns
            ):
                raise CcPackageInvalid(f"{label} 在检查后发生了变化")
            if expected_fingerprint is not None and not _matches_expected_fingerprint(
                before, expected_fingerprint
            ):
                raise CcResultChanged(
                    f"{label} 打开后与用户确认时的文件不一致，已停止"
                )

            # 打开后再看一次路径本身。Windows 上 os.open 会跟随链接；
            # 如果路径在这期间被换成链接，句柄可能已经指向链接目标。
            try:
                opened_path = os.lstat(path)
            except FileNotFoundError as exc:
                raise CcPackageInvalid(f"{label} 在打开后消失了") from exc
            except OSError as exc:
                raise CcPackageInvalid(
                    f"无法确认 {label} 打开后的状态（{exc}）"
                ) from exc
            if _stat_is_link_like(opened_path):
                raise CcPackageInvalid(f"{label} 在打开后变成了链接")
            if not statmod.S_ISREG(opened_path.st_mode):
                raise CcPackageInvalid(f"{label} 在打开后不是普通文件")
            if (
                (opened_path.st_dev, opened_path.st_ino)
                != (before.st_dev, before.st_ino)
            ):
                raise CcPackageInvalid(f"{label} 在打开后指向了另一个文件")

            data = fh.read(max_bytes + 1)
            after = os.fstat(fh.fileno())
    except CcPackageInvalid:
        raise
    except CcResultChanged:
        raise
    except FileNotFoundError as exc:
        raise CcPackageInvalid(f"找不到 {label}") from exc
    except OSError as exc:
        raise CcPackageInvalid(f"无法读取 {label}（{exc}）") from exc

    if expected_fingerprint is not None:
        if not _matches_expected_fingerprint(after, expected_fingerprint):
            raise CcResultChanged(
                f"{label} 读取后与用户确认时的文件不一致，已停止"
            )
        try:
            final_path = os.lstat(path)
        except FileNotFoundError as exc:
            raise CcResultChanged(f"{label} 在读取后消失了") from exc
        except OSError as exc:
            raise CcResultChanged(
                f"无法确认 {label} 读取后的状态（{exc}）"
            ) from exc
        if (
            _stat_is_link_like(final_path)
            or not statmod.S_ISREG(final_path.st_mode)
            or not _matches_expected_fingerprint(
                final_path, expected_fingerprint
            )
        ):
            raise CcResultChanged(
                f"{label} 读取后路径已不是用户确认时的文件，已停止"
            )

    if len(data) > max_bytes:
        raise CcPackageInvalid(f"{label} 超过大小上限 {max_bytes} 字节")
    if (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise CcPackageInvalid(f"{label} 在读取过程中发生了变化，已停止")

    text, _encoding = rp.decode_text(data)
    return protocol.parse_json_strict(text)


def read_task_document(path: Path, *, task_id: str) -> TaskDocument:
    """只读取并验证 task.json；用于在展示结果前检查 deadline。"""
    raw = read_json_capped(path, max_bytes=_MAX_TASK_DOC_BYTES, label="task.json")
    # max_files / max_total_bytes 在 task.json 里已经固定；这里用较宽的硬上限
    # 先做结构校验，随后服务层还会按当前配置再检查一遍。
    return protocol.validate_task_document(
        raw,
        expected_task_id=task_id,
        max_files=1_000,
        max_total_bytes=20_000_000,
        max_goal_chars=20_000,
        max_paths=100,
    )


def read_task_package(handoff_dir: Path, task_id: str) -> TaskDocument:
    """读取并验证 task.json + manifest.json，同时核对 task_id 与目录。"""
    task_path = handoff_dir / "task.json"
    manifest_path = handoff_dir / "manifest.json"
    try:
        task = read_task_document(task_path, task_id=task_id)
        raw_manifest = read_json_capped(
            manifest_path, max_bytes=_MAX_TASK_DOC_BYTES, label="manifest.json"
        )
        protocol.validate_manifest_document(raw_manifest, task)
    except JiaAgentError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CcPackageInvalid(f"读取任务包失败（{type(exc).__name__}）") from exc
    return task


# ----------------------------------------------------------------------
# 工作区完整性
# ----------------------------------------------------------------------
def _hash_file(
    path: Path,
    max_bytes: int,
    *,
    limit_label: str = "校验大小",
) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as fh:
            before = os.fstat(fh.fileno())
            if not statmod.S_ISREG(before.st_mode):
                raise CcPackageInvalid(f"工作区里的不是普通文件：{path.name}")
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise CcPackageInvalid(
                        f"工作区文件超过{limit_label}上限：{path.name}"
                    )
                digest.update(chunk)
            after = os.fstat(fh.fileno())
    except CcPackageInvalid:
        raise
    except FileNotFoundError as exc:
        raise CcPackageInvalid(f"工作区缺少文件：{path.name}") from exc
    except OSError as exc:
        raise CcPackageInvalid(f"无法读取工作区文件：{path.name}（{exc}）") from exc
    if (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise CcPackageInvalid(f"工作区文件在读取时发生变化：{path.name}")
    return total, digest.hexdigest()


def verify_workspace(
    task: TaskDocument,
    workspace: Path,
    max_total_bytes: int,
    *,
    max_file_bytes: int | None = None,
) -> None:
    """确认 workspace 与 manifest 完全一致：不多、不少、哈希相同。"""
    if path_is_link_like(workspace):
        raise CcPackageInvalid("workspace 是链接或目录联接，已拒绝")
    if not workspace.is_dir():
        raise CcPackageInvalid("workspace 不存在或不是目录")

    expected = {entry.path: entry for entry in task.manifest}
    found: set[str] = set()
    total = 0
    visited_dirs = 0
    visited_entries = 0

    def walk(directory: Path, prefix: str, depth: int = 0) -> None:
        nonlocal total, visited_dirs, visited_entries
        if depth > 100:
            raise CcPackageInvalid("workspace 目录层级太深，已拒绝")
        visited_dirs += 1
        if visited_dirs > 20_000:
            raise CcPackageInvalid("workspace 目录数量异常，已拒绝")
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError as exc:
            raise CcPackageInvalid(f"无法读取 workspace（{exc}）") from exc
        visited_entries += len(entries)
        if visited_entries > 100_000:
            raise CcPackageInvalid("workspace 条目数量异常，已拒绝")
        for entry in entries:
            if rp.is_link_like(entry):
                raise CcPackageInvalid(
                    f"workspace 里出现链接或目录联接：{prefix}{entry.name}"
                )
            path = Path(entry.path)
            relpath = f"{prefix}{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                walk(path, relpath + "/", depth + 1)
            elif entry.is_file(follow_symlinks=False):
                protocol.safe_relative_path(relpath, field="workspace 文件")
                found.add(relpath)
                expected_entry = expected.get(relpath)
                if expected_entry is None:
                    raise CcPackageInvalid(f"workspace 里多出未授权文件：{relpath}")
                per_file_limit = max_total_bytes
                limit_label = "校验大小"
                if max_file_bytes is not None and max_file_bytes <= max_total_bytes:
                    per_file_limit = max_file_bytes
                    limit_label = "单文件校验大小"
                size, digest = _hash_file(
                    path, per_file_limit, limit_label=limit_label
                )
                total += size
                if total > max_total_bytes:
                    raise CcPackageInvalid("workspace 总大小超过校验上限")
                if size != expected_entry.size or digest != expected_entry.sha256:
                    raise CcPackageInvalid(f"workspace 文件与 manifest 不一致：{relpath}")
            else:
                raise CcPackageInvalid(f"workspace 里有非常规文件：{relpath}")

    walk(workspace, "")
    missing = set(expected) - found
    if missing:
        raise CcPackageInvalid(
            "workspace 缺少 manifest 里的文件：" + "、".join(sorted(missing)[:5])
        )
