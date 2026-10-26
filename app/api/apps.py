"""应用索引相关的 HTTP 接口（阶段一：只读 + 手动刷新）。

三条接口：
  POST /api/apps/refresh   手动刷新索引，返回统计信息
  GET  /api/apps          列出索引里的应用，可按名字过滤
  GET  /api/apps/search   按名字搜索，返回 unique/ambiguous/none 三态

这些都是"读系统 + 写自己的数据库"，没有任何启动、删改系统文件的副作用。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.apps import index, matcher
from app.apps.model import normalize

router = APIRouter()


@router.post("/apps/refresh")
def refresh_apps() -> dict:
    """手动刷新应用索引。首次调用会顺带把索引建出来。"""
    return index.refresh()


@router.get("/apps")
def list_apps(
    q: str = Query(default="", description="可选，按名字过滤"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    index.ensure_ready()   # 索引是空的就顺手建一次，好让第一次打开就能看到
    records = index.load_all()
    if q:
        needle = normalize(q)
        records = [
            r for r in records
            if needle in r.name_norm or needle in r.identity_norm
        ]
    records.sort(key=lambda r: (r.name.casefold(), r.source.value))
    page = records[:limit]
    return {
        "total": len(records),
        "count": len(page),
        "apps": [r.to_dict() for r in page],
    }


@router.get("/apps/search")
def search_apps(
    q: str = Query(..., min_length=1, description="要搜索的应用名"),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    index.ensure_ready()
    records = index.load_all()
    result = matcher.match(q, records, limit=limit)
    return {
        "query": q,
        "status": result.status,
        "candidates": [c.to_dict() for c in result.candidates],
    }
