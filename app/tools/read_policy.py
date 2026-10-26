"""只读文件理解的路径与内容安全策略。

这个模块和 `policy.py` 是两套**故意分开**的规则：

  policy.py       给"打开应用 / 列目录 / 写新文件"用，白名单可能很宽
                  （当前 config 里甚至有 D:\\），它只关心路径能不能碰，
                  不关心文件正文会不会离开这台电脑。

  read_policy.py  给"把正文交给模型"用。它只认 config/tools.yaml 里
                  filesystem.allowed_roots，**绝不回退**到 permissions。
                  留空就什么都读不了，而不是"沿用上一个白名单"。

所有判断都尽量失败关闭：路径看不懂、根目录太宽、黑名单规则写错、
符号链接/目录联接可能越界、编码不确定、文件在授权后变化……一律拒绝，
不猜、不扩大范围。

这里只做判断和读取，不执行任何文件内容，也不提供写/删/改名能力。
"""

from __future__ import annotations

import codecs
import logging
import os
import re
import stat as statmod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.errors import JiaAgentError
from app.settings import get_tools_config
from app.tools.policy import PathNotAllowed

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 错误类型：code 会直接变成界面上的状态
# ----------------------------------------------------------------------
class ReadPathNotFound(JiaAgentError):
    code = "file_not_found"
    http_status = 404


class ReadPathNotFile(JiaAgentError):
    code = "not_a_file"
    http_status = 400


class ReadPathNotDir(JiaAgentError):
    code = "not_a_directory"
    http_status = 400


class FileTooLarge(JiaAgentError):
    code = "file_too_large"
    http_status = 413


class BinaryFile(JiaAgentError):
    code = "binary_file"
    http_status = 415


class UnsupportedEncoding(JiaAgentError):
    code = "unsupported_encoding"
    http_status = 415


class SensitiveFile(PathNotAllowed):
    code = "sensitive_file"


class FileChanged(JiaAgentError):
    code = "file_changed"
    http_status = 409


class ReadFailed(JiaAgentError):
    code = "read_failed"
    http_status = 500


class NoAllowedRoots(PathNotAllowed):
    code = "no_allowed_roots"


class ReadPolicyConfigError(PathNotAllowed):
    code = "read_policy_error"
    http_status = 500


class UnsafePath(PathNotAllowed):
    code = "path_not_allowed"


# ----------------------------------------------------------------------
# 敏感名称 / 目录
#
# 只看扩展名是不够的：keys.txt、credentials、id_rsa 都可以没有扩展名，
# .env.local、.env.production 也不是 ".env" 这个精确文件名。所以这里
# 同时看文件名、文件名里的词和所在目录。
# ----------------------------------------------------------------------
_SENSITIVE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".env",
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".claude",
    "appdata", "secrets", "credentials", "credential",
})

_SENSITIVE_FILE_NAMES = frozenset({
    ".env", ".netrc", "_netrc", ".pgpass", ".git-credentials",
    ".npmrc", ".pypirc", ".htpasswd",
    "keys.txt", "key.txt", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", "credentials.json", "credentials.yaml", "credentials.yml",
    "secrets.json", "secrets.yaml", "secrets.yml", "secret.json",
    "password.txt", "passwords.txt", "passwd", "shadow",
    "authorized_keys", "known_hosts",
    "token.json", "tokens.json", "token.txt", "tokens.txt",
    "auth.json", "session.json",
    "ntuser.dat", "sam", "security",
})

_SENSITIVE_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")

# 文件名里出现这些完整词就拒绝。故意不用单独的 "token" / "auth"：
# tokenizer.py、auth.py 是正常代码；access_token.json 才是凭据文件。
_SENSITIVE_WORDS = frozenset({
    "credential", "credentials", "password", "passwords", "passwd",
    "secret", "secrets", "apikey",
})
_SENSITIVE_SUBSTRINGS = (
    "access_token", "refresh_token", "auth_token", "private_key", "privatekey",
)

