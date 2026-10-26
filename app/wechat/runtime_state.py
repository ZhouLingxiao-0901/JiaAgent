"""微信运行时的本机状态文件。

**为什么需要它**：`started` 只活在进程内存里，CLI 是另一个进程，无法据此
判断监听是否还活着。这里用一个不含任何凭证的小 JSON 文件记录监督任务的
PID、监听是否存活、重启次数和最近一次错误，供 `wechat_cli.py status` 和
`logout` 使用。

**失败关闭**：文件存在但内容损坏、PID 非法时，一律当作“可能仍在运行”，
宁可拒绝 logout，也不要在监听还活着时删掉凭证。

**不写什么**：token、二维码、context_token、消息正文、用户原始 ID 都不写。
账号 ID 是本地配置的一部分，已在 status/pair 命令里显示。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

STATUS_FILENAME = "runtime-status.json"
_STATUS_VERSION = 1
_MAX_STATUS_BYTES = 16 * 1024
_VALID_STATES = frozenset({
    "starting", "running", "reconnecting", "paused", "stopped",
})


def _path(state_dir: Path | str) -> Path:
    return Path(state_dir) / STATUS_FILENAME


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
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
    except OSError:
        # 状态文件写失败不能拖垮监听本身；CLI 最多显示“状态未知”。
        pass


def write_runtime_status(
    state_dir: Path | str,
    *,
    account_id: str,
    state: str,
    monitor_alive: bool,
    restart_count: int = 0,
    started_at: float | None = None,
    last_error: str = "",
    pid: int | None = None,
) -> None:
    if state not in _VALID_STATES:
        state = "reconnecting"
    now = time.time()
    payload = {
        "version": _STATUS_VERSION,
        "pid": int(os.getpid() if pid is None else pid),
        "account_id": str(account_id or ""),
        "state": state,
        "monitor_alive": bool(monitor_alive),
        "restart_count": max(0, int(restart_count)),
        "started_at": float(now if started_at is None else started_at),
        "updated_at": now,
        "last_error": str(last_error or "")[:300],
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _atomic_write(_path(state_dir), raw)


def read_runtime_status(state_dir: Path | str) -> dict[str, Any] | None:
    """读取状态。不存在返回 None；存在但损坏返回“未知/活跃”标记。"""
    path = _path(state_dir)
    try:
        stat = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return {"state": "unknown", "monitor_alive": True, "pid": 0}
    if stat.st_size > _MAX_STATUS_BYTES:
        return {"state": "unknown", "monitor_alive": True, "pid": 0}
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_STATUS_BYTES + 1)
    except OSError:
        return {"state": "unknown", "monitor_alive": True, "pid": 0}
    if len(raw) > _MAX_STATUS_BYTES:
        return {"state": "unknown", "monitor_alive": True, "pid": 0}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"state": "unknown", "monitor_alive": True, "pid": 0}
    if not isinstance(data, dict) or data.get("version") != _STATUS_VERSION:
        return {"state": "unknown", "monitor_alive": True, "pid": 0}
    return data


def clear_runtime_status(state_dir: Path | str) -> None:
    try:
        _path(state_dir).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True
    try:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 无法判断时按“仍活着”处理
        return True


def runtime_is_active(state_dir: Path | str) -> bool:
    """监听是否可能仍在运行。损坏/未知状态按活跃处理（失败关闭）。"""
    status = read_runtime_status(state_dir)
    if status is None:
        return False
    if status.get("state") in ("stopped", "unknown"):
        return status.get("state") == "unknown"
    pid = status.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    return _pid_alive(pid)


def runtime_status_text(state_dir: Path | str) -> str:
    status = read_runtime_status(state_dir)
    if status is None:
        return "监听：未运行（没有运行状态文件）"
    if status.get("state") == "unknown":
        return "监听：状态未知（状态文件损坏；logout 会拒绝执行）"
    if status.get("state") == "stopped":
        return "监听：已停止"
    active = runtime_is_active(state_dir)
    if not active:
        return "监听：未运行（状态文件残留，进程已退出）"
    label = {
        "starting": "正在启动",
        "running": "运行中",
        "reconnecting": "正在重连",
        "paused": "因平台限制暂停中",
        "stopped": "已停止",
    }.get(str(status.get("state", "")), "状态未知")
    alive = "长轮询存活" if status.get("monitor_alive") else "长轮询已退出，等待监督任务恢复"
    restart_count = status.get("restart_count", 0)
    return f"监听：{label}（{alive}；重启次数 {restart_count}）"
