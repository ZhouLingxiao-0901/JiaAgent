"""模型适配器的统一接口。

上层代码（智能体核心、HTTP 接口）只认这里定义的方法，
完全不知道背后是 DeepSeek 还是 Claude。
这就是"支持多种模型、可以随时切换"的关键：
换一家模型 = 换一个适配器实现，上层一行都不用改。

阶段二多加了一个 stream_turn()：它除了吐文字，还能吐"我想调用某个工具"。
原来的 chat() 和 stream_chat() 一行没动，所以老的调用方不受影响。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

# 内部统一使用 OpenAI 风格的消息格式。普通聊天是这样：
#   [{"role": "user", "content": "你好"}]
# 出现工具调用时还会多出这两种：
#   {"role": "assistant", "content": "", "tool_calls": [ ... ]}
#   {"role": "tool", "tool_call_id": "call_xxx", "content": "执行结果"}
# 别家格式不同的（比如 Claude），在各自适配器内部转换。
Message = dict[str, Any]


@dataclass
class ToolCall:
    """模型要求调用一个工具。

    arguments 是**还没解析的 JSON 字符串**，而且它是从流式响应里
    一小片一小片拼出来的，所以这里原样保留，由调用方决定何时解析。
    """

    id: str
    name: str
    arguments: str

    def parsed_arguments(self) -> dict[str, Any]:
        """把参数字符串解析成字典。解析不动就返回空字典，交给上层报错。"""
        try:
            data = json.loads(self.arguments or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}


@dataclass
class TextDelta:
    """模型吐出来的一小段文字。"""

    text: str


@dataclass
class ToolCallsReady:
    """一轮回复结束时，模型要求调用的工具（可能不止一个）。"""

    calls: list[ToolCall]


# 一轮回复里可能出现的事件，上层用 isinstance 分辨
TurnEvent = TextDelta | ToolCallsReady


class LLMAdapter(ABC):
    """所有模型适配器都必须实现这个接口。"""

    def __init__(
        self,
        *,
        provider_name: str,
        model: str,
        api_key: str,
        base_url: str,
    ) -> None:
        self.provider_name = provider_name
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    async def chat(self, messages: list[Message], **options) -> str:
        """发一轮对话，返回模型的完整回复文字。不带工具，用于简单场景。"""
        raise NotImplementedError

    @abstractmethod
    def stream_chat(self, messages: list[Message], **options) -> AsyncIterator[str]:
        """发一轮对话，逐段吐出回复文字（用来做打字机效果）。

        注意这是个"异步生成器"，调用它不会立刻发请求，
        要用 `async for piece in adapter.stream_chat(...)` 消费。
        """
        raise NotImplementedError

    @abstractmethod
    def stream_turn(self, messages: list[Message], tools: list[dict[str, Any]] | None = None,
                    **options: Any) -> AsyncIterator[TurnEvent]:
        """带工具的流式一轮对话。

        产出两种事件：
          TextDelta     一小段文字，可以直接显示给用户
          ToolCallsReady 模型想调用工具，调用方执行完再把结果塞回 messages

        tools 为 None 或空列表时，就是普通聊天，只是事件包装了一层。
        """
        raise NotImplementedError