_SENSITIVE_SUFFIXES = frozenset({
    ".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".kdbx",
    ".gpg", ".asc", ".ovpn", ".ppk", ".rdp", ".mobileconfig",
    ".token", ".secret", ".credentials",
    ".db", ".sqlite", ".sqlite3",
})

_SYSTEM_DIRS = frozenset({
    "windows", "system32", "syswow64", "winsxs",
    "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery", "boot", "efi",
})

# 这些目录在搜索时直接不进入。它们要么是依赖/缓存，要么通常有成千上万个
# 文件，搜下去既慢又没有意义。直接读取单个文件不受这个列表限制，
# 仍然要过白名单和敏感规则。
_EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn",
    ".venv", "venv", "node_modules", "site-packages", "dist-packages",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache",
    ".tox", ".nox", ".idea", ".vscode", ".next", ".nuxt", ".svelte-kit",
    "dist", "build", "target", "vendor", "bower_components",
    "coverage", "htmlcov", ".eggs",
})

_PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN PGP PRIVATE KEY BLOCK-----",
)
_PRIVATE_KEY_SCAN_OVERLAP = max(len(marker) for marker in _PRIVATE_KEY_MARKERS) - 1

_REPARSE_POINT = getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def private_key_scan_overlap() -> int:
    """分块扫描时需要保留的尾部字节数，避免漏掉跨块标记。"""
    return _PRIVATE_KEY_SCAN_OVERLAP


def reject_private_key_bytes(data: bytes) -> None:
    """按现有私钥标记扫描字节，供分块复制路径复用。

    只识别列出的 PEM/PGP 起始标记；不是完整的凭据检测器。
    """
    sample = data.upper()
    if any(marker in sample for marker in _PRIVATE_KEY_MARKERS):
        raise SensitiveFile("这个文件包含私钥内容，已拒绝读取")


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(high, value))


def _contains(parent: Path, child: Path) -> bool:
    """child 是否等于 parent 或位于 parent 下面（按真实路径、Windows 大小写）。"""
    try:
        p = os.path.normcase(os.path.abspath(os.path.realpath(parent)))
        c = os.path.normcase(os.path.abspath(os.path.realpath(child)))
        return os.path.commonpath([p, c]) == p
    except (OSError, ValueError):
        return False


def _same_identity(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _sensitive_reason(path: Path) -> str:
    for part in path.parts:
        if part.casefold() in _SENSITIVE_DIRS:
            return f"路径包含受保护的名称「{part}」"

    name = path.name.casefold()
    if name in _SENSITIVE_FILE_NAMES or name.startswith(".env"):
        return f"文件名「{path.name}」属于凭据或密钥类文件"

    for prefix in _SENSITIVE_PREFIXES:
        if name == prefix or name.startswith(prefix + ".") or name.startswith(prefix + "-"):
            return f"文件名「{path.name}」看起来是私钥"

    words = [word for word in re.split(r"[^a-z0-9]+", name) if word]
    if any(word in _SENSITIVE_WORDS for word in words):
        return f"文件名「{path.name}」包含凭据类关键词"
    if any(marker in name for marker in _SENSITIVE_SUBSTRINGS):
        return f"文件名「{path.name}」包含凭据类关键词"

    if path.suffix.casefold() in _SENSITIVE_SUFFIXES:
        return f"文件类型「{path.suffix}」默认按敏感文件处理"
    return ""


def _system_reason(path: Path) -> str:
    for part in path.parts:
        if part.casefold() in _SYSTEM_DIRS:
            return f"路径包含系统关键目录「{part}」"
    return ""


def _too_broad_root(path: Path) -> str:
    """拒绝整盘、用户主目录及其祖先、系统目录这类过宽的读取根。"""
    # 主目录无法可靠确定时不能跳过这项检查，否则会静默放行范围过宽的根。
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError, ValueError):
        return "无法可靠解析用户主目录，已拒绝把这个路径设为读取根"
    try:
        # 用 pathlib 判断祖先关系，不假设固定盘符或固定层数。
        if path == home:
            return "不能把整个用户主目录设为读取根"
        if path in home.parents:
            return "不能把用户主目录的上级目录设为读取根"
    except (OSError, RuntimeError, ValueError):
        # 跨盘符、路径风格不一致等无法可靠判断时失败关闭，而不是放行。
        return "无法可靠判断读取根与用户主目录的关系，已拒绝"
    if path.parent == path:
        return "不能把整个磁盘或文件系统根设为读取根"
    reason = _system_reason(path)
    if reason:
        return reason
    reason = _sensitive_reason(path)
    if reason:
        return reason
    return ""


