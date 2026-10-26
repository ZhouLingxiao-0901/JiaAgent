"""真实输入的急停与速率限制。

这里不发送任何输入，只回答两个问题：

  1. 现在允许执行真实鼠标输入吗？（急停是否被按下）
  2. 这一次点击的节奏是否在安全范围内？（两次间隔、每分钟次数）

**急停是持久的。** 一旦触发，状态会写到 `data/input_emergency_stop.json`；
服务重启后会重新读回来，不会因为重启就悄悄恢复真实输入。清除急停只有
网页上的用户显式确认这一条路：先取得短时、一次性的恢复挑战，再调用
`reset_if_generation()`（见 `app/api/input.py`）。模型、工具和工作流
都没有调用它的入口。

**速率限制是保守的硬上限。** 配置只能把它调得更严格，不能放宽：
两次真实点击至少间隔 1 秒，每分钟最多 5 次。超限直接拒绝，不排队、
不拆分、不重试。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 不可放宽的底线。配置可以更慢，但不能更快。
SAFE_MIN_INTERVAL_SECONDS = 1.0
SAFE_MAX_PER_MINUTE = 5
RATE_WINDOW_SECONDS = 60.0


# ----------------------------------------------------------------------
# 急停
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class EmergencyState:
    """急停的当前状态。只读快照，给接口和日志用。"""

    stopped: bool = False
    reason: str = ""
    triggered_at: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stopped": self.stopped,
            "reason": self.reason,
            "triggered_at": self.triggered_at,
            "source": self.source,
        }


class EmergencyStop:
    """全局急停开关。

    `path=None` 表示只存在内存里（测试用）。生产默认写到 data/ 下，
    这样重启后仍然保持停止状态。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._clock = clock or (
            lambda: datetime.now().isoformat(timespec="seconds")
        )
        self._lock = threading.RLock()
        self._state = EmergencyState()
        # 每次触发/恢复都自增。恢复请求会绑定触发时的代次；如果期间又
        # 发生了一次急停，旧请求就不能再清除新的急停状态。
        self._generation = 0
        self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 读不出来就按"已急停"，失败关闭
            logger.warning(
                "读取急停状态失败，按已急停处理：%s", self._path, exc_info=True
            )
            self._state = EmergencyState(
                stopped=True,
                reason="无法读取持久化的急停状态，按已急停处理。",
                triggered_at="",
                source="persisted-corrupt",
            )
            return
        if not isinstance(data, dict) or data.get("stopped") is not True:
            return
        self._state = EmergencyState(
            stopped=True,
            reason=str(data.get("reason") or ""),
            triggered_at=str(data.get("triggered_at") or ""),
            source=str(data.get("source") or "persisted"),
        )
        logger.warning("检测到持久化的急停状态：%s", self._state.reason)

    def _persist_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if not self._state.stopped:
                if self._path.exists():
                    self._path.unlink()
                return
            payload = self._state.to_dict()
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception:  # noqa: BLE001 - 内存里的停止状态仍然有效
            logger.exception("写入急停状态失败；本次运行仍保持停止")

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._state.stopped

    @property
    def generation(self) -> int:
        """急停状态代次。trigger/reset 都会让它变化。"""
        with self._lock:
            return self._generation

    def snapshot(self) -> EmergencyState:
        with self._lock:
            return self._state

    def block_reason(self) -> str:
        """空串 = 没有急停；非空 = 必须拒绝真实输入的原因。"""
        with self._lock:
            state = self._state
        if not state.stopped:
            return ""
        detail = f"（{state.triggered_at}）" if state.triggered_at else ""
        reason = f"：{state.reason}" if state.reason else ""
        return (
            f"真实鼠标输入已被急停{detail}{reason}。"
            "恢复必须由用户在界面上显式确认，模型和工作流不能清除急停。"
        )

    def trigger(self, reason: str = "", *, source: str = "user") -> EmergencyState:
        """按下急停。只能由用户界面调用。"""
        with self._lock:
            self._state = EmergencyState(
                stopped=True,
                reason=str(reason or "").strip(),
                triggered_at=self._clock(),
                source=str(source or "user"),
            )
            self._generation += 1
            self._persist_locked()
        logger.warning("真实输入急停已触发：%s", reason or "（没有填写原因）")
        return self.snapshot()

    def reset(self, *, confirmed: bool, source: str = "user") -> tuple[bool, str]:
        """恢复真实输入。必须由用户显式确认（confirmed=True）。

        恢复**不等于**启用真实输入：双重启用条件仍然要满足，而且后端
        只在启动时按条件构造。没有后端时，恢复之后依然不会点击。
        """
        if not confirmed:
            return False, "恢复真实输入需要用户显式确认，本次没有恢复。"
        with self._lock:
            self._state = EmergencyState()
            self._generation += 1
            self._persist_locked()
        logger.warning("真实输入急停已被用户恢复（来源：%s）", source)
        return True, "急停已恢复。真实输入是否可用仍取决于双重启用条件。"

    def reset_if_generation(
        self,
        expected_generation: int,
        *,
        confirmed: bool,
        source: str = "user",
    ) -> tuple[bool, str]:
        """只有急停代次仍是 `expected_generation` 时才恢复。

        检查和清除在**同一把锁**里完成：如果确认之后又发生了一次急停，
        这次恢复会失败，不会把新的急停悄悄清掉。这是"急停优先"在并发
        下的落点。
        """
        if not confirmed:
            return False, "恢复真实输入需要用户显式确认，本次没有恢复。"
        with self._lock:
            if not self._state.stopped:
                return False, "当前没有处于急停状态，不需要恢复。"
            if int(expected_generation) != self._generation:
                return False, (
                    "急停状态在确认后已经变化，本次恢复请求已失效。"
                    "请重新确认。"
                )
            self._state = EmergencyState()
            self._generation += 1
            self._persist_locked()
        logger.warning("真实输入急停已被用户恢复（来源：%s）", source)
        return True, "急停已恢复。真实输入是否可用仍取决于双重启用条件。"


