"""自主工作流任务相关的 HTTP 接口。

进度推送用的是**轮询**而不是 SSE：后台任务不绑在某一条请求上，轮询天然就是
"刷新页面不丢"。而工具级的确认卡片走的是早就有的 `GET /api/pending?session_id=`
那一套，不用另造。

安全上要说清楚：这些接口**只能控制任务**（批准/暂停/恢复/取消/查看），
它们**不能直接执行任何工具**。真正执行工具的那条路上有确认闸门
（app/agent/gate.py），这里绕不过去、也没有绕的入口。
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.errors import JiaAgentError
from app.memory import store as memory
from app.workflow import service, store
from app.workflow.manager import manager
from app.workflow.model import TaskStatus

logger = logging.getLogger(__name__)
router = APIRouter()


class CreateTaskRequest(BaseModel):
    goal: str = Field(min_length=1, description="要做的事，用你自己的话说")
    session_id: str | None = Field(default=None, description="不传就自动新建会话")
    provider: str | None = Field(default=None, description="不传就用默认模型")


class TaskNotFound(JiaAgentError):
    """任务不存在。

    这是「你要了一个不存在的编号」，属于客户端的问题，所以该是 **404**。
    用基类的话会变成 500 internal_error——那等于把锅甩给服务器，
    而且以后看到 500 的人会去翻服务器日志，白费工夫。
    """

    code = "task_not_found"
    http_status = 404


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _require_task(task_id: str):
    task = store.get_task(task_id, with_steps=False)
    if task is None:
        raise TaskNotFound("找不到这个任务", detail="它可能已经被删掉了。")
    return task


# ----------------------------------------------------------------------
# 查看
# ----------------------------------------------------------------------
@router.get("/tasks")
async def list_tasks(
    session_id: str | None = Query(default=None, description="只看某个会话下的任务"),
    status: str | None = Query(default=None, description="只看某个状态的任务"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    tasks = store.list_tasks(session_id=session_id, status=status, limit=limit)
    return {"tasks": [t.to_dict(with_steps=False) for t in tasks]}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    """网页任务面板的**轮询主接口**：任务本身 + 全部步骤。"""
    task = _require_task(task_id)
    full = store.get_task(task_id, with_steps=True)
    return {"task": full.to_dict(with_steps=True), "running": manager.is_running(task_id)}


# ----------------------------------------------------------------------
# 新建
# ----------------------------------------------------------------------
@router.post("/tasks")
async def create_task(body: CreateTaskRequest) -> dict:
    cfg = service.workflow_config()
    if not cfg.enabled:
        raise JiaAgentError("自主工作流现在是关着的",
                            detail="要打开的话，把 config/tools.yaml 里 workflow.enabled 改成 true，然后重启服务。")

    provider = body.provider
    # 先建会话、再建任务。模型没配 Key 的话，建引擎时就会抛错，不会留下半个任务。
    session_id = memory.ensure_session(body.session_id, model=provider or "")
    task = await service.create_and_start(
        goal=body.goal.strip(), session_id=session_id, provider=provider
    )
    full = store.get_task(task.id, with_steps=True)
    return {"task": full.to_dict(with_steps=True)}


# ----------------------------------------------------------------------
# 控制
# ----------------------------------------------------------------------
@router.post("/tasks/{task_id}/approve")
async def approve_task(task_id: str) -> dict:
    """你批准了计划，开始执行。

    如果这个任务当前没有执行体（典型情况：服务重启过），这里顺手补一个，
    好让它能接着往下跑。
    """
    task = _require_task(task_id)
    if task.is_terminal:
        return {"ok": False, "error": "这个任务已经结束了，不能再批准"}

    await manager.approve(task_id)
    started = await service.start_if_idle(task, skip_approval=True)
    logger.info("任务 %s 被批准（补执行体：%s）", task_id, started)
    return {"ok": True, "started": started}


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: str) -> dict:
    task = _require_task(task_id)
    if task.is_terminal:
        return {"ok": False, "error": "这个任务已经结束了"}
    if not manager.is_running(task_id):
        return {"ok": False, "error": "这个任务现在没在执行"}
    await manager.pause(task_id)
    return {"ok": True, "note": "暂停会在当前这一步做完之后生效。"}


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str) -> dict:
    _require_task(task_id)
    if not manager.is_running(task_id):
        return {"ok": False, "error": "这个任务现在没在执行"}
    await manager.resume(task_id)
    return {"ok": True}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict:
    task = _require_task(task_id)
    if task.is_terminal:
        return {"ok": False, "error": "这个任务已经结束了"}

    if manager.is_running(task_id):
        await manager.cancel(task_id)
    else:
        # 没有执行体（比如服务重启过、或者它还停在待执行），直接标掉就行
        store.update_task(task_id, status=TaskStatus.CANCELLED, paused=False,
                          error="你取消了这个任务", finished_at=_now())
    return {"ok": True, "note": "取消会在当前这一步做完之后生效。"}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str) -> dict:
    """删任务。只允许删已经结束的，免得把正在跑的删成孤儿。"""
    task = _require_task(task_id)
    if not task.is_terminal:
        return {"ok": False, "error": "这个任务还没结束，先取消它再删"}
    if manager.is_running(task_id):
        return {"ok": False, "error": "这个任务还在跑，先取消它再删"}
    return {"deleted": store.delete_task(task_id)}