def _validate_raw_syntax(raw: Any, *, max_chars: int, what: str = "路径") -> str:
    """在交给 Path.resolve() 之前先挡掉 UNC、设备路径、ADS、相对路径等。"""
    if not isinstance(raw, str):
        raise UnsafePath(f"{what}必须是字符串")
    text = raw.strip()
    if not text:
        raise UnsafePath(f"没有给出{what}")
    if len(text) > max_chars:
        raise UnsafePath(f"{what}太长了（{len(text)} 个字符，上限 {max_chars}）")
    if "\x00" in text:
        raise UnsafePath(f"{what}里包含空字符，已拒绝")
    if text.startswith("\\\\.\\") or text.startswith("\\\\?\\") or text.startswith("\\??\\"):
        raise UnsafePath("不支持 Windows 设备路径或 \\\\?\\ 前缀")
    if text.startswith("//./") or text.startswith("//?/"):
        raise UnsafePath("不支持 Windows 设备路径或 \\\\?\\ 前缀")
    if text.startswith("\\\\") or text.startswith("//"):
        raise UnsafePath("不支持 UNC 网络共享或网络设备路径")

    # D:foo 是"盘符相对路径"，会跟着当前目录跑，不能用。
    if re.match(r"^[A-Za-z]:(?![\\/])", text):
        raise UnsafePath(f"请使用{what}的绝对路径，例如 D:\\notes\\a.txt")

    # 盘符后面的第一个冒号只允许出现在 "D:" 这个位置；再出现就是
    # Windows 的备用数据流（file.txt:stream），会绕过普通文件检查。
    remainder = text[2:] if re.match(r"^[A-Za-z]:", text) else text
    if ":" in remainder:
        raise UnsafePath("不支持带备用数据流（ADS）的路径")

    if not (text.startswith("~") or os.path.isabs(text)):
        raise UnsafePath(f"请使用{what}的绝对路径，例如 D:\\notes\\a.txt")
    return text


def _resolve_existing(raw: str) -> Path:
    try:
        return Path(os.path.expanduser(raw)).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ReadPathNotFound(f"这个路径不存在：{raw}") from exc
    except RuntimeError as exc:
        # Windows 上符号链接/目录联接绕成环时可能抛 RuntimeError
        raise UnsafePath("这个路径包含循环链接，已拒绝") from exc
    except PermissionError as exc:
        raise ReadFailed(f"没有权限访问这个路径：{raw}") from exc
    except OSError as exc:
        raise ReadFailed(f"无法解析这个路径：{raw}（{exc}）") from exc


