"""项目统一的异常类型。

好处：接口层可以用一个 except 捕获所有"我们自己的"错误，
返回统一格式的 JSON 给前端，而不是把 Python 报错堆栈直接甩给用户。
"""

from __future__ import annotations


class JiaAgentError(Exception):
    """本项目所有自定义异常的基类。"""

    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class ConfigError(JiaAgentError):
    """配置文件找不到、写错了、或者缺东西。"""

    code = "config_error"
    http_status = 500


class LLMError(JiaAgentError):
    """调用大模型失败（网络断了、对方返回错误等）。"""

    code = "llm_error"
    http_status = 502


class MissingApiKeyError(LLMError):
    """没有配置对应模型的 API Key。这是最常见的新手问题，单独区分出来。"""

    code = "missing_api_key"
    http_status = 400


class VisionError(JiaAgentError):
    """屏幕感知不可用：截图失败、系统不支持、或者功能被关掉了。

    单独一个错误类型，是因为它几乎都是"环境问题"（DPI、多屏、被遮挡、
    安全桌面），而不是代码 bug。接口层要把它原样告诉用户。
    """

    code = "vision_unavailable"
    http_status = 503


class StorageError(JiaAgentError):
    """数据库读写失败。"""

    code = "storage_error"
    http_status = 500
