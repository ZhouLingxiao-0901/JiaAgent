"""OpenAI 兼容协议的适配器。

为什么一个类能支持四家模型？
因为 DeepSeek、智谱 GLM、通义千问、OpenAI 都提供"OpenAI 兼容接口"，
即请求地址和请求格式都跟 OpenAI 一样，区别只有 base_url 和 model 名字。

所以 models.yaml 里这四家都写成 type: openai_compatible，
共用这一个文件，不需要写四份代码。工具调用也是同一套协议。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
)

from app.errors import LLMError, MissingApiKeyError
from app.llm.base import LLMAdapter, Message, TextDelta, ToolCall, ToolCallsReady, TurnEvent

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3          # 最多尝试几次
REQUEST_TIMEOUT = 60.0    # 单次请求最多等多少秒

# 这些错误是"临时的"，值得重试
_RETRYABLE = (APIConnectionError, APITimeoutError)


class OpenAICompatibleAdapter(LLMAdapter):
    """面向所有 OpenAI 兼容接口的适配器。"""

    def __init__(self, *, provider_name: str, model: str, api_key: str, base_url: str) -> None:
        super().__init__(
            provider_name=provider_name, model=model, api_key=api_key, base_url=base_url
        )
        if not api_key:
            raise MissingApiKeyError(
                f"没有找到 {provider_name} 的 API Key",
                detail="请打开项目根目录的 .env 文件，填写对应的 JIA_..._API_KEY，然后重启程序。",
            )
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=REQUEST_TIMEOUT)

    # ------------------------------------------------------------------
    # 错误转换：把 SDK 的异常翻译成我们自己的 LLMError
    # ------------------------------------------------------------------
    @staticmethod
    def _translate(exc: Exception) -> LLMError:
        if isinstance(exc, AuthenticationError):
            return LLMError(
                "API Key 无效或已过期",
                detail=f"请检查 .env 里的 Key 是否复制完整。原始错误：{exc}",
            )
        if isinstance(exc, APIStatusError):
            return LLMError(
                f"模型接口返回错误（HTTP {exc.status_code}）", detail=str(exc)
            )
        if isinstance(exc, _RETRYABLE):
            return LLMError("连接模型服务失败，请检查网络", detail=str(exc))
        return LLMError("调用模型失败", detail=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # 非流式：一次拿到完整回复
    # ------------------------------------------------------------------
    async def chat(self, messages: list[Message], **options) -> str:
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.chat.completions.create(
                    model=self.model, messages=messages, **options
                )
                text = response.choices[0].message.content or ""
                logger.info("模型 %s 回复成功（%d 字）", self.model, len(text))
                return text

            except AuthenticationError:
                raise
            except _RETRYABLE as exc:
                last_error = exc
                if attempt < MAX_ATTEMPTS:
                    wait = 2 ** (attempt - 1)          # 1 秒、2 秒
                    logger.warning("第 %d 次调用失败，%d 秒后重试：%s", attempt, wait, exc)
                    await asyncio.sleep(wait)
            except APIStatusError as exc:
                last_error = exc
                if exc.status_code >= 500 and attempt < MAX_ATTEMPTS:
                    wait = 2 ** (attempt - 1)
                    logger.warning("对方服务器返回 %s，%d 秒后重试", exc.status_code, wait)
                    await asyncio.sleep(wait)
                    continue
                raise self._translate(exc) from exc

        raise LLMError("调用模型失败，已重试多次", detail=str(last_error))

    # ------------------------------------------------------------------
    # 流式：逐段吐出文字
    # ------------------------------------------------------------------
    async def stream_chat(self, messages: list[Message], **options) -> AsyncIterator[str]:
        try:
            stream = await self._client.chat.completions.create(
                model=self.model, messages=messages, stream=True, **options
            )
        except Exception as exc:  # noqa: BLE001 - 统一翻译成 LLMError
            raise self._translate(exc) from exc

        async for chunk in stream:
            if not chunk.choices:
                continue
            piece = chunk.choices[0].delta.content
            if piece:
                yield piece

    # ------------------------------------------------------------------
    # 带工具的流式一轮对话
    # ------------------------------------------------------------------
    async def stream_turn(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        **options: Any,
    ) -> AsyncIterator[TurnEvent]:
        """一轮回复。文字边收边吐，工具调用等收完再一次性给出。

        工具调用在流里是**碎着来的**，实测长这样：
            第 9 个分片: index=0 id='call_xxx' name='open_application' args=''
            第 10 个分片: index=0 args='{'
            第 11 个分片: index=0 args='"'
            ... 一直拼到 '}' 为止
        所以必须按 index 归拢，把 id、name 留下，arguments 一路字符串拼接。
        """
        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            **options,
        }
        if tools:
            request["tools"] = tools

        try:
            stream = await self._client.chat.completions.create(**request)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

        # index -> {id, name, arguments}
        buffers: dict[int, dict[str, str]] = {}
        text_length = 0

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.content:
                text_length += len(delta.content)
                yield TextDelta(delta.content)

            for piece in (delta.tool_calls or []):
                buf = buffers.setdefault(piece.index, {"id": "", "name": "", "arguments": ""})
                if piece.id:
                    buf["id"] = piece.id
                if piece.function is not None:
                    if piece.function.name:
                        # 名字是一次性给全的，后面对同一 index 不会再给
                        buf["name"] = piece.function.name
                    if piece.function.arguments:
                        buf["arguments"] += piece.function.arguments

        if buffers:
            calls = [
                ToolCall(
                    id=buffers[index]["id"] or f"call_{index}",
                    name=buffers[index]["name"],
                    arguments=buffers[index]["arguments"] or "{}",
                )
                for index in sorted(buffers)
            ]
            logger.info(
                "模型 %s 要求调用 %d 个工具：%s",
                self.model, len(calls), "、".join(c.name for c in calls),
            )
            yield ToolCallsReady(calls)
        else:
            logger.info("模型 %s 回复完成（%d 字），没有调用工具", self.model, text_length)
