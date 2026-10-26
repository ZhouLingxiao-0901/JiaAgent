"""判断一句话要不要进工作流。

两条路：
  1. **明说** —— 文本里有「规划」「分几步」「一步步」这类词，直接进，不问模型
  2. **猜**   —— `auto_start` 开着的话，让模型看一眼这是不是一个多步任务

**宁可漏判，也不误判。** 把一句普通闲聊误当成任务，代价是：多一次模型调用、
多一次确认打扰、还可能把简单的事搞复杂。所以第二层故意设得很保守——
文本太短、像是寒暄的，直接放走，连那次模型调用都不花；任何出错
（超时、网络、解析不了）一律按「不是任务」处理。
"""

from __future__ import annotations

import logging

from app.llm.base import LLMAdapter

logger = logging.getLogger(__name__)

# 明说的标志词。命中任意一个就进工作流，不问模型。
_EXPLICIT_MARKERS = (
    "规划", "分几步", "拆成步骤", "拆成几步", "一步步", "一步一步",
    "多步", "工作流", "做个任务", "帮我做个任务",
)

# 明显的寒暄/闲聊，直接放走。
_CHITCHAT = frozenset({
    "你好", "您好", "hi", "hello", "hey", "在吗", "在么",
    "谢谢", "多谢", "拜拜", "晚安", "早安", "你是谁", "你叫什么",
})

# 短于这个长度的，多半是寒暄或简单请求，不值得走工作流。
MIN_LENGTH = 12

_JUDGE_SYSTEM = (
    "你是一个意图分类器。判断用户这句话是不是一个**需要多步才能完成**的任务。"
    "\n只回一个词：yes 或 no。不要解释。"
    "\n\n判断标准："
    "\n- 需要查/看几次、做几件事、或者「先…然后…」有先后关系的 -> yes"
    "\n- 一句话就能答完、或者只是一个动作（比如打开某个软件、问个问题） -> no"
)


async def should_start_workflow(
    text: str,
    adapter: LLMAdapter | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 8.0,
) -> bool:
    """这句话该不该进工作流。

    adapter 传 None 就只做关键词判断（测试和"不想花模型调用"的场景用）。
    """
    clean = (text or "").strip()
    if not clean:
        return False

    lowered = clean.casefold()
    if any(marker in lowered for marker in _EXPLICIT_MARKERS):
        logger.info("触发工作流：命中了明确说法")
        return True

    if not auto_start or adapter is None:
        return False

    if len(clean) < MIN_LENGTH or lowered in _CHITCHAT:
        return False

    try:
        reply = await adapter.chat([
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": clean},
        ])
    except Exception:  # noqa: BLE001 - 判断失败绝不能拖垮正常聊天
        logger.warning("判断要不要进工作流时出错，按「不进」处理", exc_info=True)
        return False

    verdict = (reply or "").strip().casefold()
    # 宽容一点：模型可能回 "yes." 或 "Yes, this is..."，只认开头
    started = verdict.startswith("yes") or verdict.startswith("是")
    if started:
        logger.info("触发工作流：模型判断这是多步任务")
    return started