# ----------------------------------------------------------------------
# 指纹
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class FileFingerprint:
    """授权前固定下来的文件身份，执行时用来发现"文件被换了/改了"。

    这不是密码学哈希：它不读取正文，所以不违反"拒绝时绝不读内容"。
    它能挡住常见的替换、截断、追加和修改；如果有人在同一时间粒度内
    原地改写并恢复了 mtime，指纹无法证明内容完全一致。这一点在文档里
    明确写出，不夸大。
    """

    path: str
    size: int
    mtime_ns: int
    dev: int
    ino: int

    @classmethod
    def from_stat(cls, path: Path, st: os.stat_result) -> "FileFingerprint":
        return cls(
            path=str(path),
            size=int(st.st_size),
            mtime_ns=int(st.st_mtime_ns),
            dev=int(st.st_dev),
            ino=int(st.st_ino),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "dev": self.dev,
            "ino": self.ino,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "FileFingerprint":
        if not isinstance(data, dict):
            raise FileChanged("读取目标信息无效，请重新发起读取")
        keys = {"path", "size", "mtime_ns", "dev", "ino"}
        if set(data) != keys or not isinstance(data.get("path"), str):
            raise FileChanged("读取目标信息无效，请重新发起读取")
        values: dict[str, int] = {}
        for key in ("size", "mtime_ns", "dev", "ino"):
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise FileChanged("读取目标信息无效，请重新发起读取")
            values[key] = value
        return cls(path=data["path"], **values)


@dataclass(frozen=True)
class DirFingerprint:
    path: str
    dev: int
    ino: int

    @classmethod
    def from_stat(cls, path: Path, st: os.stat_result) -> "DirFingerprint":
        return cls(path=str(path), dev=int(st.st_dev), ino=int(st.st_ino))

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "dev": self.dev, "ino": self.ino}

    @classmethod
    def from_dict(cls, data: Any) -> "DirFingerprint":
        if not isinstance(data, dict):
            raise FileChanged("搜索目标信息无效，请重新发起搜索")
        keys = {"path", "dev", "ino"}
        if set(data) != keys or not isinstance(data.get("path"), str):
            raise FileChanged("搜索目标信息无效，请重新发起搜索")
        values: dict[str, int] = {}
        for key in ("dev", "ino"):
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise FileChanged("搜索目标信息无效，请重新发起搜索")
            values[key] = value
        return cls(path=data["path"], **values)


@dataclass(frozen=True)
class ResolvedTarget:
    path: Path
    stat: os.stat_result
    root: Path


# ----------------------------------------------------------------------
# 策略本体
# ----------------------------------------------------------------------
class ReadAccessPolicy:
    """只读文件理解的唯一权限入口。

    allowed_roots 为空 = 拒绝一切正文读取。构造时会丢掉过宽/敏感的根，
    并在日志里说明原因；不会把坏规则"放宽"成能用的规则。

    strict_roots=True 时，只要有一项根被丢弃、同时又还有可用根，就记为
    配置错误并拒绝继续；避免静默改用剩余根目录。默认关闭，保持 filesystem
    等现有调用方的行为不变。CC 交接使用严格模式。
    """

    def __init__(
        self,
        *,
        allowed_roots: list[str] | tuple[str, ...] | str = (),
        blocked_paths: list[str] | tuple[str, ...] | str = (),
        max_file_bytes: int = 200_000,
        max_read_output_chars: int = 400_000,
        max_search_files: int = 500,
        max_search_results: int = 100,
        max_search_file_bytes: int = 200_000,
        max_search_output_chars: int = 20_000,
        max_context_lines: int = 3,
        max_line_chars: int = 500,
        max_query_chars: int = 200,
        max_path_chars: int = 4096,
        config_label: str = "filesystem",
        strict_roots: bool = False,
    ) -> None:
        if isinstance(allowed_roots, str):
            allowed_roots = [allowed_roots]
        if isinstance(blocked_paths, str):
            blocked_paths = [blocked_paths]

        # 错误文案里的配置段名字。默认保持 filesystem；CC 交接传 "cc"，
        # 免得用户看到"文件读取"却其实是在准备 CC 快照。
        self.config_label = str(config_label or "filesystem")

        self.max_file_bytes = _clamp_int(max_file_bytes, 1, 5_000_000, 200_000)
        self.max_read_output_chars = _clamp_int(
            max_read_output_chars, 1_000, 2_000_000, 400_000
        )
        self.max_search_files = _clamp_int(max_search_files, 1, 10_000, 500)
        self.max_search_results = _clamp_int(max_search_results, 1, 1_000, 100)
        self.max_search_file_bytes = _clamp_int(
            max_search_file_bytes, 1, 1_000_000, 200_000
        )
        self.max_search_output_chars = _clamp_int(
            max_search_output_chars, 500, 200_000, 20_000
        )
        self.max_context_lines = _clamp_int(max_context_lines, 0, 5, 3)
        self.max_line_chars = _clamp_int(max_line_chars, 80, 2_000, 500)
        self.max_query_chars = _clamp_int(max_query_chars, 1, 500, 200)
        self.max_path_chars = _clamp_int(max_path_chars, 100, 8192, 4096)

        self._config_error = ""
        self._strict_roots = strict_roots is True
        self._roots: tuple[Path, ...] = ()
        self._blocked: tuple[Path, ...] = ()
        self._load_roots(allowed_roots)
        self._load_blocked(blocked_paths)

    # ------------------------------------------------------------------
    def _load_roots(self, raw_roots: Any) -> None:
        roots: list[Path] = []
        seen: set[str] = set()
        rejected = 0
        for raw in raw_roots or []:
            try:
                text = _validate_raw_syntax(raw, max_chars=self.max_path_chars,
                                            what="读取根目录")
                candidate = Path(os.path.expanduser(text)).resolve(strict=False)
            except (UnsafePath, OSError, RuntimeError, ValueError) as exc:
                logger.warning("忽略 %s.allowed_roots 中的无效项 %r：%s", self.config_label, raw, exc)
                rejected += 1
                continue

            reason = _too_broad_root(candidate)
            if reason:
                logger.warning(
                    "拒绝过宽的读取根 %s：%s。为安全起见，这一项不会生效。",
                    candidate, reason,
                )
                rejected += 1
                continue
            if candidate.exists() and not candidate.is_dir():
                logger.warning("读取根 %s 不是文件夹，已忽略", candidate)
                rejected += 1
                continue

            key = os.path.normcase(str(candidate))
            if key not in seen:
                seen.add(key)
                roots.append(candidate)
        self._roots = tuple(roots)
        # 没有可用根时由 _ensure_ready() 统一报 NoAllowedRoots；这里只在
        # "仍有根可用、但另有坏项被静默跳过" 的情况下失败关闭，防止悄悄
        # 改用剩余根目录而偏离配置意图。
        if self._strict_roots and rejected and roots:
            self._config_error = (
                f"{self.config_label}.allowed_roots 中有 {rejected} 项无效或不被允许，"
                "已拒绝使用剩余根目录。请修正或删除这些项后重启服务。"
            )
            logger.error(self._config_error)

    def _load_blocked(self, raw_paths: Any) -> None:
        blocked: list[Path] = []
        for raw in raw_paths or []:
            try:
                text = _validate_raw_syntax(raw, max_chars=self.max_path_chars,
                                            what="禁止访问的路径")
                blocked.append(Path(os.path.expanduser(text)).resolve(strict=False))
            except (UnsafePath, OSError) as exc:
                # 黑名单规则看不懂，就无法证明它挡住了什么。失败关闭：
                # 所有读取都拒绝，直到配置被修正。
                self._config_error = (
                    f"{self.config_label}.blocked_paths 里有一条规则无法解析：{raw!r}（{exc}）。"
                    "在修正配置前，所有文件读取都会被拒绝。"
                )
                logger.error(self._config_error)
                return
        self._blocked = tuple(blocked)

    def ensure_ready(self) -> None:
        """公开的失败关闭检查：配置错误或没有有效根目录时拒绝继续。

        调用方需要在**创建任何目录/文件之前**执行它，不能先写再发现没授权。
        """
        self._ensure_ready()

    def _ensure_ready(self) -> None:
        if self._config_error:
            raise ReadPolicyConfigError(self._config_error)
        if not self._roots:
            raise NoAllowedRoots(
                "没有配置可读取的目录，已拒绝读取",
                detail=(
                    f"请在 config/tools.yaml 的 {self.config_label}.allowed_roots 中明确列出"
                    "允许读取的文件夹，然后重启服务。留空表示什么都不允许读，"
                    "不会自动沿用 permissions.allowed_roots。"
                ),
            )

    # ------------------------------------------------------------------
    @property
    def roots(self) -> tuple[Path, ...]:
        """允许读取的根目录（只读视图）。CC 交接按这里的顺序生成虚拟目录名。"""
        return self._roots

    def _allowed_root_for(self, target: Path) -> Path | None:
        for root in self._roots:
            if _contains(root, target):
                return root
        return None

    def _check_target(self, raw_path: Path, resolved: Path) -> Path:
        # 原始写法也要看一遍：名为 .env 的链接即使指向 notes.txt，也不该
        # 因为"真实文件不敏感"就放行。
        reason = _sensitive_reason(raw_path) or _sensitive_reason(resolved)
        if reason:
            raise SensitiveFile(f"这个文件受保护，已拒绝读取：{reason}")
        reason = _system_reason(raw_path) or _system_reason(resolved)
        if reason:
            raise PathNotAllowed(f"这个位置不允许读取：{reason}")

        for blocked in self._blocked:
            if _contains(blocked, resolved):
                raise PathNotAllowed(
                    f"这个位置不允许读取：{resolved}",
                    detail="它命中了 config/tools.yaml 里 "
                    f"{self.config_label}.blocked_paths 的规则。",
                )

        root = self._allowed_root_for(resolved)
        if root is None:
            readable = "、".join(str(item) for item in self._roots)
            raise PathNotAllowed(
                f"这个位置不在允许读取的范围内：{resolved}",
                detail=(
                    f"目前 {self.config_label}.allowed_roots 只允许：{readable}。"
                    "要增加请修改 config/tools.yaml 并重启服务；不会自动扩大范围。"
                ),
            )
        return root

    def resolve_file(self, raw: Any, *, max_bytes: int | None = None) -> ResolvedTarget:
        """校验一个要读取的文件。只做 stat，不打开、不读正文。"""
        self._ensure_ready()
        text = _validate_raw_syntax(raw, max_chars=self.max_path_chars)
        raw_path = Path(os.path.expanduser(text))
        resolved = _resolve_existing(text)
        root = self._check_target(raw_path, resolved)

        try:
            st = resolved.stat()
        except FileNotFoundError as exc:
            raise ReadPathNotFound(f"这个文件不存在：{resolved}") from exc
        except PermissionError as exc:
            raise ReadFailed(f"没有权限查看这个文件：{resolved}") from exc
        except OSError as exc:
            raise ReadFailed(f"无法查看这个文件：{resolved}（{exc}）") from exc

        if not statmod.S_ISREG(st.st_mode):
            raise ReadPathNotFile(f"这不是一个普通文本文件：{resolved}")

        limit = self.max_file_bytes if max_bytes is None else int(max_bytes)
        if st.st_size > limit:
            raise FileTooLarge(
                f"文件太大了：{st.st_size} 字节，上限是 {limit} 字节。"
                "请换一个小一点的文件，或先用搜索工具定位需要的内容。"
            )
        return ResolvedTarget(path=resolved, stat=st, root=root)

    def resolve_dir(self, raw: Any) -> ResolvedTarget:
        """校验一个要搜索的文件夹。只做 stat，不列目录、不读文件。"""
        self._ensure_ready()
        text = _validate_raw_syntax(raw, max_chars=self.max_path_chars)
        raw_path = Path(os.path.expanduser(text))
        resolved = _resolve_existing(text)
        root = self._check_target(raw_path, resolved)

        try:
            st = resolved.stat()
        except FileNotFoundError as exc:
            raise ReadPathNotFound(f"这个文件夹不存在：{resolved}") from exc
        except PermissionError as exc:
            raise ReadFailed(f"没有权限查看这个文件夹：{resolved}") from exc
        except OSError as exc:
            raise ReadFailed(f"无法查看这个文件夹：{resolved}（{exc}）") from exc

        if not statmod.S_ISDIR(st.st_mode):
            raise ReadPathNotDir(f"这不是一个文件夹：{resolved}")
        if is_excluded_dir_name(resolved.name):
            raise PathNotAllowed(
                f"这个文件夹属于依赖或缓存目录，搜索默认不进入：{resolved}",
                detail=(
                    "请直接搜索更具体的文件夹。这样既能避免结果被依赖代码淹没，"
                    "也能避免一次扫描成千上万个文件。"
                ),
            )
        return ResolvedTarget(path=resolved, stat=st, root=root)

    # ------------------------------------------------------------------
    def assert_file_unchanged(
        self, target: ResolvedTarget, fingerprint: Any
    ) -> None:
        fp = FileFingerprint.from_dict(fingerprint)
        if os.path.normcase(fp.path) != os.path.normcase(str(target.path)):
            raise FileChanged("读取目标和授权时固定的目标不一致，已停止")
        st = target.stat
        if (
            (fp.dev, fp.ino) != (st.st_dev, st.st_ino)
            or fp.size != st.st_size
            or fp.mtime_ns != st.st_mtime_ns
        ):
            raise FileChanged(
                "文件在你授权之后发生了变化（被修改、替换或截断），"
                "为了不返回错误内容，这次读取已停止。请重新发起读取。"
            )

    def assert_dir_unchanged(
        self, target: ResolvedTarget, fingerprint: Any
    ) -> None:
        fp = DirFingerprint.from_dict(fingerprint)
        if os.path.normcase(fp.path) != os.path.normcase(str(target.path)):
            raise FileChanged("搜索目标和授权时固定的目标不一致，已停止")
        st = target.stat
        if (fp.dev, fp.ino) != (st.st_dev, st.st_ino):
            raise FileChanged("文件夹在你授权之后被替换了，已停止这次搜索")

    # ------------------------------------------------------------------
    def read_text_verified(
        self, target: ResolvedTarget, *, max_bytes: int | None = None
    ) -> tuple[str, str]:
        """打开并读取，前后用 fstat 核对；发现变化就整次放弃，不返回半截内容。"""
        limit = self.max_file_bytes if max_bytes is None else int(max_bytes)
        try:
            with open(target.path, "rb") as fh:
                before = os.fstat(fh.fileno())
                if not _same_identity(before, target.stat):
                    raise FileChanged("打开的文件和授权时的文件不是同一个，已停止")
                if (
                    before.st_size != target.stat.st_size
                    or before.st_mtime_ns != target.stat.st_mtime_ns
                ):
                    raise FileChanged("文件在授权后、读取前发生了变化，已停止")
                if before.st_size > limit:
                    raise FileTooLarge(
                        f"文件太大了：{before.st_size} 字节，上限是 {limit} 字节"
                    )
                data = fh.read(limit + 1)
                after = os.fstat(fh.fileno())
        except (FileChanged, FileTooLarge, SensitiveFile):
            raise
        except FileNotFoundError as exc:
            raise ReadPathNotFound(f"这个文件不存在：{target.path}") from exc
        except PermissionError as exc:
            raise ReadFailed(f"没有权限读取这个文件：{target.path}") from exc
        except OSError as exc:
            raise ReadFailed(f"读取这个文件失败：{target.path}（{exc}）") from exc

        if len(data) > limit:
            raise FileTooLarge(
                f"文件在读取时超过了大小上限 {limit} 字节，已停止"
            )
        if (
            not _same_identity(before, after)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise FileChanged("文件在读取过程中发生了变化，已停止，不返回混合内容")

        text, encoding = decode_text(data)
        return text, encoding

    # ------------------------------------------------------------------
    def prune_reason(self, path: Path) -> str:
        """搜索时判断这个目录要不要直接跳过。返回空串表示可以进入。"""
        for part in path.parts:
            name = part.casefold()
            if name in _SENSITIVE_DIRS:
                return "sensitive"
            if name in _SYSTEM_DIRS:
                return "system"
        if is_excluded_dir_name(path.name):
            return "excluded"
        for blocked in self._blocked:
            if _contains(blocked, path):
                return "blocked"
        if not any(_contains(root, path) for root in self._roots):
            return "outside"
        return ""

    @classmethod
    def from_config(cls) -> "ReadAccessPolicy":
        cfg = get_tools_config().filesystem
        return cls(
            allowed_roots=cfg.allowed_roots,
            blocked_paths=cfg.blocked_paths,
            max_file_bytes=cfg.max_file_bytes,
            max_read_output_chars=cfg.max_read_output_chars,
            max_search_files=cfg.max_search_files,
            max_search_results=cfg.max_search_results,
            max_search_file_bytes=cfg.max_search_file_bytes,
            max_search_output_chars=cfg.max_search_output_chars,
            max_context_lines=cfg.max_context_lines,
            max_line_chars=cfg.max_line_chars,
            max_query_chars=cfg.max_query_chars,
            max_path_chars=cfg.max_path_chars,
        )


# ----------------------------------------------------------------------
# 搜索时用到的目录/入口判断
# ----------------------------------------------------------------------
def is_excluded_dir_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered in _EXCLUDED_DIRS
        or lowered.endswith(".egg-info")
        or lowered.endswith(".dist-info")
    )


