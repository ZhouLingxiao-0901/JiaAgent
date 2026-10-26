"""Jia-WeChat 的可选运行时。

**默认关闭**：环境变量 `JIA_WECHAT_ENABLED` 不是真值时，不创建任何网络
客户端，也不读凭证。即使打开，缺少凭证或允许名单为空时也拒绝连接——
“登录成功”不是“可以开始接消息”，必须显式配对。

所有配置来自环境变量（或 .env），不修改 config/tools.yaml。凭证路径默认
`data/wechat/`，已被 .gitignore 覆盖。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from app.settings import DATA_DIR, ENV_FILE
from app.wechat.api import ApiConfig, WeChatApi
from app.wechat.bridge import JiaRuntimeBridge
from app.wechat.credentials import (
    ApprovalSecretStore,
    AllowListStore,
    CredentialStore,
    DpapiProtector,
    Protector,
)
from app.wechat.monitor import ACCOUNT_PAUSE_SECONDS, MonitorConfig, WeChatMonitor
from app.wechat.remote_auth import RemoteAuth
from app.wechat.runtime_state import (
    clear_runtime_status,
    write_runtime_status,
)
from app.wechat.service import ApiOutbound, WeChatService
from app.wechat.store import WeChatStore
from app.wechat.task_watch import TaskWatcher

logger = logging.getLogger(__name__)

DEFAULT_CHANNEL_VERSION = "0.1.0"
DEFAULT_CLIENT_VERSION = (0, 1, 0)
DEFAULT_BOT_AGENT = "Jia-WeChat/0.1.0"
DEFAULT_LONG_POLL = 35.0
DEFAULT_WATCH_INTERVAL = 2.0


def _truthy(value: str) -> bool:
    return str(value or "").strip().casefold() in ("1", "true", "yes", "on")


def _runtime_env() -> dict[str, str]:
    """进程环境 + `.env` 里的 `JIA_WECHAT_*`，进程环境优先。

    `WeChatRuntimeConfig` 是普通 dataclass，不走 pydantic-settings，所以
    `.env` 不会自动出现在 `os.environ` 里。文档一直让用户把开关写进
    `.env`，这里显式读取，保持两边一致。只取 `JIA_WECHAT_*`，不把其他
    密钥带进这个配置对象；读取失败时退回纯进程环境。
    """
    values: dict[str, str] = {}
    try:
        parsed = dotenv_values(ENV_FILE)
    except Exception:  # noqa: BLE001 - .env 损坏不能阻止主服务启动
        parsed = {}
    for key, value in parsed.items():
        if key.startswith("JIA_WECHAT_") and value is not None:
            values[key] = str(value)
    values.update(os.environ)
    return values


def _version_tuple(value: str) -> tuple[int, int, int]:
    parts = str(value or "").strip().split(".")
    if len(parts) != 3:
        return DEFAULT_CLIENT_VERSION
    try:
        numbers = tuple(int(p) for p in parts)
    except ValueError:
        return DEFAULT_CLIENT_VERSION
    if any(n < 0 or n > 255 for n in numbers):
        return DEFAULT_CLIENT_VERSION
    return numbers  # type: ignore[return-value]


@dataclass(frozen=True)
class WeChatRuntimeConfig:
    enabled: bool = False
    account_id: str = ""
    state_dir: Path = field(default_factory=lambda: DATA_DIR / "wechat")
    channel_version: str = DEFAULT_CHANNEL_VERSION
    client_version: tuple[int, int, int] = DEFAULT_CLIENT_VERSION
    bot_agent: str = DEFAULT_BOT_AGENT
    route_tag: str = ""
    long_poll_timeout: float = DEFAULT_LONG_POLL
    watch_interval: float = DEFAULT_WATCH_INTERVAL
    account_pause_seconds: float = ACCOUNT_PAUSE_SECONDS
    supervisor_backoff_base: float = 2.0
    supervisor_backoff_max: float = 60.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "WeChatRuntimeConfig":
        values = _runtime_env() if env is None else env
        raw_dir = str(values.get("JIA_WECHAT_STATE_DIR", "")).strip()
        state_dir = Path(raw_dir) if raw_dir else DATA_DIR / "wechat"
        try:
            long_poll = float(values.get("JIA_WECHAT_LONG_POLL_TIMEOUT_SECONDS", "") or DEFAULT_LONG_POLL)
        except ValueError:
            long_poll = DEFAULT_LONG_POLL
        return cls(
            enabled=_truthy(values.get("JIA_WECHAT_ENABLED", "")),
            account_id=str(values.get("JIA_WECHAT_ACCOUNT_ID", "")).strip(),
            state_dir=state_dir,
            channel_version=(
                str(values.get("JIA_WECHAT_CHANNEL_VERSION", "")).strip()
                or DEFAULT_CHANNEL_VERSION
            ),
            client_version=_version_tuple(
                str(values.get("JIA_WECHAT_CLIENT_VERSION", "")).strip()
            ),
            bot_agent=(
                str(values.get("JIA_WECHAT_BOT_AGENT", "")).strip()
                or DEFAULT_BOT_AGENT
            ),
            route_tag=str(values.get("JIA_WECHAT_ROUTE_TAG", "")).strip(),
            long_poll_timeout=long_poll,
        )


class WeChatRuntime:
    def __init__(
        self,
        config: WeChatRuntimeConfig,
        *,
        protector: Protector | None = None,
        transport: Any = None,
    ) -> None:
        self.config = config
        self._protector = protector
        self._transport = transport
        self._stop = asyncio.Event()
        self._supervisor_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self._service: WeChatService | None = None
        self._store: WeChatStore | None = None
        self.account_id = ""
        self._monitor_alive = False
        self._restart_count = 0
        self._started_at = 0.0
        self._last_restart_log = 0.0
        self._last_error = ""

    @property
    def started(self) -> bool:
        """监督任务是否真的还在运行，而不是“启动动作曾经成功过”。"""
        task = self._supervisor_task
        return task is not None and not task.done()

    @property
    def monitor_alive(self) -> bool:
        return self._monitor_alive and self.started

    async def start(self) -> bool:
        if not self.config.enabled:
            return False
        if self.started:
            return True

        state_dir = Path(self.config.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)

        try:
            protector = self._protector or DpapiProtector()
        except Exception as exc:  # noqa: BLE001
            logger.error("微信凭证保护不可用，已拒绝启动：%s", type(exc).__name__)
            return False

        credentials_store = CredentialStore(state_dir, protector)
        account_id = self.config.account_id
        if not account_id:
            accounts = credentials_store.list_accounts()
            if len(accounts) == 1:
                account_id = accounts[0]
            elif len(accounts) > 1:
                logger.error(
                    "存在多个微信账号，请设置 JIA_WECHAT_ACCOUNT_ID 明确指定"
                )
                return False
            else:
                logger.warning("没有微信登录凭证，微信入口不启动")
                return False
        self.account_id = account_id

        try:
            credentials = credentials_store.load(account_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("读取微信凭证失败，已拒绝启动：%s", type(exc).__name__)
            return False
        if credentials is None:
            logger.warning("找不到微信账号 %s 的凭证，微信入口不启动", account_id)
            return False

        allowlist = AllowListStore(state_dir)
        if not any(u.account_id == account_id for u in allowlist.list()):
            logger.warning(
                "微信账号 %s 还没有配对用户，拒绝连接。"
                "请先运行 scripts/wechat_cli.py pair",
                account_id,
            )
            return False

        try:
            approval_secret = ApprovalSecretStore(
                state_dir, protector
            ).load_or_create()
        except Exception as exc:  # noqa: BLE001
            logger.error("读取远程授权密钥失败，已拒绝启动：%s", type(exc).__name__)
            return False

        store = WeChatStore(state_dir / "state.db")
        try:
            store.init()
            store.cleanup_approvals(now=time.time())
        except Exception:  # noqa: BLE001
            logger.exception("初始化微信状态库失败，已拒绝启动")
            return False

        api = WeChatApi(
            ApiConfig(
                base_url=credentials.base_url,
                token=credentials.token,
                channel_version=self.config.channel_version,
                client_version=self.config.client_version,
                bot_agent=self.config.bot_agent,
                route_tag=self.config.route_tag,
                long_poll_timeout_seconds=self.config.long_poll_timeout,
            ),
            transport=self._transport,
        )
        bridge = JiaRuntimeBridge(store=store)
        remote_auth = RemoteAuth(store, approval_secret)
        service = WeChatService(
            account_id=account_id,
            allowlist=allowlist,
            store=store,
            bridge=bridge,
            remote_auth=remote_auth,
            outbound=ApiOutbound(api),
        )
        watcher = TaskWatcher(
            bridge=bridge,
            store=store,
            remote_auth=remote_auth,
            notifier=service.notify,
            allowlist=allowlist,
            is_connected=self._notifications_allowed,
            interval=self.config.watch_interval,
        )
        monitor = self._build_monitor(
            account_id=account_id, api=api, store=store, service=service
        )

        self._service = service
        self._store = store
        self._stop = asyncio.Event()
        self._monitor_alive = False
        self._restart_count = 0
        self._last_restart_log = 0.0
        self._last_error = ""

        self._supervisor_task = asyncio.create_task(
            self._supervise(monitor=monitor, store=store)
        )
        self._watch_task = asyncio.create_task(watcher.run(self._stop))
        logger.info(
            "微信入口已启动（账号 %s；长轮询 %.0fs）",
            account_id, self.config.long_poll_timeout,
        )
        return True

    def _build_monitor(
        self,
        *,
        account_id: str,
        api: Any,
        store: WeChatStore,
        service: WeChatService,
    ) -> Any:
        """测试可以覆盖这个工厂，注入一个不联网的假监听。"""
        return WeChatMonitor(
            account_id=account_id,
            api=api,
            store=store,
            service=service,
            config=MonitorConfig(
                default_long_poll_seconds=self.config.long_poll_timeout,
                pause_seconds=self.config.account_pause_seconds,
            ),
        )

    def _notifications_allowed(self) -> bool:
        """监听在线且账号不在暂停期时，才允许 watcher 发送任务通知。"""
        if not self.monitor_alive or self._store is None:
            return False
        try:
            state = self._store.account_state(self.account_id)
            paused_until = float(state.get("paused_until", 0) or 0)
        except Exception:  # noqa: BLE001 - 状态库异常时失败关闭
            return False
        return paused_until <= time.time()

    def _write_state(self, state: str) -> None:
        if not self.account_id:
            return
        write_runtime_status(
            self.config.state_dir,
            account_id=self.account_id,
            state=state,
            monitor_alive=self._monitor_alive,
            restart_count=self._restart_count,
            started_at=self._started_at,
            last_error=self._last_error,
        )

    async def _supervise(self, *, monitor: Any, store: WeChatStore) -> None:
        """监督长轮询：意外退出后按有界退避重连；-14 暂停期间不重试。"""
        self._started_at = time.time()
        backoff = max(0.01, float(self.config.supervisor_backoff_base))
        max_backoff = max(backoff, float(self.config.supervisor_backoff_max))
        self._write_state("starting")
        try:
            while not self._stop.is_set():
                self._monitor_alive = True
                self._write_state("running")
                reason = "stopped"
                try:
                    self._monitor_task = asyncio.create_task(
                        monitor.run(self._stop)
                    )
                    reason = await self._monitor_task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 监督任务不能因此消失
                    reason = "error"
                    self._last_error = type(exc).__name__
                    self._log_restart(exc=exc)
                finally:
                    self._monitor_alive = False
                    self._monitor_task = None

                if self._stop.is_set():
                    break

                if reason == "paused":
                    self._write_state("paused")
                    try:
                        await self._wait_paused(store)
                        if self._stop.is_set():
                            break
                        # 暂停到期后才清除并恢复；不绕过平台的会话限制。
                        store.clear_account_paused(self.account_id, time.time())
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        # 状态库临时出错不能杀死监督任务，也不能立刻重连。
                        self._restart_count += 1
                        self._last_error = type(exc).__name__
                        self._log_restart(exc=exc)
                        self._write_state("reconnecting")
                        await self._wait_backoff(backoff)
                        backoff = min(max_backoff, backoff * 2)
                        continue
                    backoff = max(0.01, float(self.config.supervisor_backoff_base))
                    continue

                self._restart_count += 1
                if reason != "error":
                    self._last_error = f"监听意外退出（{reason}）"
                    self._log_restart()
                self._write_state("reconnecting")
                await self._wait_backoff(backoff)
                backoff = min(max_backoff, backoff * 2)
        finally:
            self._monitor_alive = False
            self._monitor_task = None
            if self._stop.is_set():
                clear_runtime_status(self.config.state_dir)
            else:
                # 监督任务自己意外结束：停掉 watcher，避免留下一个还在
                # 尝试发送通知的孤儿任务。状态文件保留为 stopped。
                self._stop.set()
                self._write_state("stopped")

    def _log_restart(self, *, exc: Exception | None = None) -> None:
        """重启日志限流：首次立刻记，之后最多每 5 分钟一条。"""
        now = time.time()
        if self._last_restart_log and now - self._last_restart_log < 300.0:
            return
        self._last_restart_log = now
        if exc is not None:
            logger.warning(
                "微信监听异常退出（第 %d 次），将按退避重连：%s",
                self._restart_count + 1, type(exc).__name__,
            )
        else:
            logger.warning(
                "微信监听意外退出（第 %d 次），将按退避重连", self._restart_count + 1
            )

    async def _wait_backoff(self, delay: float) -> None:
        if delay <= 0:
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _wait_paused(self, store: WeChatStore) -> None:
        """等到 paused_until 到期。期间不发起任何网络请求。"""
        while not self._stop.is_set():
            state = store.account_state(self.account_id)
            until = float(state.get("paused_until", 0) or 0)
            now = time.time()
            if until <= now:
                if until <= 0:
                    # 监听声称暂停却没有留下暂停时间：按配置补一个，
                    # 避免监督任务空转重试。
                    until = now + max(0.0, float(self.config.account_pause_seconds))
                    store.set_account_paused(
                        self.account_id,
                        paused_until=until,
                        reason="监听进入暂停状态",
                        now=now,
                    )
                    continue
                return
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=min(until - now, 30.0)
                )
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        monitor_task = self._monitor_task
        tasks = [
            t for t in (
                self._supervisor_task, self._watch_task, monitor_task,
            ) if t is not None
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._supervisor_task = None
        self._monitor_task = None
        self._watch_task = None
        self._monitor_alive = False
        clear_runtime_status(self.config.state_dir)
        if self._service is not None:
            await self._service.stop()
            self._service = None
        self._store = None
        if self.account_id:
            logger.info("微信入口已停止（账号 %s）", self.account_id)


_runtime: WeChatRuntime | None = None


async def start_wechat_runtime(
    config: WeChatRuntimeConfig | None = None,
    *,
    protector: Protector | None = None,
    transport: Any = None,
) -> bool:
    global _runtime
    if _runtime is not None and _runtime.started:
        return True
    runtime = WeChatRuntime(
        config or WeChatRuntimeConfig.from_env(),
        protector=protector,
        transport=transport,
    )
    try:
        started = await runtime.start()
    except Exception:  # noqa: BLE001 - 微信入口失败不能拖垮 Jia 主服务
        logger.exception("微信入口启动失败；Jia 其他功能继续运行")
        return False
    if not started:
        return False
    _runtime = runtime
    return True


async def stop_wechat_runtime() -> None:
    global _runtime
    runtime = _runtime
    _runtime = None
    if runtime is not None:
        try:
            await runtime.stop()
        except Exception:  # noqa: BLE001
            logger.exception("停止微信入口时出错")
