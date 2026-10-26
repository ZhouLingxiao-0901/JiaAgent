"""HTTP 传输层。

默认实现只用 Python 标准库 `urllib.request`，不新增第三方依赖。所有调用
都在线程里执行，避免阻塞 asyncio 事件循环。

测试和离线验收必须注入 `Transport`；`scripts/test_wechat.py` 不会创建
`UrllibTransport`，也不会发出任何真实网络请求。
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from app.wechat.errors import TransportError, TransportTimeout


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        ...


class UrllibTransport:
    """标准库 HTTP 客户端。

    不主动关闭系统代理：访问微信域名需要遵循本机网络设置。
    """

    def __init__(self, *, max_response_bytes: int = 4 * 1024 * 1024) -> None:
        self.max_response_bytes = int(max_response_bytes)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url=url,
            data=body,
            headers=headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(self.max_response_bytes + 1)
                if len(payload) > self.max_response_bytes:
                    raise TransportError("响应体超过上限")
                return HttpResponse(status=int(response.status), body=payload)
        except urllib.error.HTTPError as exc:
            # HTTPError 本身也是响应。只读有限正文；正文不进异常，也不写日志。
            try:
                exc.read(self.max_response_bytes + 1)
            except Exception:  # noqa: BLE001
                pass
            return HttpResponse(status=int(exc.code), body=b"")
        except (TimeoutError, socket.timeout) as exc:
            raise TransportTimeout("请求超时") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise TransportTimeout("请求超时") from exc
            raise TransportError("网络请求失败") from exc
        except OSError as exc:
            raise TransportError("网络请求失败") from exc
