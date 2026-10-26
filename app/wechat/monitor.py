"""长轮询监听。

**游标只在整页处理完之后持久化**。如果先存游标再处理，进程崩一下消息就
永远丢了；现在即使崩在保存之前，重连会再拿到这一页，由 inbound 表的原子
claim 挡住重复执行。这是“宁可重复投递，也不漏消息，但不重复执行”的组合。

**超时不是故障**：getUpdates 的长轮询超时是正常控制流，直接继续。
网络错误连续 3 次才退避 30 秒，否则 2 秒。服务端返回 -14 时暂停账号
1 小时并退出循环——第一版不自动绕过平台的会话限制。

**这里不解析、不鉴权、不执行**：原始消息交给 service.handle_update，
所有安全判断在那一层。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.wechat.errors import (
    ApiHttpError,
    ProtocolError,
    TransportError,
    TransportTimeout,
)
from app.wechat.redact import safe_exception
from app.wechat.service import WeChatService
from app.wechat.store import WeChatStore

logger = logging.getLogger(__name__)

ACCOUNT_PAUSE_SECONDS = 3600.0
PAUSE_ERROR_CODE = -14


@dataclass
class MonitorConfig:
    min_long_poll_seconds: float = 5.0
    max_long_poll_seconds: float = 120.0
    default_long_poll_seconds: float = 35.0
    error_backoff_seconds: float = 2.0
    long_error_backoff_seconds: float = 30.0
    long_error_threshold: int = 3
    empty_page_sleep_seconds: float = 0.2
    # -14 的暂停时长。默认 1 小时；运行时可以在测试里调短，
    # 但生产配置仍由运行时显式传入 3600。
    pause_seconds: float = ACCOUNT_PAUSE_SECONDS


class WeChatMonitor:
    def __init__(
        self,
        *,
        account_id: str,
        api: object,
        store: WeChatStore,
        service: WeChatService,
        config: MonitorConfig | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.account_id = account_id
        self.api = api
        self.store = store
        self.service = service
        self.config = config or MonitorConfig()
        self.clock = clock
        self.sleep = sleep
        self._last_failure_log = 0.0
        self.long_poll_seconds = self._clamp_poll(
            self.config.default_long_poll_seconds
        )

    def _clamp_poll(self, seconds: float) -> float:
        return max(
            self.config.min_long_poll_seconds,
            min(self.config.max_long_poll_seconds, float(seconds)),
        )

    def _is_paused(self, now: float) -> bool:
        state = self.store.account_state(self.account_id)
        paused_until = float(state.get("paused_until", 0) or 0)
        return paused_until > now

    async def run(self, stop_event: asyncio.Event) -> str:
        """跑监听循环。返回退出原因（stopped / paused / error）。"""
        now = self.clock()
        if self._is_paused(now):
            state = self.store.account_state(self.account_id)
            logger.warning(
                "微信账号 %s 仍处于暂停状态（原因：%s），本次不连接",
                self.account_id, str(state.get("last_error", ""))[:120],
            )
            return "paused"

        await self._notify("start")
        failures = 0
        reason = "stopped"
        try:
            while not stop_event.is_set():
                now = self.clock()
                if self._is_paused(now):
                    reason = "paused"
                    break

                cursor = self.store.get_cursor(self.account_id)
                try:
                    page = await self.api.get_updates(
                        cursor=cursor, timeout_seconds=self.long_poll_seconds
                    )
                except TransportTimeout:
                    # 长轮询超时：正常，不算失败
                    continue
                except asyncio.CancelledError:
                    raise
                except (TransportError, ApiHttpError, ProtocolError) as exc:
                    failures += 1
                    self._log_failure(failures, exc)
                    await self._backoff(failures)
                    continue
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    self._log_failure(failures, exc, unexpected=True)
                    await self._backoff(failures)
                    continue

                if page.ret != 0 or (page.errcode not in (None, 0)):
                    code = page.errcode if page.errcode not in (None, 0) else page.ret
                    if code == PAUSE_ERROR_CODE:
                        pause_seconds = max(0.0, float(self.config.pause_seconds))
                        until = self.clock() + pause_seconds
                        self.store.set_account_paused(
                            self.account_id,
                            paused_until=until,
                            reason=f"微信服务端返回 {PAUSE_ERROR_CODE}，暂停账号会话",
                            now=self.clock(),
                        )
                        logger.warning(
                            "微信服务端返回 -14，账号 %s 暂停 %.0f 秒，不自动绕过",
                            self.account_id, pause_seconds,
                        )
                        reason = "paused"
                        break
                    failures += 1
                    logger.warning(
                        "微信长轮询返回业务错误码 %s（连续 %d 次）", code, failures
                    )
                    await self._backoff(failures)
                    continue

                failures = 0
                if page.longpolling_timeout_ms:
                    self.long_poll_seconds = self._clamp_poll(
                        page.longpolling_timeout_ms / 1000.0
                    )

                all_processed = True
                for raw in page.messages:
                    if stop_event.is_set():
                        all_processed = False
                        break
                    try:
                        await self.service.handle_update(raw)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - 一条坏消息不能停掉监听
                        logger.error(
                            "处理一条微信消息时出现未预期错误：%s",
                            safe_exception(exc),
                        )

                # 整页处理完再保存游标。这一页里被成功 claim 的消息，
                # 即使这里崩溃也不会执行第二次；没处理完就不保存，
                # 下次重连由 inbound 的 claim 挡住重复执行。
                if page.cursor and all_processed:
                    self.store.set_cursor(
                        self.account_id, page.cursor, self.clock()
                    )

                if not page.messages and not page.cursor:
                    # 服务端立即返回空页时别把 CPU 打满。
                    await self.sleep(self.config.empty_page_sleep_seconds)
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        finally:
            await self._notify("stop")
        return reason

    def _log_failure(
        self, failures: int, exc: BaseException, *, unexpected: bool = False
    ) -> None:
        """断网持续时不要每 30 秒刷一条：前三次必记，之后最多每 5 分钟一条。"""
        now = self.clock()
        if failures > self.config.long_error_threshold:
            if now - self._last_failure_log < 300.0:
                return
        self._last_failure_log = now
        label = "未预期错误" if unexpected else "失败"
        logger.warning(
            "微信长轮询%s（连续 %d 次）：%s", label, failures, safe_exception(exc)
        )

    async def _backoff(self, failures: int) -> None:
        delay = (
            self.config.long_error_backoff_seconds
            if failures >= self.config.long_error_threshold
            else self.config.error_backoff_seconds
        )
        if delay > 0:
            await self.sleep(delay)

    async def _notify(self, kind: str) -> None:
        try:
            if kind == "start":
                await self.api.notify_start()
            else:
                await self.api.notify_stop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响收发
            logger.info("微信上下线通知失败（%s）：%s", kind, safe_exception(exc))