def is_link_like(entry: os.DirEntry) -> bool:
    """符号链接、目录联接、以及任何 reparse point 都按链接处理。

    读取时跟随链接可能跳出 allowed_roots，所以搜索直接跳过它们；
    单个文件读取仍然可以解析链接，但最终真实路径必须仍在白名单内。
    """
    try:
        if entry.is_symlink():
            return True
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def decode_text(data: bytes) -> tuple[str, str]:
    """只接受 UTF-8 / UTF-8-SIG / 带 BOM 的 UTF-16/UTF-32。

    其他编码（GBK、无 BOM 的 UTF-16 等）一律报"编码不支持"，不猜。
    二进制（NUL、控制字符比例过高）在解码前后各挡一次。
    """
    if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
        try:
            text = data.decode("utf-32")
        except UnicodeDecodeError as exc:
            raise UnsupportedEncoding("文件自称 UTF-32，但无法解码") from exc
        _reject_binary_text(text, data)
        _reject_private_key(text, data)
        return text, "UTF-32"

    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise UnsupportedEncoding("文件自称 UTF-16，但无法解码") from exc
        _reject_binary_text(text, data)
        _reject_private_key(text, data)
        return text, "UTF-16"

    if data.startswith(codecs.BOM_UTF8):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise UnsupportedEncoding("文件自称 UTF-8，但无法解码") from exc
        _reject_binary_text(text, data)
        _reject_private_key(text, data)
        return text, "UTF-8 (BOM)"

    if b"\x00" in data[:8192]:
        raise BinaryFile("这看起来是二进制文件（包含 NUL 字节），已拒绝读取")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedEncoding(
            "这个文件不是 UTF-8 编码，当前只支持 UTF-8 / UTF-8-SIG / "
            "带 BOM 的 UTF-16 和 UTF-32。为避免乱码，已拒绝读取。"
        ) from exc

    _reject_binary_text(text, data)
    _reject_private_key(text, data)
    return text, "UTF-8"


def _reject_binary_text(text: str, data: bytes) -> None:
    sample = text[:8192]
    if not sample:
        return
    controls = sum(
        1 for ch in sample
        if (ord(ch) < 32 and ch not in "\n\r\t\f") or ch == "\x7f"
    )
    if controls > max(1, len(sample) // 50):
        raise BinaryFile("这看起来是二进制文件（控制字符过多），已拒绝读取")


def _reject_private_key(text: str, data: bytes) -> None:
    reject_private_key_bytes(data[:65536])
    upper = text[:65536].upper()
    if any(marker.decode("ascii") in upper for marker in _PRIVATE_KEY_MARKERS):
        raise SensitiveFile("这个文件包含私钥内容，已拒绝读取")
