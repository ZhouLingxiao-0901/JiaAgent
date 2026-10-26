"""后台任务的管家：谁在跑、怎么停下来、怎么叫醒。

工作流的执行**不绑在一次 HTTP 请求上**。用户点"批准"以后，引擎跑在一个
`asyncio.Task` 里，请求立刻返回；网页靠轮询看进度。这样刷新页面不会丢任务，
任务跑着的时候用户还能继续聊天。

管家负责三件事：
  1. 起任务：同一时刻**同一个任务只能有一个执行体**（重复点批准不会跑两遍）
  2. 传信号：暂停 / 恢复 / 取消 / 批准。这些只是**标志位 + 事件**，
     真正"什么时候生效"由引擎在步骤边界自己检查——协作式，不是抢占式
  3. 启动对账：服务重启后，内存里的执行体全没了，数据库里残留的
     "执行中/等待确认" 任务要标成失败（见 store.mark_interrupted_as_failed）

**为什么取消要先唤醒事件**：引擎可能正卡在 `await` 上（等用户点确认、或者
停在暂停点）。只设标志位它是收不到的，必须把事件 set 一下把它叫醒，它醒来
才会看到标志位。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.agent import pending
from app.workflow import store

logger = logging.getLogger(__name__)


@dataclass
class Control:
    """一个任务的运行时控制块。只活在内存里。"""

    cancel_requested: bool = False
    pause_requested: bool = False
    approved: bool = False

    # resume_evt：set = 正在跑，clear = 已暂停
    resume_evt: asyncio.Event = field(default_factory=asyncio.Event)
    # approve_evt：计划被批准时 set
    approve_evt: asyncio.Event = field(default_factory=asyncio.Event)

    # 当前正在等的那个待办编号。取消时用它把 wait() 叫醒。
    current_action_id: str = ""


def _new_control() -> Control:
    control = Control()
    control.resume_evt.set()      # 默认不是暂停状态
    return control


class TaskManager:
    """进程级的后台任务登记处。全项目一个就够。"""

    def __init__(self) -> None:
        self._jobs: dict[str, asyncio.Task] = {}
        self._controls: dict[str, Control] = {}
        self._guard = asyncio.Lock()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def control_of(self, task_id: str) -> Control:
        """拿到（或就地创建）一个任务的控制块。引擎随时都能要到，不用判空。"""
        control = self._controls.get(task_id)
        if control is None:
            control = _new_control()
            self._controls[task_id] = control
        return control

    def is_running(self, task_id: str) -> bool:
        job = self._jobs.get(task_id)
        return job is not None and not job.done()

    def running_ids(self) -> list[str]:
        return [tid for tid, job in self._jobs.items() if not job.done()]

    # ------------------------------------------------------------------
    # 起任务
    # ------------------------------------------------------------------
    async def start(
        self,
        task_id: str,
        runner: Callable[[], Awaitable[None]],
    ) -> bool:
        """把一个任务交给后台跑。已经在跑就返回 False（防重复执行）。

        runner 是一个无参协程函数。引擎的构造在调用方做，管家不管它怎么建。
        """
        async with self._guard:
            if self.is_running(task_id):
                logger.info("任务 %s 已经在跑了，忽略这次的启动请求", task_id)
                return False
            # 保留已有的控制块（用户可能在启动前就点了暂停/批准）
            self._controls.setdefault(task_id, _new_control())
            job = asyncio.create_task(self._wrap(task_id, runner))
            self._jobs[task_id] = job
            job.add_done_callback(lambda _job, tid=task_id: self._forget(tid))
            logger.info("任务 %s 开始后台执行", task_id)
            return True

    async def _wrap(self, task_id: str, runner: Callable[[], Awaitable[None]]) -> None:
        """兜底：引擎自己应该管好自己的状态机，这里只是最后一道网，
        保证不会有"协程崩了、数据库里还停在执行中"这种假象。"""
        try:
            await runner()
        except asyncio.CancelledError:
            logger.warning("任务 %s 的执行体被强制取消", task_id)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("任务 %s 执行体崩了", task_id)
            try:
                store.update_task(
                    task_id,
                    status="failed",
                    paused=False,
                    error=f"执行过程中出错了：{type(exc).__name__}: {exc}",
                )
            except Exception:  # noqa: BLE001 - 兜底里再出错就只记日志
                logger.exception("给任务 %s 写失败状态时又出错了", task_id)

    def _forget(self, task_id: str) -> None:
        self._jobs.pop(task_id, None)
        self._controls.pop(task_id, None)

    # ------------------------------------------------------------------
    # 控制信号
    # ------------------------------------------------------------------
    async def approve(self, task_id: str) -> bool:
        """用户批准了计划。"""
        control = self.control_of(task_id)
        control.approved = True
        control.approve_evt.set()
        logger.info("任务 %s 的计划被批准", task_id)
        return True

    async def pause(self, task_id: str) -> bool:
        """请求暂停。**在下一个步骤边界生效**，不会打断正在跑的步骤。"""
        control = self.control_of(task_id)
        control.pause_requested = True
        control.resume_evt.clear()
        logger.info("任务 %s 收到暂停请求", task_id)
        return True

    async def resume(self, task_id: str) -> bool:
        control = self.control_of(task_id)
        control.pause_requested = False
        control.resume_evt.set()
        logger.info("任务 %s 恢复执行", task_id)
        return True

    async def cancel(self, task_id: str) -> bool:
        """取消任务。会把可能正在等待的引擎叫醒。"""
        control = self.control_of(task_id)
        control.cancel_requested = True

        # 把三种可能的等待都叫醒——只设标志位的话，卡在 await 上的引擎收不到
        control.resume_evt.set()      # 唤醒"暂停中"的
        control.approve_evt.set()     # 唤醒"等批准"的

        if control.current_action_id:
            # 唤醒"等确认"的。这里当作"没批准"，wait() 会正常返回 False/空串，
            # 闸门就会走"用户拒绝了"那条路——正是我们想要的。
            pending.store.answer(control.current_action_id, False)

        job = self._jobs.get(task_id)
        if job is not None and not job.done():
            # 给引擎一点时间自己收尾（写状态、写总结）。它要是赖着不走，
            # 再强制掐掉——但正常路径下应该是它先自己结束。
            try:
                await asyncio.wait_for(asyncio.shield(job), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.warning("任务 %s 没能自己停下来，强制取消", task_id)
                job.cancel()
                # 等它把终态写进数据库再返回。否则 /cancel 的回复和
                # 授权码撤销可能先完成，通知层仍会看到“执行中”的任务。
                try:
                    await asyncio.wait_for(
                        asyncio.shield(job), timeout=1.0
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            except Exception:  # noqa: BLE001
                logger.exception("等任务 %s 收尾时出错", task_id)

        logger.info("任务 %s 已取消", task_id)
        return True

    # ------------------------------------------------------------------
    # 启动对账
    # ------------------------------------------------------------------
    def reconcile_on_startup(self) -> int:
        """服务启动时调一次。

        内存里的执行体随进程一起没了，但数据库里还留着 "执行中/等待确认" 的行。
        **绝不自动接着跑**——那些步骤可能有非幂等的副作用（开应用、写文件），
        重跑一遍可能出事。所以老实标成失败，让用户自己决定要不要重新发起。

        注意：只标 running / waiting_confirmation。**"待执行"（等你批计划）的不动**
        ——那种任务一步都还没跑，批准之后接着跑是安全的。
        """
        self._jobs.clear()
        self._controls.clear()

        count = store.mark_interrupted_as_failed()
        if count:
            logger.warning("启动对账：%d 个任务因服务重启被标记为失败", count)
        return count


# 全局唯一的后台任务管家
manager = TaskManager()
