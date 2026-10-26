"""视觉客户端：把一张图交给"会看图"的模型。

**为什么单独抽一层**
  - 将来接千问、GLM，只需要在 config/models.yaml 加一条 provider，
    再把 config/tools.yaml 的 vision.provider 指过去。这个文件不用改。
  - 测试可以塞一个假客户端，不联网、不花钱。

本阶段只做单模型分析：没有多模型协同，也没有自动降级。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from app.errors import JiaAgentError, VisionError
from app.llm.registry import create_adapter
from app.vision import capture

logger = logging.getLogger(__name__)


class VisionClient(ABC):
    """"看图"的统一接口。

    以后接新厂商，实现这个类即可；分析模块只认这个接口，
    不知道背后是 DeepSeek 还是千问。
    """

    provider: str = ""
    model: str = ""

    @abstractmethod
    async def analyze(self, image: Any, prompt: str) -> str:
        """把图 + 提示词发给模型，返回原始文字。"""

    async def repair(self, previous: str) -> str:
        """第一次输出不是合法 JSON 时，请模型重排一次（不再上传图片）。"""
        raise VisionError(
            "这个视觉客户端不支持重排输出", detail=type(self).__name__
        )


class AdapterVisionClient(VisionClient):
    """任何 LLMAdapter 都能当视觉客户端用。

    现有适配器（OpenAI 兼容）本来就把 messages 原样交给 SDK，
    图片用标准的 image_url 内容块即可，所以这里不需要改适配器。
    """

    def __init__(
        self,
        adapter,
        *,
        image_format: str = "jpeg",
        jpeg_quality: int = 85,
        max_tokens: int = 2000,
    ) -> None:
        self.adapter = adapter
        self.provider = adapter.provider_name
        self.model = adapter.model
        self.image_format = image_format
        self.jpeg_quality = jpeg_quality
        self.max_tokens = max_tokens

    async def analyze(self, image: Any, prompt: str) -> str:
        url = capture.image_data_url(
            image, fmt=self.image_format, quality=self.jpeg_quality
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ]
        try:
            return await self.adapter.chat(messages, max_tokens=self.max_tokens)
        except JiaAgentError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一翻成看得懂的错误
            raise VisionError(
                "调用视觉模型失败", detail=f"{type(exc).__name__}: {exc}"
            ) from exc

    async def repair(self, previous: str) -> str:
        """重排输出。**不再上传图片**：只是让它把刚才的话改成 JSON。"""
        messages = [
            {
                "role": "user",
                "content": (
                    "你刚才输出的内容不是合法的 JSON 对象。"
                    "请只输出那个 JSON 对象本身，不要有任何解释、前后缀或代码围栏。\n\n"
                    "原始输出：\n" + (previous or "")[:4000]
                ),
            }
        ]
        try:
            return await self.adapter.chat(messages, max_tokens=self.max_tokens)
        except JiaAgentError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VisionError(
                "重排视觉模型输出失败", detail=f"{type(exc).__name__}: {exc}"
            ) from exc


def build_client(cfg) -> VisionClient:
    """按配置创建视觉客户端。

    cfg.provider 为空 = 用 models.yaml 里的默认模型。
    没有 Key 时 create_adapter 会抛 MissingApiKeyError（带人话说明），
    交给工具层如实展示，不在这里吞掉。
    """
    adapter = create_adapter(cfg.provider or None)
    return AdapterVisionClient(
        adapter,
        image_format=getattr(cfg, "image_format", "jpeg"),
        jpeg_quality=getattr(cfg, "jpeg_quality", 85),
        max_tokens=getattr(cfg, "max_tokens", 2000),
    )
