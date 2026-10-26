"""工作流的执行循环：一步一步跑，每一步都看真实结果再决定下一步。

**安全上最要紧的一条**：这个模块**从不自己判断要不要用户确认**。
它执行工具前一定先过 `app/agent/gate.py` 的闸门——和聊天那边是同一份代码。
它唯一一次碰工具执行，是在闸门明确说 `proceed=True` 之后：

    outcome = gate.GateOutcome()
    async for ev in gate.authorize_tool(tool, args, ..., outcome=outcome): ...
    if not outcome.proceed:   # 用户拒绝 / 没选 / 超时
        ...不执行...
    else:
        await tool.execute(args, injected=outcome.injected)

要是哪天有人为了"省事"把闸门绕过、直接 `tool.execute()`，那就等于把
"高风险操作必须用户同意"整个废掉了。scripts/test_workflow_engine.py 里
有断言守着这一点。

**循环长这样**：

    拆计划 -> 等用户批准 -> ┌─> 取下一步
                            │    过闸门（用户确认）-> 执行 -> 记真实结果
                            │    问模型：继续 / 改计划 / 求助 / 收尾
                            └── 直到没有待执行的步骤
                          校验目标达成没有 -> 写总结 -> 终态

**暂停和取消是协作式的**：只在**步骤边界**（和等待确认时）生效。
一次工具执行跑到一半是打不断的——工具没有中断钩子，硬断会留下半截状态。
这是有意选的，也写进了给用户看的说明里。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable

from app.agent import gate, pending
from app.llm.registry import create_adapter
from app.memory import store as memory
from app.tools.base import Tool
from app.workflow import store
from app.workflow.manager import manager
from app.workflow.model import Step, StepStatus, TaskStatus
from app.workflow.planner import (
    ACTION_ASK_USER,
    ACTION_CONTINUE,
    ACTION_FINISH,
    ACTION_REPLAN,
    Planner,
    mechanical_summary,
)

logger = logging.getLogger(__name__)

MAX_STEP_ATTEMPTS = 3              # 一步最多试几次（含第一次）
_RETRY_BACKOFF = (0.5, 1.0)        # 重试之间的等待秒数

# 只有错误文本里出现这些词，才认为"可能是暂时的，值得再试一次"。
# **故意保守**：大部分失败是确定性的（路径不允许、文件已存在、用户拒绝），
# 重试一遍只会重复副作用。宁可少重试，也不要瞎重试。
_RETRYABLE_MARKERS = (
    "timeout", "timed out", "超时",
    "connection", "连接", "网络",
    "busy", "繁忙", "temporarily", "稍后",
)

# 出现这些词就**绝对不重试**——重试也不会变好，只会打扰用户。
_FATAL_MARKERS = (
    "不允许", "拒绝", "没有选择", "缺少必要的参数", "不认识的参数", "没有名为",
)


class _Stopped(Exception):
    """任务已经到达终态，不用再往下走了。"""


class _Cancelled(_Stopped):
    """任务被取消了。"""


class _EmergencyStopped(Exception):
    """急停生效，整个任务立刻停下。

    和 `_Stopped` 分开：急停不只是"这一步不做了"，后面的步骤也一律不执行，
    任务本身要落到失败终态。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_retryable(error: str) -> bool:
    text = (error or "").lower()
    if not text:
        return False
    if any(marker in text for marker in _FATAL_MARKERS):
        return False
    return any(marker in text for marker in _RETRYABLE_MARKERS)


