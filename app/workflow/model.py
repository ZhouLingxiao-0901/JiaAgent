"""工作流的数据结构：任务、步骤，以及它们的状态机。

这个模块只定义「数据长什么样」和「哪些状态转移是合法的」——不读文件、不碰
数据库、不调用模型。纯逻辑，可以放心大胆地测。

**关于状态**：用户要求的任务状态是六个（待执行、执行中、等待确认、成功、失败、
已取消）。「暂停」**没有**做成第七个状态，而是一个独立的布尔字段（见 Task.paused）。
理由：暂停是一个可以随时恢复的正交控制位；硬塞进 status 会和「等待确认」打架
——两者都是「停着」，但一个在等你点按钮、一个在等你点恢复，语义完全不同。
面板上显示「已暂停」时读的是 status + paused 两个字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ----------------------------------------------------------------------
# 任务状态
# ----------------------------------------------------------------------


class TaskStatus(str, Enum):
    """一个任务的六种状态。"""

    PENDING = "pending"                            # 待执行（还没开始，或刚拆完计划等你确认）
    RUNNING = "running"                            # 执行中
    WAITING_CONFIRMATION = "waiting_confirmation"  # 等待确认（某个步骤要你点头）
    SUCCEEDED = "succeeded"                        # 成功
    FAILED = "failed"                              # 失败
    CANCELLED = "cancelled"                        # 已取消

    @property
    def label(self) -> str:
        return _TASK_LABELS[self]

    @property
    def is_terminal(self) -> bool:
        """终态：到这儿就不会再动了。"""
        return self in TERMINAL_TASK_STATUSES


_TASK_LABELS: dict[TaskStatus, str] = {
    TaskStatus.PENDING: "待执行",
    TaskStatus.RUNNING: "执行中",
    TaskStatus.WAITING_CONFIRMATION: "等待确认",
    TaskStatus.SUCCEEDED: "成功",
    TaskStatus.FAILED: "失败",
    TaskStatus.CANCELLED: "已取消",
}

TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

# 合法的状态转移。没列出来的都算非法——让"偷偷从成功改回执行中"这种事做不到。
_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    # "待执行"也能直接走到"失败"：计划还没拆出来就失败了（比如模型给不出可用的
    # 步骤）。这时候任务一步都没跑，但必须能如实标成失败——不让它失败的话，
    # 任务就会永远卡在"待执行"，那才是真的骗人。
    TaskStatus.PENDING: frozenset({
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.WAITING_CONFIRMATION,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.WAITING_CONFIRMATION: frozenset({
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


# ----------------------------------------------------------------------
# 步骤状态
# ----------------------------------------------------------------------


class StepStatus(str, Enum):
    """一个步骤的状态。比任务多一个"已跳过"。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"      # 用户选择跳过，或重规划时被替掉
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return _STEP_LABELS[self]

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STEP_STATUSES


_STEP_LABELS: dict[StepStatus, str] = {
    StepStatus.PENDING: "待执行",
    StepStatus.RUNNING: "执行中",
    StepStatus.WAITING_CONFIRMATION: "等待确认",
    StepStatus.SUCCEEDED: "成功",
    StepStatus.FAILED: "失败",
    StepStatus.SKIPPED: "已跳过",
    StepStatus.CANCELLED: "已取消",
}

TERMINAL_STEP_STATUSES = frozenset({
    StepStatus.SUCCEEDED,
    StepStatus.FAILED,
    StepStatus.SKIPPED,
    StepStatus.CANCELLED,
})

_STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset({
        StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.CANCELLED,
    }),
    StepStatus.RUNNING: frozenset({
        StepStatus.WAITING_CONFIRMATION, StepStatus.SUCCEEDED,
        StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.CANCELLED,
    }),
    StepStatus.WAITING_CONFIRMATION: frozenset({
        StepStatus.RUNNING, StepStatus.FAILED,
        StepStatus.SKIPPED, StepStatus.CANCELLED,
    }),
    StepStatus.SUCCEEDED: frozenset(),
    StepStatus.FAILED: frozenset(),
    StepStatus.SKIPPED: frozenset(),
    StepStatus.CANCELLED: frozenset(),
}


def can_transition(current: TaskStatus, nxt: TaskStatus) -> bool:
    """任务能不能从 current 走到 nxt。"""
    return nxt in _TASK_TRANSITIONS.get(current, frozenset())


def can_step_transition(current: StepStatus, nxt: StepStatus) -> bool:
    """步骤能不能从 current 走到 nxt。"""
    return nxt in _STEP_TRANSITIONS.get(current, frozenset())


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------


@dataclass
class Step:
    """一个执行步骤。

    tool 为空字符串表示「这一步不调工具」，只是让模型说明/汇报一下
    （比如最后一步"把结论告诉用户"）。

    arguments 是 dict，存进数据库时序列化成 JSON 文本——数据库那边不用关心
    这个细节，由 store.py 负责转换。
    """

    step_index: int
    goal: str
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    expect: str = ""                                  # 预期结果，供决策时参考
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    result: str = ""                                  # 真实输出（会被截断）
    error: str = ""                                   # 真实错误
    started_at: str = ""
    finished_at: str = ""
    id: int = 0                                       # 数据库主键，新建时还没有

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "step_index": self.step_index,
            "goal": self.goal,
            "tool": self.tool,
            "arguments": self.arguments,
            "expect": self.expect,
            "status": self.status.value,
            "status_label": self.status.label,
            "attempts": self.attempts,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def describe(self) -> str:
        """一句话说清这一步要干什么，写进会话记录用。"""
        head = f"第 {self.step_index + 1} 步 {self.goal}"
        return f"{head}（{self.tool}）" if self.tool else head


@dataclass
class Task:
    """一个任务：一个目标 + 一串步骤 + 当前状态。"""

    id: str
    session_id: str                                   # 挂在哪个会话下（软引用）
    goal: str                                         # 用户原话
    title: str = ""                                   # 面板上显示的短标题
    status: TaskStatus = TaskStatus.PENDING
    paused: bool = False                              # 独立的暂停位，不是状态
    provider: str = ""                                # 用哪个模型跑的
    current_step: int = 0                             # 进行到第几步（0 基）
    plan_revision: int = 0                            # 计划改过几次
    replan_count: int = 0                             # 重规划次数（用来卡上限）
    error: str = ""
    summary: str = ""                                 # 结束时的总结
    created_at: str = ""
    updated_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    steps: list[Step] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def to_dict(self, *, with_steps: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "session_id": self.session_id,
            "goal": self.goal,
            "title": self.title,
            "status": self.status.value,
            "status_label": self.status.label,
            "paused": self.paused,
            "provider": self.provider,
            "current_step": self.current_step,
            "plan_revision": self.plan_revision,
            "replan_count": self.replan_count,
            "error": self.error,
            "summary": self.summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "step_count": len(self.steps),
        }
        if with_steps:
            data["steps"] = [s.to_dict() for s in self.steps]
        return data
