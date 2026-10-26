"""应用发现的两个只读工具：列出索引、按名字搜索。

阶段一里它们**只读、不启动任何东西**，让模型（和你）先看到
"佳到底认识了哪些应用、认成了什么名字"。真正打开应用还走
open_application，那个工具要到阶段二才接上索引。

这两个工具 requires_confirmation = False：纯读索引，没有副作用。
"""

from __future__ import annotations

from typing import Any

from app.apps import index, matcher
from app.apps.model import normalize
from app.tools.base import Tool, ToolResult


def _as_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


class ListIndexedAppsTool(Tool):
    name = "list_indexed_apps"
    label = "列出已索引的应用"
    description = "列出佳在这台电脑上发现并索引的应用名称，可按关键词过滤。"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "可选，只列出名字里包含这个关键词的应用",
            },
            "limit": {
                "type": "integer",
                "description": "可选，最多列多少条，默认 50",
            },
        },
        "required": [],
    }
    requires_confirmation = False

    async def run(self, query: str = "", limit: int = 50) -> ToolResult:
        index.ensure_ready()   # 索引是空的就顺手建一次，免得误报"没有应用"
        records = index.load_all()
        needle = normalize(query) if query else ""
        if needle:
            records = [
                r for r in records
                if needle in r.name_norm or needle in r.identity_norm
            ]
        records.sort(key=lambda r: (r.name.casefold(), r.source.value))

        limit = _as_int(limit, 50, 1, 200)
        shown = records[:limit]
        total = len(records)
        if not shown:
            return ToolResult(
                ok=True,
                output="索引里还没有应用" + ("，也没有名字里带这个关键词的" if needle else "") + "。",
            )
        names = "、".join(r.name for r in shown)
        tail = f"（共 {total} 个，这里列前 {len(shown)} 个）" if total > len(shown) else f"（共 {total} 个）"
        return ToolResult(ok=True, output=f"已索引的应用{tail}：{names}")


class SearchAppsTool(Tool):
    name = "search_apps"
    label = "搜索应用"
    description = "按名字（中文、英文、简称、模糊）在索引里搜索应用，返回最像的几个候选。"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要搜索的应用名字，例如 记事本、notepad、夸克、vscode",
            },
        },
        "required": ["query"],
    }
    requires_confirmation = False

    async def run(self, query: str) -> ToolResult:
        index.ensure_ready()
        records = index.load_all()
        candidates = matcher.search(query, records, limit=10)
        if not candidates:
            return ToolResult(
                ok=True,
                output=f"在索引里没有找到和「{query}」对得上的应用。",
            )
        lines = [f"「{query}」找到这些候选："]
        for c in candidates:
            lines.append(f"  - {c.record.name}（{c.record.source.label}，匹配度 {c.score:.0%}）")
        return ToolResult(ok=True, output="\n".join(lines))
