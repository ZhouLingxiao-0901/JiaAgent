"""把「建任务 + 装引擎 + 起后台执行」收在一处。

`/api/tasks` 和 `/api/chat/stream` 都要干这几件事，写在两个地方迟早长歪，
所以收到这里。接口层只管收参数、返回结果。
"""

from __future__ import annotations

import logging

from app.llm.registry import create_adapter
from app.settings import get_tools_config
from app.workflow import store
from app.workflow.engine import WorkflowEngine
from app.workflow.manager import manager
from app.workflow.model import Task
from app.workflow.planner import Planner

logger = logging.getLogger(__name__)


def workflow_config():
    return get_tools_config().workflow


def build_engine(task: Task, *, provider: str | None = None) -> WorkflowEngine:
    """按配置装配一个引擎。provider 传 None 就用默认模型。

    模型没配 Key 的话，create_adapter 会抛 MissingApiKeyError——接口层不用管，
    main.py 里的全局处理器会把它转成给用户看的 JSON。
    """
    cfg = workflow_config()
    adapter = create_adapter(provider or task.provider or None)
    planner = Planner(
        adapter,
        max_steps=cfg.max_steps,
        max_replans=cfg.max_replans,
        decide_mode=cfg.decide_mode,
    )
    return WorkflowEngine(
        task.id,
        planner=planner,
        confirm_timeout=cfg.confirm_timeout_seconds,
    )


async def create_and_start(
    *, goal: str, session_id: str, provider: str | None = None
) -> Task:
    """建一个任务并让它在后台开跑。

    这一步只是"拆计划 + 等你批准"，真正执行要等你点批准。
    """
    task = store.create_task(session_id, goal, provider=provider or "")
    engine = build_engine(task, provider=provider)
    started = await manager.start(task.id, lambda: engine.run(skip_approval=False))
    if not started:
        logger.warning("任务 %s 没能启动后台执行体（可能已经在跑了）", task.id)
    return task


async def start_if_idle(task: Task, *, skip_approval: bool = True) -> bool:
    """任务没有执行体的话补一个。

    用在两个地方：
      - 服务重启后，一个停在"待执行"的任务，你点批准时补上执行体
      - 计划早就拆好了、却没有执行体的任务

    skip_approval=True 表示"用户刚刚亲手点了批准"，不用再问一遍。
    """
    if manager.is_running(task.id):
        return False
    engine = build_engine(task)
    return await manager.start(task.id, lambda: engine.run(skip_approval=skip_approval))
