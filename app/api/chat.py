"""聊天相关的 HTTP 接口。

接口层只做三件事：收参数、调用下层、把结果或错误转成 JSON。
真正的业务逻辑都在 app/agent 和 app/memory 里，这里不写。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent import pending
from app.agent.core import Agent
from app.errors import JiaAgentError
from app.llm.registry import list_providers
from app.memory import store
from app.settings import get_tools_config
from app.workflow import service as workflow_service
from app.workflow import store as workflow_store
from app.workflow.trigger import should_start_workflow

logger = logging.getLogger(__name__)
router = APIRouter()


# ----------------------------------------------------------------------
# 请求体格式（FastAPI 按这个定义自动校验前端传来的数据）
# ----------------------------------------------------------------------
class NewSessionRequest(BaseModel):
    title: str = "新对话"
    provider: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, description="用户说的话")
    session_id: str | None = Field(default=None, description="不传就自动新建会话")
    provider: str | None = Field(default=None, description="不传就用默认模型")


# ----------------------------------------------------------------------
# 会话
# ----------------------------------------------------------------------
@router.post("/sessions")
async def create_session(body: NewSessionRequest) -> dict:
    return store.create_session(title=body.title or "新对话", model=body.provider or "")


@router.get("/sessions")
async def list_sessions(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    return {"sessions": store.list_sessions(limit=limit)}


@router.get("/sessions/{session_id}/messages")
async def get_messages(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    include_tool: bool = Query(default=False, description="是否把工具执行记录也一起返回"),
) -> dict:
    return {
        "session_id": session_id,
        "messages": store.get_messages(session_id, limit=limit, include_tool=include_tool),
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    # 顺带把这个会话下的任务也清掉。任务表是工作流自己建的、故意没加外键级联
    # （换来的是测试能指向临时库），所以不主动删就会留下孤儿行。
    removed_tasks = workflow_store.delete_tasks_for_session(session_id)
    return {
        "deleted": store.delete_session(session_id),
        "tasks_deleted": removed_tasks,
    }


# ----------------------------------------------------------------------
# 模型列表
# ----------------------------------------------------------------------
@router.get("/models")
async def get_models() -> dict:
    return {"providers": list_providers()}


# ----------------------------------------------------------------------
# 聊天
# ----------------------------------------------------------------------
@router.post("/chat")
async def chat(body: ChatRequest) -> dict:
    """普通聊天：等模型说完再一次性返回。用 curl 测试最方便的就是这个。"""
    provider = body.provider
    # 先创建 Agent（这一步会检查有没有配 Key），再建会话。
    # 顺序反过来的话，配置有问题的请求会在数据库里留下一个空会话。
    agent = Agent(provider_name=provider)
    # 记录 agent.provider_name 而不是前端传来的 provider：
    # 前端可能没指定模型（跟着默认走），传过来是空的，
    # 那样会话记录里就不知道到底用了哪家模型。
    session_id = store.ensure_session(body.session_id, model=agent.provider_name)
    return await agent.reply(session_id, body.message.strip())


@router.post("/chat/stream")
async def chat_stream(body: ChatRequest) -> StreamingResponse:
    """流式聊天：一边推文字，一边推工具调用和授权请求。

    返回 SSE（Server-Sent Events）格式。帧的类型有：
        start（开始）、delta（一段文字）、need_confirm（请求授权）、
        tool_start（开始执行）、tool_result（执行结果）、
        done（结束）、error（出错）。
    """
    provider = body.provider
    # 同上：先创建 Agent（会检查有没有配 Key），再建会话
    agent = Agent(provider_name=provider)
    # 同上：存实际用到的模型名，而不是前端传来的（可能是空的）
    session_id = store.ensure_session(body.session_id, model=agent.provider_name)
    user_text = body.message.strip()
    workflow_cfg = get_tools_config().workflow

    async def event_stream():
        # 先告诉前端这次用的是哪个会话、哪个模型
        yield _sse(
            {
                "type": "start",
                "session_id": session_id,
                "model": agent.model,
                "provider": agent.provider_name,
            }
        )
        try:
            # 先看一眼这句话是不是一个多步任务。是的话就不走普通聊天，
            # 改成「拆计划 -> 等你批准 -> 后台执行」，网页收到 workflow_started
            # 就去把任务面板画出来。判断失败一律按"不是任务"处理，绝不拖垮聊天。
            if workflow_cfg.enabled and await should_start_workflow(
                user_text, agent.adapter, auto_start=workflow_cfg.auto_start
            ):
                task = await workflow_service.create_and_start(
                    goal=user_text,
                    session_id=session_id,
                    provider=agent.provider_name,
                )
                full = workflow_store.get_task(task.id, with_steps=True)
                yield _sse({
                    "type": "workflow_started",
                    "task": full.to_dict(with_steps=True),
                })
                yield _sse({"type": "done", "session_id": session_id, "model": agent.model})
                return

            async for event in agent.run_turn(session_id, user_text):
                yield _sse(event)
            yield _sse({"type": "done", "session_id": session_id, "model": agent.model})
        except JiaAgentError as exc:
            # 知道原因的错：明确告诉前端是什么问题
            logger.warning("流式聊天失败：[%s] %s", exc.code, exc.message)
            yield _sse({"type": "error", **exc.to_dict()})
        except Exception as exc:  # noqa: BLE001 - 兜底，别让连接直接断掉
            logger.exception("流式聊天出现未预期的错误")
            yield _sse(
                {
                    "type": "error",
                    "code": "internal_error",
                    "message": "服务器内部错误",
                    "detail": str(exc),
                }
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # 防止中间层缓冲，确保逐字到达
        },
    )


# ----------------------------------------------------------------------
# 待授权动作：用户在网页上点「允许 / 拒绝」
# ----------------------------------------------------------------------
@router.get("/pending")
async def list_pending(session_id: str = Query(..., description="会话 ID")) -> dict:
    """某个会话还没答复的授权请求。网页刷新后用它把卡片重新画出来。"""
    return {
        "actions": [a.to_public_dict() for a in pending.store.pending_for(session_id)]
    }


@router.post("/pending/{action_id}/approve")
async def approve_pending(action_id: str) -> dict:
    action = pending.store.answer(action_id, True)
    if action is None:
        return {"ok": False, "error": "这个授权请求不存在，或已经过期了"}
    return {"ok": True}


@router.post("/pending/{action_id}/reject")
async def reject_pending(action_id: str) -> dict:
    action = pending.store.answer(action_id, False)
    if action is None:
        return {"ok": False, "error": "这个授权请求不存在，或已经过期了"}
    return {"ok": True}


class ChooseRequest(BaseModel):
    option_id: str = Field(min_length=1, description="你选中的那个候选项编号")


@router.post("/pending/{action_id}/choose")
async def choose_pending(action_id: str, body: ChooseRequest) -> dict:
    """用户在网页上从一个候选列表里挑了一个（回答"打开哪一个"）。

    只接受这个待办自己列出来的候选编号，别的一律拒掉。
    """
    action = pending.store.choose(action_id, body.option_id)
    if action is None:
        return {"ok": False, "error": "这个选择请求不存在、已过期，或者这个选项不在候选里"}
    return {"ok": True}


def _sse(payload: dict) -> str:
    """把字典打包成 SSE 的一帧。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