def _emergency_reason(tool: Tool) -> str:
    """工具是否声明自己受急停约束；是的话返回停止原因。

    只有声明了 `emergency_block_reason()` 的工具（目前是 click_element）
    才会被检查。检查本身出错按"急停生效"处理，绝不继续执行。
    """
    getter = getattr(tool, "emergency_block_reason", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.exception("检查工具 %s 的急停状态时出错", tool.name)
        return f"无法确认急停状态（{type(exc).__name__}），已停止执行。"


class WorkflowEngine:
    """跑一个任务。一个任务一个引擎，用完就丢。"""

    def __init__(
        self,
        task_id: str,
        *,
        planner: Planner | None = None,
        adapter: Any = None,
        tool_lookup: Callable[[str], Tool | None] | None = None,
        confirm_timeout: float | None = None,
        approve_timeout: float | None = None,
        db_path: Any = None,
        publisher: Callable[[str, str], None] | None = None,
    ) -> None:
        self.task_id = task_id
        self.db_path = db_path
        # 注入点：测试传假的 planner / 假工具 / 假记录器，生产走真实实现
        self.planner = planner if planner is not None else Planner(adapter or create_adapter())
        self._tool_lookup = tool_lookup
        self._publisher = publisher
        self.confirm_timeout = confirm_timeout
        # None 表示**一直等你确认计划，不设超时**——生产就该这样。
        # 测试传一个小值，好在"没批准"这个分支上快速过掉。
        self.approve_timeout = approve_timeout
        self.session_id = ""
        # 完整工具结果只留在内存里，供模型在当前任务中继续使用。
        # 数据库写的是工具给出的安全摘要（ToolResult.as_persist_text）。
        self._full_results: dict[int, str] = {}
        self._full_errors: dict[int, str] = {}

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    def _tool(self, name: str) -> Tool | None:
        """拿工具。没注入就现去全局 registry 取（惰性，避免和 tools 层绕成环）。"""
        if self._tool_lookup is not None:
            return self._tool_lookup(name)
        from app.tools.registry import registry

        return registry.get(name)

    def _task(self):
        return store.get_task(self.task_id, db_path=self.db_path)

    def _remember(self, step_id: int, result) -> None:
        """把完整结果留在内存里给模型用。

        成功和失败互相清掉旧值：步骤重试成功后，不该还挂着上一次的错误。
        """
        if result.ok:
            self._full_results[step_id] = result.as_text()
            self._full_errors.pop(step_id, None)
        else:
            self._full_errors[step_id] = result.error or result.output or "未知错误"
            self._full_results.pop(step_id, None)

    def _steps_for_model(self, steps: list[Step]) -> list[Step]:
        """把数据库里的步骤换成"结果用内存完整版"的副本。

        只影响给模型看的文本，不改数据库、不改状态。
        库里存安全摘要，模型这边仍然拿得到完整结果。
        """
        out: list[Step] = []
        for step in steps:
            full_result = self._full_results.get(step.id)
            full_error = self._full_errors.get(step.id)
            if full_result is None and full_error is None:
                out.append(step)
            else:
                out.append(replace(
                    step,
                    result=full_result if full_result is not None else step.result,
                    error=full_error if full_error is not None else step.error,
                ))
        return out

    def _publish(self, text: str, *, role: str = "assistant") -> None:
        """往会话里写一条记录。

        步骤执行写 tool 角色，里程碑写 assistant。选 tool 是有意的：
        memory.get_messages 默认**不取** tool 角色，所以任务的执行细节
        不会污染模型的聊天上下文；而网页带 include_tool=1 时历史可见。

        这一步失败绝不该毁掉任务，所以吞掉异常只记日志。

        publisher 是**给测试用的注入点**——不然跑一次测试就得往你的真实数据库里
        写一堆消息，把会话历史搞脏。
        """
        try:
            if self._publisher is not None:
                self._publisher(role, text)
            elif self.session_id:
                memory.add_message(self.session_id, role, text)
        except Exception:  # noqa: BLE001
            logger.warning("往会话写任务进展失败", exc_info=True)

    def _succeed(self, message: str) -> None:
        store.update_task(self.task_id, status=TaskStatus.SUCCEEDED,
                          summary=message, paused=False, finished_at=_now(),
                          db_path=self.db_path)
        self._publish(f"任务完成。{message}")
        raise _Stopped()

    def _fail(self, message: str, *, summary: str = "") -> None:
        store.update_task(self.task_id, status=TaskStatus.FAILED, error=message,
                          summary=summary, paused=False, finished_at=_now(),
                          db_path=self.db_path)
        self._publish(f"任务没做成。{message}")
        raise _Stopped()

    def _write_cancelled(
        self,
        message: str,
        *,
        running_note: str = (
            "任务取消时这一步还在执行，无法确认底层操作是否已经停止。"
        ),
    ) -> None:
        """把取消落成真实终态。

        还在执行的步骤标成取消（**不宣称底层操作已经停下**）；没开始的
        步骤也标成取消，避免以后被当成可以继续的 PENDING 再捡起来跑。
        """
        task = self._task()
        if task is None or task.is_terminal:
            return
        for step in task.steps:
            if step.status.is_terminal:
                continue
            if step.status is StepStatus.RUNNING:
                note = running_note
            else:
                note = "任务已取消，这一步没有执行。"
            store.update_step(
                step.id, status=StepStatus.CANCELLED, error=note,
                finished_at=_now(), db_path=self.db_path,
            )
        store.update_task(self.task_id, status=TaskStatus.CANCELLED, paused=False,
                          error=message, finished_at=_now(), db_path=self.db_path)
        self._publish(f"任务已取消。{message}")

    def _cancel(self, message: str) -> None:
        self._write_cancelled(message)
        raise _Cancelled()

    def _check_cancelled(self) -> None:
        if manager.control_of(self.task_id).cancel_requested:
            self._cancel("你取消了这个任务")

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def run(self, *, skip_approval: bool = False) -> None:
        """跑完一个任务。从头到尾都在这里，异常不往外抛。"""
        task = self._task()
        if task is None:
            logger.error("任务 %s 不存在，没法执行", self.task_id)
            return
        self.session_id = task.session_id

        try:
            await self._plan_phase(task, skip_approval=skip_approval)

            # 计划定下来了，正式进入"执行中"。**这一句千万不能少**：
            # 少了它，任务会一直停在"待执行"，后面 _succeed / _fail 想把状态改成
            # 终态时会被状态机当成非法转移直接拒掉。表现是"任务明明跑完了，
            # 状态却还停在待执行"——不报错、不崩溃，就是不对，极难查。
            if task.status is TaskStatus.PENDING:
                store.update_task(self.task_id, started_at=_now(), db_path=self.db_path)
            store.update_task(self.task_id, status=TaskStatus.RUNNING, db_path=self.db_path)

            await self._run_loop()
            await self._finish_phase()
        except _EmergencyStopped as stop:
            logger.warning("任务 %s 因急停终止：%s", self.task_id, stop.reason)
            try:
                task = self._task()
                # 剩下没跑的步骤一律标成跳过，任务落到失败终态，不留在"执行中"。
                for step in (task.steps if task is not None else []):
                    if step.status is StepStatus.PENDING:
                        store.update_step(step.id, status=StepStatus.SKIPPED,
                                          db_path=self.db_path)
                self._fail(stop.reason)
            except _Stopped:
                pass
        except _Stopped as stop:
            logger.info("任务 %s 结束：%s", self.task_id, stop or "已到终态")
        except asyncio.CancelledError:
            # 管家等不到协作式收尾时会强制取消执行体。这里必须把数据库里
            # 的“执行中/等待确认”改成取消，否则任务会永远像还在跑，
            # 通知层也可能继续为它生成授权码。
            logger.warning("任务 %s 被执行体强制取消", self.task_id)
            try:
                if manager.control_of(self.task_id).cancel_requested:
                    self._write_cancelled(
                        "任务在执行中被取消；正在运行的操作无法保证立即停止，"
                        "可能已经产生部分效果。"
                    )
            except Exception:  # noqa: BLE001
                logger.exception("写取消状态时又出错了")
            raise
        except Exception as exc:  # noqa: BLE001 - 绝不让异常穿透，也不留假的"执行中"
            logger.exception("任务 %s 执行出错", self.task_id)
            try:
                store.update_task(self.task_id, status=TaskStatus.FAILED, paused=False,
                                  error=f"执行过程中出错了：{type(exc).__name__}: {exc}",
                                  finished_at=_now(), db_path=self.db_path)
            except Exception:  # noqa: BLE001
                logger.exception("写失败状态时又出错了")

    # ------------------------------------------------------------------
    # 一、拆计划 + 等批准
    # ------------------------------------------------------------------
    async def _plan_phase(self, task, *, skip_approval: bool) -> None:
        if not task.steps:
            plan = await self.planner.plan(task.goal)
            if plan is None:
                self._fail("我没能把这件事拆成步骤。你可以说得更具体一点，或者分几次让我做。")
            assert plan is not None

            store.add_steps(self.task_id, [s.to_dict() for s in plan.steps],
                            db_path=self.db_path)
            if plan.title:
                store.update_task(self.task_id, title=plan.title, db_path=self.db_path)

            lines = [f"我把这件事拆成了 {len(plan.steps)} 步："]
            for index, step in enumerate(plan.steps, 1):
                suffix = f"（用 {step.tool}）" if step.tool else ""
                lines.append(f"{index}. {step.goal}{suffix}")
            if plan.problems:
                lines.append("有几点我做了处理：" + "；".join(plan.problems))
            self._publish("\n".join(lines))

        if skip_approval:
            return

        # 等你批准。批准和取消都会把事件 set 起来，所以醒来后要再看一眼标志位。
        control = manager.control_of(self.task_id)
        self._publish("等你确认之后我就开始执行。")
        if self.approve_timeout is None:
            # 生产环境：不设超时，一直等你。任务停在"待执行"，
            # 你什么时候点批准，什么时候接着往下跑。
            await control.approve_evt.wait()
        else:
            try:
                await asyncio.wait_for(control.approve_evt.wait(),
                                       timeout=self.approve_timeout)
            except asyncio.TimeoutError:
                logger.info("任务 %s 在 %.1f 秒内没等到批准", self.task_id,
                            self.approve_timeout)

        if control.cancel_requested:
            self._cancel("你取消了这个任务")
        if not control.approved:
            # 没批准就退出。任务停在"待执行"，你什么时候批准，什么时候接着跑。
            logger.info("任务 %s 还没被批准，先停下", self.task_id)
            raise _Stopped()

    # ------------------------------------------------------------------
    # 二、执行循环
    # ------------------------------------------------------------------
    async def _run_loop(self) -> None:
        while True:
            await self._checkpoint()

            step = store.next_step(self.task_id, db_path=self.db_path)
            if step is None:
                return                      # 没有待执行的步骤了，交给收尾阶段

            await self._execute_step(step)
            # 最后一步执行期间收到的取消也必须生效，不能被后面的
            # “没有待执行步骤了 -> 去收尾”直接吞掉。
            await self._checkpoint()

            task = self._task()
            # 给模型看内存里的完整结果，不是数据库里的安全摘要
            steps_for_model = self._steps_for_model(task.steps)
            done = next((s for s in steps_for_model if s.id == step.id), step)

            # 后面没活干了就省一次模型调用，直接去收尾
            if not any(s.status is StepStatus.PENDING for s in task.steps):
                return

            decision = await self.planner.decide_next(task.goal, steps_for_model, done)
            await self._handle_decision(decision, task, done)

    async def _handle_decision(self, decision, task, done: Step) -> None:
        if decision.action == ACTION_CONTINUE:
            return

        if decision.action == ACTION_FINISH:
            # 模型说目标达成了。**不当场信它**——交给收尾阶段真校验一遍。
            logger.info("任务 %s：模型认为可以收尾了", self.task_id)
            for step in task.steps:
                if step.status is StepStatus.PENDING:
                    store.update_step(step.id, status=StepStatus.SKIPPED,
                                      db_path=self.db_path)
            return

        if decision.action == ACTION_REPLAN:
            if task.replan_count >= self.planner.max_replans:
                self._fail(f"计划已经改了 {task.replan_count} 次还是走不通，我先停下来。")
            store.replace_pending_steps(
                self.task_id,
                [s.to_dict() for s in decision.new_steps],
                from_index=done.step_index + 1,
                db_path=self.db_path,
            )
            store.update_task(self.task_id, replan_count=task.replan_count + 1,
                              plan_revision=task.plan_revision + 1, db_path=self.db_path)
            self._publish(f"计划改了：{decision.reason or '换个做法'}")
            return

        if decision.action == ACTION_ASK_USER:
            chosen = await self._ask_user(decision.question, decision.options)
            if chosen == "retry":
                store.update_step(done.id, status=StepStatus.PENDING, db_path=self.db_path)
                self._publish("按你说的，这一步重试。")
            elif chosen == "skip":
                store.update_step(done.id, status=StepStatus.SKIPPED, db_path=self.db_path)
                self._publish("按你说的，跳过这一步，继续后面的。")
            else:
                # 选了"取消"，或者压根没人应答（超时 / 关页面）——一律按取消处理。
                # 宁可停下来，也不要自己替用户拿主意继续往下做。
                self._cancel("这个任务卡住了，没有得到你的指示，所以我停下来。")
            return

    async def _ask_user(self, question: str, options: list[dict[str, str]]) -> str:
        """卡住的时候问用户。复用现有的"选择型待办"，不另造一套问答。"""
        action = pending.store.create_choice(
            session_id=self.session_id,
            tool_name="workflow",
            tool_label="任务需要你决定",
            question=question,
            options=options,
            summary="任务需要你决定",
        )
        store.update_task(self.task_id, status=TaskStatus.WAITING_CONFIRMATION,
                          db_path=self.db_path)
        control = manager.control_of(self.task_id)
        control.current_action_id = action.id
        try:
            chosen = await pending.store.wait_choice(action, timeout=self.confirm_timeout
                                                     or pending.DEFAULT_TIMEOUT)
        finally:
            control.current_action_id = ""
        store.update_task(self.task_id, status=TaskStatus.RUNNING, db_path=self.db_path)
        return chosen

    # ------------------------------------------------------------------
    # 三、跑一步
    # ------------------------------------------------------------------
    async def _checkpoint(self) -> None:
        """步骤边界上的检查点：暂停就在这儿停，取消就在这儿走。

        **协作式**：正在跑的那一步不会被打断，只会在下一步之前生效。
        """
        control = manager.control_of(self.task_id)
        self._check_cancelled()

        if control.pause_requested:
            store.update_task(self.task_id, paused=True, db_path=self.db_path)
            self._publish("任务已暂停，你说继续就接着跑。")
            await control.resume_evt.wait()
            self._check_cancelled()
            store.update_task(self.task_id, paused=False, db_path=self.db_path)
            self._publish("继续执行。")

    async def _execute_step(self, step: Step) -> None:
        """跑一步：过闸门 -> 执行 -> 记真实结果。失败按需重试。"""
        # 不调工具的步骤（计划里最后一步常常是"把结论一句话告诉用户"）。
        # **必须真的说出点东西**：只把它标成"完成"而什么都不说，等于这一步没做——
        # 用户什么都收不到，而且收尾校验会（正确地）判目标没达成，最后报一个
        # **假失败**：真实结果明明拿到了，状态却是失败。
        # 所以这里让模型基于真实结果，把要对用户说的那段话写出来。
        if not step.tool:
            store.mark_step_running(step.id, db_path=self.db_path)
            task = self._task()
            try:
                said = await self.planner.compose(
                    task.goal, step, self._steps_for_model(task.steps)
                )
            except Exception:  # noqa: BLE001
                logger.exception("给第 %d 步写汇报内容时出错", step.step_index + 1)
                said = ""

            if said:
                store.update_step(step.id, status=StepStatus.SUCCEEDED, result=said,
                                  db_path=self.db_path)
                # 这段是直接给用户看的话，所以用 assistant 角色（进聊天区）
                self._publish(said, role="assistant")
            else:
                # 写不出来就**如实说**，不要假装这一步完成了
                store.update_step(step.id, status=StepStatus.FAILED,
                                  error="没能写出这一步要汇报的内容",
                                  db_path=self.db_path)
                self._publish(f"{step.describe()} -> 没能写出汇报内容", role="tool")
            return

        tool = self._tool(step.tool)
        if tool is None:
            # 计划阶段已经挡过一道，这里是兜底。**绝不去执行一个不存在的工具**。
            store.update_step(step.id, status=StepStatus.FAILED,
                              error=f"没有名为 {step.tool} 的工具",
                              db_path=self.db_path)
            self._publish(f"{step.describe()} -> 失败：没有这个工具", role="tool")
            return

        for attempt in range(1, MAX_STEP_ATTEMPTS + 1):
            # 急停必须在闸门之前检查：停止之后连确认卡片都不该再出现。
            stop_reason = _emergency_reason(tool)
            if stop_reason:
                store.update_step(
                    step.id,
                    status=StepStatus.FAILED,
                    error=stop_reason,
                    db_path=self.db_path,
                )
                self._publish(
                    f"{step.describe()} -> 已停止：{stop_reason[:160]}",
                    role="tool",
                )
                # 急停不是某一步的普通失败：整条任务立刻停，后续输入一律不执行。
                raise _EmergencyStopped(stop_reason)

            await self._checkpoint()
            store.mark_step_running(step.id, db_path=self.db_path)
            store.update_task(self.task_id, current_step=step.step_index,
                              db_path=self.db_path)

            outcome = gate.GateOutcome()
            async for event in gate.authorize_tool(
                tool, step.arguments, session_id=self.session_id,
                outcome=outcome, timeout=self.confirm_timeout,
            ):
                if event.get("type") != "need_confirm":
                    continue
                # 把"正在等用户确认"这件事记下来：面板能看到，取消时也能被叫醒
                control = manager.control_of(self.task_id)
                control.current_action_id = event["action"]["id"]
                store.update_task(self.task_id, status=TaskStatus.WAITING_CONFIRMATION,
                                  db_path=self.db_path)
                store.update_step(step.id, status=StepStatus.WAITING_CONFIRMATION,
                                  db_path=self.db_path)

            manager.control_of(self.task_id).current_action_id = ""
            if manager.control_of(self.task_id).cancel_requested:
                self._cancel("你取消了这个任务")
            store.update_task(self.task_id, status=TaskStatus.RUNNING, db_path=self.db_path)

            # ---- 闸门没放行：用户拒绝 / 没选 / 超时。不执行，也不重试 ----
            if not outcome.proceed:
                store.update_step(step.id, status=StepStatus.FAILED,
                                  error=outcome.result_output or "没有得到你的允许",
                                  db_path=self.db_path)
                self._publish(f"{step.describe()} -> 没有执行：{outcome.result_output}",
                              role="tool")
                return

            # ---- 只有闸门放行了，才真的执行 ----
            result = await tool.execute(step.arguments, injected=outcome.injected)

            # 完整结果只留在内存里给模型用；数据库写工具给的安全摘要。
            self._remember(step.id, result)

            if result.ok:
                store.update_step(step.id, status=StepStatus.SUCCEEDED,
                                  result=result.as_persist_text(),
                                  db_path=self.db_path)
                self._publish(f"{outcome.summary} -> 成功", role="tool")
                return

            # 重试判断用真实错误（只在内存里）；落库和写会话用安全摘要。
            raw_error = result.error or result.output or "未知错误"
            safe_error = result.as_persist_error()
            store.update_step(step.id, status=StepStatus.FAILED, error=safe_error,
                              db_path=self.db_path)
            self._publish(f"{outcome.summary} -> 失败：{safe_error[:200]}", role="tool")

            # 有副作用且不可重复的工具（未来的点击/输入）会把自己标成
            # retry_safe=False。即使错误文本看起来像瞬时问题，也绝不再执行一次。
            if (
                attempt < MAX_STEP_ATTEMPTS
                and getattr(tool, "retry_safe", True)
                and _is_retryable(raw_error)
            ):
                wait = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
                logger.info("第 %d 步失败（可能是暂时的），%.1f 秒后重试", step.step_index + 1, wait)
                self._publish(f"这一步像是暂时性问题，我等 {wait:g} 秒再试一次。")
                await asyncio.sleep(wait)
                continue
            return

    # ------------------------------------------------------------------
    # 四、校验 + 总结
    # ------------------------------------------------------------------
    async def _finish_phase(self) -> None:
        while True:
            self._check_cancelled()
            task = self._task()
            verdict = await self.planner.verify(
                task.goal, self._steps_for_model(task.steps)
            )

            if verdict.ok:
                # verify 是一次 await；取消可能刚好在这期间到达。
                # 这里查过之后到 _succeed 之间没有 await，不会被插队。
                self._check_cancelled()
                summary = verdict.summary or mechanical_summary(task.goal, task.steps)
                self._succeed(summary)

            # 没达成。还有改计划的额度就问它要补步骤，否则如实报失败。
            if task.replan_count >= self.planner.max_replans:
                self._fail(verdict.missing or "目标没有达成。",
                           summary=verdict.summary)

            last_failed = next(
                (s for s in reversed(task.steps) if s.status is StepStatus.FAILED), None
            )
            decision = await self.planner.decide_next(task.goal, task.steps, last_failed)
            if decision.action != ACTION_REPLAN or not decision.new_steps:
                self._fail(verdict.missing or "目标没有达成。", summary=verdict.summary)

            store.add_steps(self.task_id, [s.to_dict() for s in decision.new_steps],
                            db_path=self.db_path)
            store.update_task(self.task_id, replan_count=task.replan_count + 1,
                              plan_revision=task.plan_revision + 1, db_path=self.db_path)
            self._publish(f"还差一点，我补了几步：{decision.reason or '继续补齐'}")
            await self._run_loop()
