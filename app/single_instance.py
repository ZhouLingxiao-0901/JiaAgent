"""单实例锁与 PID 文件。

**为什么不用端口占用来判断**：端口冲突只在 uvicorn 真正 bind 时才报错，
用户看到的是 traceback；两个实例同时启动时，落败的那个可能已经把启动
流程走到一半。这里在进入 uvicorn 之前就挡住，写一条清楚的日志并正常退出。

Windows 用**本会话命名互斥体**（``Local\\``）。不用 ``Global\\``：创建全局
对象通常需要额外权限，而 Jia 本来就是当前用户、当前桌面会话里的程序。
互斥体随进程句柄自动释放，进程崩溃也不会留下需要手工清理的锁文件。

非 Windows 用 ``flock`` 兜底——项目只支持 Windows，这只是为了让静态检查
和测试在别的系统上也能跑，不代表正式支持。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import IO, Mapping

_ERROR_ALREADY_EXISTS = 183


def project_digest(root: Path) -> str:
    """同一项目目录的稳定短标识；不同 checkout 互不干扰。"""
    resolved = str(Path(root).resolve()).replace("\\", "/").casefold()
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


def _instance_name(root: Path) -> str:
    """同一个项目目录共用一个互斥体；不同 checkout 互不干扰。"""
    return f"Local\\JiaAgent-{project_digest(root)}"


class InstanceLock:
    """已经拿到的锁。调用方必须保持引用，进程退出前 release。"""

    def __init__(
        self,
        *,
        handle: int | None = None,
        file: IO[bytes] | None = None,
        path: Path | None = None,
    ) -> None:
        self._handle = handle
        self._file = file
        self._path = path
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True

        if self._handle is not None:
            handle, self._handle = self._handle, None
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle(ctypes.c_void_p(handle))
            except OSError:
                pass

        if self._file is not None:
            file, self._file = self._file, None
            try:
                file.close()
            finally:
                if self._path is not None:
                    try:
                        self._path.unlink()
                    except OSError:
                        pass


def _acquire_windows(root: Path) -> InstanceLock | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # BOOL is a 4-byte int on Win32; c_bool would be 1 byte.
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
    ]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool

    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, _instance_name(root))
    error = ctypes.get_last_error()
    if not handle:
        # 锁机制本身出错不能当成“已有实例”，也不能继续启动：调用方
        # （run.py）会拒绝启动。固定端口不能代替进程级互斥。
        raise OSError(error, f"无法创建单实例互斥体（错误码 {error}）")
    if error == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return InstanceLock(handle=handle)


def _acquire_posix(root: Path, lock_path: Path | None) -> InstanceLock | None:
    import fcntl
    import tempfile

    if lock_path is None:
        digest = hashlib.sha256(
            str(Path(root).resolve()).encode("utf-8")
        ).hexdigest()[:12]
        lock_path = Path(tempfile.gettempdir()) / f"jiaagent-{digest}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    file = open(lock_path, "a+b")
    try:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        file.close()
        return None
    return InstanceLock(file=file, path=lock_path)


def acquire(root: Path, *, lock_path: Path | None = None) -> InstanceLock | None:
    """拿单实例锁。

    返回 ``InstanceLock`` 表示拿到；返回 ``None`` 表示已有实例在跑。
    创建锁本身失败时抛 ``OSError``，由调用方决定是继续还是退出。
    """
    if os.name == "nt":
        return _acquire_windows(root)
    return _acquire_posix(root, lock_path)


# ----------------------------------------------------------------------
# PID 文件：只给本机的停止/状态脚本看，不写任何凭证或命令行。
# 内容是 JSON：pid、进程启动时间（用于防止 PID 复用）、监听地址/端口、
# 以及本机优雅关闭事件名。停止脚本必须核对启动时间后才会结束进程。
# ----------------------------------------------------------------------
def write_pid_file(path: Path, state: Mapping[str, object]) -> None:
    """原子写入状态文件。

    ``state["pid"]`` 必须是正整数；其余字段按调用方约定写入。这样停止
    脚本可以核对 PID 对应的启动时间和监听地址，而不是只信任一个数字。
    """
    payload = dict(state)
    raw_pid = payload.get("pid")
    if isinstance(raw_pid, bool):
        raise ValueError("PID 必须是正整数")
    try:
        pid = int(raw_pid)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("PID 必须是正整数") from exc
    if pid <= 0:
        raise ValueError("PID 必须是正整数")
    payload["pid"] = pid

    started_at = payload.get("started_at")
    if started_at is not None:
        try:
            started = float(started_at)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("started_at 必须是正数") from exc
        if not started > 0:
            raise ValueError("started_at 必须是正数")
        payload["started_at"] = started

    port = payload.get("port")
    if port is not None:
        if isinstance(port, bool):
            raise ValueError("port 必须是 1..65535")
        try:
            port_value = int(port)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("port 必须是 1..65535") from exc
        if not 1 <= port_value <= 65535:
            raise ValueError("port 必须是 1..65535")
        payload["port"] = port_value

    encoded = (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    )
    fd = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def read_pid_file(path: Path) -> dict[str, object] | None:
    """读取状态文件；损坏、缺 PID 或旧格式返回可识别的字典/None。

    旧版本只写一个整数。这里返回 ``{"pid": n, "legacy": True}``，让状态
    脚本可以显示它，但停止脚本会因缺少启动时间而拒绝结束进程。
    """
    try:
        raw = Path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw:
        return None
    if raw.isdigit():
        value = int(raw)
        return {"pid": value, "legacy": True} if value > 0 else None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw_pid = data.get("pid")
    if isinstance(raw_pid, bool) or not isinstance(raw_pid, int) or raw_pid <= 0:
        return None
    return data


def clear_pid_file(path: Path, pid: int) -> None:
    """只删自己的 PID 状态，避免误删下一次启动写下的新状态。

    旧版整数文件没有启动时间，无法证明属于当前进程，因此宁可不删。
    """
    state = read_pid_file(path)
    if state is None or state.get("legacy") or state.get("pid") != pid:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass
