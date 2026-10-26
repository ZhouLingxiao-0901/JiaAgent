"""微信模块的异常。

这些异常只带“能安全写日志/发给人看”的信息。HTTP 响应正文、二维码值、
Bot token、context_token 一律不放进异常消息。
"""

from __future__ import annotations


class WeChatError(Exception):
    """微信模块的基类。"""


class ProtocolError(WeChatError):
    """消息或服务端响应不符合已核实的最小协议。"""


class TransportError(WeChatError):
    """网络层失败（不是业务返回码）。"""


class TransportTimeout(TransportError):
    """请求超时。长轮询超时是正常控制流，调用方要单独处理。"""


class ApiHttpError(WeChatError):
    """服务端返回了非 2xx 状态码。只保留状态码，不保留正文。"""

    def __init__(self, status: int) -> None:
        super().__init__(f"微信接口返回 HTTP {status}")
        self.status = int(status)


class ApiBusinessError(WeChatError):
    """服务端 2xx，但业务返回码失败。"""

    def __init__(self, operation: str, ret: int) -> None:
        super().__init__(f"{operation} 返回失败码 {ret}")
        self.operation = operation
        self.ret = int(ret)


class CredentialError(WeChatError):
    """凭证无法安全保存、读取或解密。"""


class UnauthorizedSender(WeChatError):
    """发送者不在允许名单里，或者不是一对一消息。"""


class RateLimited(WeChatError):
    """同一个发送者短时间发了太多请求。"""


class RemoteApprovalError(WeChatError):
    """远程授权无效、过期、已使用或与任务不匹配。"""
