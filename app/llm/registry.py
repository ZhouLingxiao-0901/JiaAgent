"""模型注册表：按名字创建对应的适配器。

以后要支持 Claude，只需要做两件事：
  1. 新建 app/llm/anthropic_adapter.py，实现 LLMAdapter 接口
  2. 在下面的 _ADAPTER_CLASSES 里加一行
其它文件一行都不用改。
"""

from __future__ import annotations

from functools import lru_cache

from app.errors import ConfigError
from app.llm.base import LLMAdapter
from app.llm.openai_compatible import OpenAICompatibleAdapter
from app.settings import get_config, get_secrets

# 适配器类型 -> 实现类。models.yaml 里的 type 字段就是查这张表。
_ADAPTER_CLASSES: dict[str, type[LLMAdapter]] = {
    "openai_compatible": OpenAICompatibleAdapter,
}


def list_providers() -> list[dict[str, object]]:
    """列出所有模型，以及各自的 Key 有没有配好。给前端和诊断脚本用。"""
    config = get_config()
    secrets = get_secrets()

    result: list[dict[str, object]] = []
    for name, provider in config.providers.items():
        result.append(
            {
                "name": name,
                "label": provider.label or name,
                "model": provider.model,
                "type": provider.type,
                "enabled": provider.enabled,
                "supported": provider.type in _ADAPTER_CLASSES,
                "key_ready": bool(secrets.key_for(provider.api_key_env)),
                "is_default": name == config.default_provider,
            }
        )
    return result


@lru_cache(maxsize=None)
def create_adapter(provider_name: str | None = None) -> LLMAdapter:
    """拿到一个模型适配器。同名模型只会创建一次，会被缓存复用。"""
    config = get_config()
    secrets = get_secrets()
    provider = config.provider(provider_name)

    adapter_class = _ADAPTER_CLASSES.get(provider.type)
    if adapter_class is None:
        raise ConfigError(
            f"暂不支持的模型类型：{provider.type}",
            detail="阶段一只实现了 openai_compatible 类型。",
        )

    adapter = adapter_class(
        provider_name=provider.name,
        model=provider.model,
        api_key=secrets.key_for(provider.api_key_env),
        base_url=provider.base_url,
    )
    return adapter