# ----------------------------------------------------------------------
# 速率限制
# ----------------------------------------------------------------------
class ClickRateLimiter:
    """服务端的真实点击速率限制。

    时钟可注入，测试不需要真的等待。`check()` 不消耗额度，`try_acquire()`
    在通过时记录一次点击。调用方应在真正发送输入前才 acquire。
    """

    def __init__(
        self,
        *,
        min_interval: float = SAFE_MIN_INTERVAL_SECONDS,
        max_per_minute: int = SAFE_MAX_PER_MINUTE,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.min_interval = max(SAFE_MIN_INTERVAL_SECONDS, float(min_interval))
        self.max_per_minute = max(
            1, min(SAFE_MAX_PER_MINUTE, int(max_per_minute))
        )
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._clicks: deque[float] = deque()

    def _prune_locked(self, now: float) -> None:
        while self._clicks and now - self._clicks[0] >= RATE_WINDOW_SECONDS:
            self._clicks.popleft()

    def check(self) -> tuple[bool, str]:
        """只看现在能不能点，不消耗额度。"""
        with self._lock:
            now = float(self._clock())
            self._prune_locked(now)
            if self._clicks:
                elapsed = now - self._clicks[-1]
                if elapsed < self.min_interval:
                    wait = self.min_interval - max(0.0, elapsed)
                    return (
                        False,
                        f"两次真实点击至少间隔 {self.min_interval:g} 秒，"
                        f"请至少等待 {wait:.1f} 秒。不会自动排队或重试。",
                    )
            if len(self._clicks) >= self.max_per_minute:
                return (
                    False,
                    f"最近 {RATE_WINDOW_SECONDS:g} 秒内已经执行了 "
                    f"{len(self._clicks)} 次真实点击，达到上限 "
                    f"{self.max_per_minute} 次。不会自动排队或重试。",
                )
        return True, ""

    def try_acquire(self) -> tuple[bool, str]:
        """检查并占用一次额度。失败时不占用。"""
        with self._lock:
            now = float(self._clock())
            self._prune_locked(now)
            if self._clicks:
                elapsed = now - self._clicks[-1]
                if elapsed < self.min_interval:
                    wait = self.min_interval - max(0.0, elapsed)
                    return (
                        False,
                        f"两次真实点击至少间隔 {self.min_interval:g} 秒，"
                        f"请至少等待 {wait:.1f} 秒。不会自动排队或重试。",
                    )
            if len(self._clicks) >= self.max_per_minute:
                return (
                    False,
                    f"最近 {RATE_WINDOW_SECONDS:g} 秒内已经执行了 "
                    f"{len(self._clicks)} 次真实点击，达到上限 "
                    f"{self.max_per_minute} 次。不会自动排队或重试。",
                )
            self._clicks.append(now)
        return True, ""

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = float(self._clock())
            self._prune_locked(now)
            return {
                "min_interval_seconds": self.min_interval,
                "max_per_minute": self.max_per_minute,
                "clicks_in_window": len(self._clicks),
                "window_seconds": RATE_WINDOW_SECONDS,
            }


# ----------------------------------------------------------------------
# 组合
# ----------------------------------------------------------------------
class RealInputController:
    """真实后端在执行前必须过的两个闸门：急停 + 速率。"""

    def __init__(
        self,
        *,
        emergency: EmergencyStop | None = None,
        limiter: ClickRateLimiter | None = None,
    ) -> None:
        self.emergency = emergency or EmergencyStop()
        self.limiter = limiter or ClickRateLimiter()

    def block_reason(self) -> str:
        return self.emergency.block_reason()

    def acquire_click(self) -> tuple[bool, str]:
        """急停检查 + 占用一次速率额度。失败时绝不发送输入。"""
        reason = self.emergency.block_reason()
        if reason:
            return False, reason
        return self.limiter.try_acquire()


def _configured_limiter() -> ClickRateLimiter:
    try:
        from app.settings import get_tools_config

        input_cfg = get_tools_config().input
        return ClickRateLimiter(
            min_interval=input_cfg.min_click_interval_seconds,
            max_per_minute=input_cfg.max_clicks_per_minute,
        )
    except Exception:  # noqa: BLE001 - 配置读不到就退回最保守的默认值
        logger.warning("读取输入速率配置失败，使用默认值", exc_info=True)
        return ClickRateLimiter()


def _default_emergency_path() -> Path | None:
    try:
        from app.settings import DATA_DIR

        return DATA_DIR / "input_emergency_stop.json"
    except Exception:  # noqa: BLE001
        return None


# 生产用的全局实例。测试可以自己构造隔离的 EmergencyStop/ClickRateLimiter。
emergency = EmergencyStop(_default_emergency_path())
limiter = _configured_limiter()
controller = RealInputController(emergency=emergency, limiter=limiter)
