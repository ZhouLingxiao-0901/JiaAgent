"""凭证和本地允许名单。

**凭证**
  * 只放在 `data/wechat/` 下（已被 .gitignore 的 `data/` 覆盖）。
  * Windows 上用 DPAPI（当前用户）加密；DPAPI 不可用时失败关闭，绝不写明文。
  * API token、context_token、二维码值、验证码不写日志。
  * 文件原子写入，初始权限 0600；拒绝符号链接/目录联接。

**允许名单**
  * 登录成功只说明“谁扫了码”，**不等于授权**。必须由本机管理员显式
    `pair` 才能发消息、派任务或批准任务。
  * 只按 `from_user_id` 精确匹配，不看昵称。
  * 每个条目带作用域：chat / task / approve。默认只给 chat；需要派任务或
    远程审批时必须由管理员显式追加。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import secrets
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from app.wechat import protocol
from app.wechat.errors import CredentialError

logger = logging.getLogger(__name__)

_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9@._-]{1,128}\Z")
_ALLOWED_SCOPES = frozenset({"chat", "task", "approve"})
_MAX_CREDENTIAL_BYTES = 64 * 1024
_MAX_ALLOWLIST_BYTES = 256 * 1024
_DPAPI_ENTROPY = b"Jia-WeChat/credentials/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    checker = getattr(os.path, "isjunction", None)
    if checker is not None:
        try:
            if checker(path):
                return True
        except OSError:
            return True
    return False


def _safe_account_id(account_id: str) -> str:
    text = str(account_id or "").strip()
    if not _ACCOUNT_RE.match(text) or ".." in text:
        raise CredentialError("账号 ID 含非法字符")
    return text


def _check_no_links(path: Path, root: Path | None = None) -> None:
    """拒绝目标文件以及 root 到目标之间任何一级目录是符号链接/目录联接。

    只查直接父目录不够：`root/credentials` 本身可能被替换成 junction，
    之后写进去的“加密凭证”就落到了别处。这里从目标一路查到 root（含）。
    """
    if _is_link_like(path):
        raise CredentialError(f"{path.name} 是链接，已拒绝")
    if root is None:
        return
    try:
        root = Path(root)
        relative = path.parent.relative_to(root)
    except ValueError:
        # 路径不在这棵树里（调用方失误）：至少保住直接父目录这一层。
        if _is_link_like(path.parent):
            raise CredentialError("凭证目录是链接，已拒绝")
        return
    current = path.parent
    if _is_link_like(root):
        raise CredentialError("凭证根目录是链接，已拒绝")
    for _ in range(len(relative.parts) + 1):
        if _is_link_like(current):
            raise CredentialError("凭证路径经过链接，已拒绝")
        if current == root:
            return
        current = current.parent
    raise CredentialError("凭证路径异常，已拒绝")


def _atomic_write(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    root: Path | None = None,
) -> None:
    _check_no_links(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 目录建好后再查一遍：mkdir 期间可能有并发替换。
    _check_no_links(path, root)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(temp, flags, mode)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except BaseException:
            try:
                temp.unlink()
            except OSError:
                pass
            raise
    except OSError as exc:
        raise CredentialError(f"无法安全写入 {path.name}") from exc


def _read_capped(
    path: Path, *, max_bytes: int, label: str, root: Path | None = None
) -> bytes:
    _check_no_links(path, root)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise CredentialError(f"无法读取 {label}") from exc
    if st.st_size > max_bytes:
        raise CredentialError(f"{label} 太大")
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise CredentialError(f"无法读取 {label}") from exc
    if len(data) > max_bytes:
        raise CredentialError(f"{label} 太大")
    return data


class Protector:
    def protect(self, data: bytes) -> bytes:
        raise NotImplementedError

    def unprotect(self, data: bytes) -> bytes:
        raise NotImplementedError


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _make_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_ubyte]]:
    if not data:
        raise CredentialError("不能加密空数据")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


class DpapiProtector(Protector):
    """Windows DPAPI，当前用户范围。非 Windows 或调用失败时抛错。"""

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialError("DPAPI 只在 Windows 上可用")
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    def protect(self, data: bytes) -> bytes:
        in_blob, in_buffer = _make_blob(data)  # noqa: F841 - 保持 buffer 存活
        entropy, entropy_buffer = _make_blob(_DPAPI_ENTROPY)  # noqa: F841
        out_blob = _DataBlob()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "Jia-WeChat credentials",
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise CredentialError("DPAPI 加密失败")
        try:
            return ctypes.string_at(out_blob.pbData, int(out_blob.cbData))
        finally:
            self._kernel32.LocalFree(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))

    def unprotect(self, data: bytes) -> bytes:
        in_blob, in_buffer = _make_blob(data)  # noqa: F841
        entropy, entropy_buffer = _make_blob(_DPAPI_ENTROPY)  # noqa: F841
        out_blob = _DataBlob()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise CredentialError("DPAPI 解密失败")
        try:
            return ctypes.string_at(out_blob.pbData, int(out_blob.cbData))
        finally:
            self._kernel32.LocalFree(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))


@dataclass(frozen=True)
class AccountCredentials:
    account_id: str
    token: str
    base_url: str
    user_id: str
    saved_at: str


class CredentialStore:
    def __init__(self, root: Path | str, protector: Protector) -> None:
        self.root = Path(root)
        self.protector = protector

    def _path(self, account_id: str) -> Path:
        safe = _safe_account_id(account_id)
        return self.root / "credentials" / safe / "account.bin"

    def save(self, credentials: AccountCredentials) -> None:
        account_id = _safe_account_id(credentials.account_id)
        token = str(credentials.token or "").strip()
        if not token:
            raise CredentialError("拒绝保存空 token")
        if len(token) > 8_192:
            raise CredentialError("token 太长")
        if not protocol.is_allowed_base_url(credentials.base_url):
            raise CredentialError("base_url 不在允许的微信域名内")
        payload = {
            "version": 1,
            "account_id": account_id,
            "token": token,
            "base_url": protocol.normalize_base_url(credentials.base_url),
            "user_id": str(credentials.user_id or "").strip(),
            "saved_at": str(credentials.saved_at or ""),
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        protected = self.protector.protect(raw)
        _atomic_write(self._path(account_id), protected, mode=0o600, root=self.root)

    def load(self, account_id: str) -> AccountCredentials | None:
        path = self._path(account_id)
        try:
            raw = _read_capped(
                path, max_bytes=_MAX_CREDENTIAL_BYTES, label="凭证文件",
                root=self.root,
            )
        except FileNotFoundError:
            return None
        plain = self.protector.unprotect(raw)
        try:
            data = json.loads(plain.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialError("凭证内容损坏") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise CredentialError("凭证版本不支持")
        if str(data.get("account_id", "")) != account_id:
            raise CredentialError("凭证账号不匹配")
        token = data.get("token")
        base_url = data.get("base_url")
        user_id = data.get("user_id", "")
        if not isinstance(token, str) or not token.strip():
            raise CredentialError("凭证缺少 token")
        if not isinstance(base_url, str) or not protocol.is_allowed_base_url(base_url):
            raise CredentialError("凭证 base_url 不合法")
        if not isinstance(user_id, str):
            raise CredentialError("凭证 user_id 不合法")
        return AccountCredentials(
            account_id=account_id,
            token=token.strip(),
            base_url=protocol.normalize_base_url(base_url),
            user_id=user_id.strip(),
            saved_at=str(data.get("saved_at", "")),
        )

    def list_accounts(self) -> list[str]:
        base = self.root / "credentials"
        try:
            _check_no_links(base, self.root)
        except CredentialError:
            return []
        if not base.is_dir():
            return []
        result: list[str] = []
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or _is_link_like(entry):
                continue
            if (entry / "account.bin").is_file():
                try:
                    result.append(_safe_account_id(entry.name))
                except CredentialError:
                    continue
        return result

    def clear(self, account_id: str) -> None:
        path = self._path(account_id)
        _check_no_links(path, self.root)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CredentialError("无法删除凭证文件") from exc


@dataclass(frozen=True)
class AllowedUser:
    account_id: str
    user_id: str
    label: str
    scopes: tuple[str, ...]


def _normalize_scopes(scopes: tuple[str, ...]) -> tuple[str, ...]:
    """显式传入的作用域必须全部合法。

    不能静默丢弃未知值：`--scopes chat,task,root` 若被“过滤成 chat,task”，
    调用者会误以为自己表达了某种权限。这里宁可整次拒绝。
    """
    clean: set[str] = set()
    for scope in scopes:
        if not isinstance(scope, str):
            raise CredentialError("作用域必须是字符串")
        value = scope.strip()
        if not value or value not in _ALLOWED_SCOPES:
            raise CredentialError("作用域只能是 chat、task、approve")
        clean.add(value)
    if not clean:
        raise CredentialError("至少要给一个作用域")
    return tuple(sorted(clean))


class AllowListStore:
    """本地显式配对名单。文件损坏时按空名单处理（失败关闭）。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.path = self.root / "allowlist.json"

    def _load(self) -> list[AllowedUser]:
        try:
            raw = _read_capped(
                self.path, max_bytes=_MAX_ALLOWLIST_BYTES, label="允许名单",
                root=self.root,
            )
        except FileNotFoundError:
            return []
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("微信允许名单损坏，已按空名单处理")
            return []
        if not isinstance(data, dict) or data.get("version") != 1:
            logger.warning("微信允许名单版本不支持，已按空名单处理")
            return []
        users = data.get("users")
        if not isinstance(users, list):
            return []
        result: list[AllowedUser] = []
        for item in users:
            if not isinstance(item, dict):
                continue
            account_id = item.get("account_id")
            user_id = item.get("user_id")
            label = item.get("label", "")
            scopes = item.get("scopes")
            if not isinstance(account_id, str) or not isinstance(user_id, str):
                continue
            if not account_id.strip() or not user_id.strip():
                continue
            if not isinstance(scopes, list):
                continue
            clean_scopes = tuple(
                sorted({str(s) for s in scopes if isinstance(s, str) and s in _ALLOWED_SCOPES})
            )
            if not clean_scopes:
                continue
            result.append(AllowedUser(
                account_id=account_id.strip(),
                user_id=user_id.strip(),
                label=str(label or "").strip()[:80],
                scopes=clean_scopes,
            ))
        return result

    def list(self) -> list[AllowedUser]:
        return self._load()

    def is_allowed(self, account_id: str, user_id: str, scope: str) -> bool:
        if scope not in _ALLOWED_SCOPES:
            return False
        account = str(account_id or "").strip()
        user = str(user_id or "").strip()
        if not account or not user:
            return False
        return any(
            item.account_id == account
            and item.user_id == user
            and scope in item.scopes
            for item in self._load()
        )

    def add(
        self,
        *,
        account_id: str,
        user_id: str,
        label: str = "",
        scopes: tuple[str, ...] = ("chat",),
    ) -> AllowedUser:
        account = str(account_id or "").strip()
        user = str(user_id or "").strip()
        if not account or not user:
            raise CredentialError("账号 ID 和用户 ID 都不能为空")
        clean_scopes = _normalize_scopes(scopes)
        users = [u for u in self._load() if not (u.account_id == account and u.user_id == user)]
        entry = AllowedUser(
            account_id=account,
            user_id=user,
            label=str(label or "").strip()[:80],
            scopes=clean_scopes,
        )
        users.append(entry)
        self._write(users)
        return entry

    def remove(self, *, account_id: str, user_id: str) -> bool:
        users = self._load()
        kept = [
            u for u in users
            if not (u.account_id == str(account_id).strip() and u.user_id == str(user_id).strip())
        ]
        if len(kept) == len(users):
            return False
        self._write(kept)
        return True

    def _write(self, users: list[AllowedUser]) -> None:
        data = {
            "version": 1,
            "users": [
                {
                    "account_id": u.account_id,
                    "user_id": u.user_id,
                    "label": u.label,
                    "scopes": list(u.scopes),
                }
                for u in users
            ],
        }
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write(self.path, raw, mode=0o600, root=self.root)


class ApprovalSecretStore:
    """远程授权码 HMAC 的本地密钥。用同一个 Protector 加密。"""

    def __init__(self, root: Path | str, protector: Protector) -> None:
        self.root = Path(root)
        self.path = self.root / "approval.key.bin"
        self.protector = protector

    def load_or_create(self) -> bytes:
        try:
            raw = _read_capped(
                self.path, max_bytes=_MAX_CREDENTIAL_BYTES, label="授权密钥",
                root=self.root,
            )
        except FileNotFoundError:
            raw = b""
        if raw:
            key = self.protector.unprotect(raw)
            if len(key) != 32:
                raise CredentialError("授权密钥长度不对")
            return key
        key = secrets.token_bytes(32)
        _atomic_write(
            self.path, self.protector.protect(key), mode=0o600, root=self.root
        )
        return key
