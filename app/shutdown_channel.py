"""本机关闭通道：让停止脚本请求 uvicorn 优雅退出。

为什么不加 HTTP 关闭接口：
  - 不需要新的网络入口、令牌分发或额外的鉴权面；
  - Windows 命名事件只在当前用户会话（Local\\ 命名空间）可见；
  - 停止脚本已经持有同一用户的权限，能打开事件也就能结束进程，
    所以这里只解决“先请求清理”的问题，不假装它是身份认证。

事件名由项目路径哈希决定，和单实例互斥体同源。run.py 创建事件；
停止脚本打开事件并 Set()；监听线程把 ``server.should_exit`` 置为 True，
uvicorn 随后走正常的 lifespan 关闭流程（停止微信监督、窗口追踪等）。
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from pathlib import Path

from app.single_instance import project_digest

logger = logging.getLogger(__name__)

EVENT_PREFIX = "Local\\JiaAgent-Shutdown-"
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002


def event_name(root: Path) -> str:
    return EVENT_PREFIX + project_digest(root)


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    from ctypes import wintypes

    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.OpenEventW.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.OpenEventW.restype = ctypes.c_void_p
    kernel32.SetEvent.argtypes = [ctypes.c_void_p]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


class ShutdownChannel:
    """一个自动重置的命名事件；收到信号后让 uvicorn 正常退出。"""

    def __init__(self, handle: int, name: str) -> None:
        self._handle: int | None = int(handle)
        self.name = name
        self._closed = False
        self._thread: threading.Thread | None = None

    @classmethod
    def create(cls, root: Path) -> "ShutdownChannel | None":
        if os.name != "nt":
            return None
        name = event_name(root)
        kernel32 = _kernel32()
        ctypes.set_last_error(0)
        handle = kernel32.CreateEventW(None, False, False, name)
        error = ctypes.get_last_error()
        if not handle:
            logger.warning("无法创建本机关闭事件（错误码 %s）；将只能强制停止主进程", error)
            return None
        if error == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            logger.warning("本机关闭事件已存在；本次不接管它")
            return None
        return cls(int(handle), name)

    def attach(self, server) -> None:
        """启动监听线程。只写 server.should_exit，不直接调用 uvicorn 内部方法。"""
        if self._handle is None or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._wait,
            args=(server,),
            name="jia-shutdown-watch",
            daemon=True,
        )
        self._thread.start()

    def _wait(self, server) -> None:
        kernel32 = _kernel32()
        while not self._closed and self._handle is not None:
            result = int(
                kernel32.WaitForSingleObject(
                    ctypes.c_void_p(self._handle), 250
                )
            )
            if result == _WAIT_OBJECT_0:
                if not self._closed:
                    server.should_exit = True
                return
            if result not in (_WAIT_TIMEOUT,):
                logger.warning("等待本机关闭事件失败（结果码 %s）", result)
                return

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        if self._handle is not None:
            handle, self._handle = self._handle, None
            try:
                _kernel32().CloseHandle(ctypes.c_void_p(handle))
            except OSError:
                pass

    @staticmethod
    def signal(name: str) -> bool:
        """测试/诊断用：打开已存在的命名事件并 Set。生产用的停脚本在 PowerShell 里做同样的事。"""
        if os.name != "nt":
            return False
        if not isinstance(name, str) or not name.startswith(EVENT_PREFIX):
            return False
        kernel32 = _kernel32()
        handle = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(ctypes.c_void_p(handle)))
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
