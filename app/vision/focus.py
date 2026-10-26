"""最近使用的非佳窗口追踪。

为什么需要它：
  inspect_screen 的默认捕获范围是「最近用过的那个应用窗口」，而不是整个桌面。
  但佳的聊天网页通常就在前台，所以不能简单地「取当前前台窗口」——那会把
  佳自己截进去。这里在后台持续记录最近一个**非佳**、可见、没有最小化的
  外部窗口；当佳自己的网页在前台时，保留上一个符合条件的外部窗口。

生命周期：
  由 app/main.py 的 lifespan 在服务启动时 start()、关闭时 stop()。
  后台线程只读窗口元数据，不截图、不发送任何输入、不创建文件。

几条硬规则：
  - 过期（默认 120 秒）、最小化、关闭、句柄失效、进程变化时，latest()
    返回 (None, 原因)，**不会改选另一个窗口**。调用方必须把原因告诉用户。
  - 不按 electron.exe 这类通用进程名排除应用；只排除配置里明确列出的
    佳自身进程名，以及标题里明确是佳聊天页面的窗口。
  - 取不到前台窗口（锁屏、切换桌面）不是错误，只是这一轮没有新记录。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app.vision import window as window_mod
from app.vision.model import WindowInfo

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 120.0
DEFAULT_POLL_SECONDS = 1.0

# 默认只写很具体的片段。注意不要写成 "JiaAgent"：
# 那会把标题为 "JiaAgent MVP 项目架构 - Visual Studio Code" 的编辑器也误伤。
DEFAULT_TITLE_PATTERNS: tuple[str, ...] = (
    "JiaAgent 佳",
    "localhost:8000",
    "127.0.0.1:8000",
)

# 佳自身界面的进程名。默认空：现在的佳是网页，跑在浏览器里；
# 不把 electron.exe 这类通用进程名写进来，否则别的 Electron 应用会被误伤。
DEFAULT_PROCESS_NAMES: tuple[str, ...] = ()


@dataclass(frozen=True)
class FocusTarget:
    """最近一次观察到的、符合条件的外部窗口。"""

    hwnd: int
    pid: int
    process: str
    title: str
    seen_at: float              # time.monotonic() 的时刻，用来算过期
    seen_wall: str = ""         # 给人看的时间戳

    def age(self, now: float) -> float:
        return max(0.0, float(now) - self.seen_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "hwnd": self.hwnd,
            "pid": self.pid,
            "process": self.process,
            "title": self.title,
            "seen_at": self.seen_wall,
        }


def is_jia_window(
    info: WindowInfo | None,
    *,
    title_patterns: tuple[str, ...] | list[str] | None = None,
    process_names: tuple[str, ...] | list[str] | None = None,
) -> bool:
    """这个窗口是不是佳自己的界面。

    进程名只做**精确**匹配（配置里明确列出的「佳」进程）；
    标题按配置里的片段匹配。默认片段故意写得很具体，见文件开头。
    """
    if info is None:
        return False

    process = (info.process or "").strip().casefold()
    names = DEFAULT_PROCESS_NAMES if process_names is None else process_names
    normalized_names = {
        str(item).strip().casefold() for item in names if str(item).strip()
    }
    if process and process in normalized_names:
        return True

    title = (info.title or "").strip().casefold()
    patterns = DEFAULT_TITLE_PATTERNS if title_patterns is None else title_patterns
    for pattern in patterns:
        text = str(pattern).strip().casefold()
        if text and text in title:
            return True
    return False


def _usable(info: WindowInfo | None) -> bool:
    """能不能把它记成「最近使用的外部窗口」。"""
    return bool(
        info is not None
        and info.hwnd
        and info.visible
        and not info.minimized
        and info.is_valid
    )


class FocusTracker:
    """在后台记录最近一个符合条件的外部窗口。

    所有外部依赖都可以从构造函数注入（时钟、前台窗口来源、窗口查询），
    这样测试不需要真实的前台窗口，也不会受用户实际在操作什么影响。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        title_patterns: tuple[str, ...] | list[str] | None = None,
        process_names: tuple[str, ...] | list[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        foreground_provider: Callable[[], WindowInfo | None] | None = None,
        window_lookup: Callable[[int], WindowInfo | None] | None = None,
    ) -> None:
        self._ttl = max(0.0, float(ttl_seconds))
        self._poll = max(0.05, float(poll_seconds))
        self._title_patterns = tuple(
            DEFAULT_TITLE_PATTERNS if title_patterns is None else title_patterns
        )
        self._process_names = tuple(
            DEFAULT_PROCESS_NAMES if process_names is None else process_names
        )
        self._clock = clock
        self._foreground = foreground_provider or window_mod.get_foreground_window
        self._lookup = window_lookup or window_mod.get_window_info
        self._lock = threading.Lock()
        self._target: FocusTarget | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 配置与状态
    # ------------------------------------------------------------------
    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def configure(
        self,
        *,
        ttl_seconds: float | None = None,
        poll_seconds: float | None = None,
        title_patterns: tuple[str, ...] | list[str] | None = None,
        process_names: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        """更新配置。传 None 表示这一项保持不变。"""
        with self._lock:
            if ttl_seconds is not None:
                self._ttl = max(0.0, float(ttl_seconds))
            if poll_seconds is not None:
                self._poll = max(0.05, float(poll_seconds))
            if title_patterns is not None:
                self._title_patterns = tuple(title_patterns)
            if process_names is not None:
                self._process_names = tuple(process_names)

    # ------------------------------------------------------------------
    # 观察
    # ------------------------------------------------------------------
    def poll_once(self) -> FocusTarget | None:
        """观察一次前台窗口。佳自己的界面、不可用窗口都不会被记录。

        返回刚记录下的目标；这一轮没有符合条件的目标就返回 None。
        注意：返回 None **不会**清空之前记录的目标。
        """
        info = self._foreground()
        if info is None:
            return None
        if is_jia_window(
            info,
            title_patterns=self._title_patterns,
            process_names=self._process_names,
        ):
            return None
        if not _usable(info):
            return None

        target = FocusTarget(
            hwnd=int(info.hwnd),
            pid=int(info.pid or 0),
            process=info.process or "",
            title=info.title or "",
            seen_at=float(self._clock()),
            seen_wall=datetime.now().isoformat(timespec="seconds"),
        )
        with self._lock:
            self._target = target
        return target

    def latest(self, *, allow_lazy_poll: bool = True) -> tuple[FocusTarget | None, str]:
        """返回最近一个符合条件的外部窗口，以及失败原因。

        这里**只校验记录下来的那一个窗口**。它失效时返回 (None, 原因)，
        绝不会去窗口列表里另找一个——「不擅自换成其他目标」就靠这一条。
        """
        with self._lock:
            target = self._target

        if target is None and allow_lazy_poll:
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - 观察失败不能把调用方带崩
                logger.debug("懒加载观察前台窗口失败", exc_info=True)
            with self._lock:
                target = self._target

        if target is None:
            return None, "还没有记录到最近使用的外部窗口"

        now = float(self._clock())
        if self._ttl > 0 and target.age(now) > self._ttl:
            return None, f"最近使用的窗口已经超过 {self._ttl:g} 秒没有更新"

        try:
            info = self._lookup(int(target.hwnd))
        except Exception:  # noqa: BLE001
            logger.debug("读取最近窗口状态失败", exc_info=True)
            return None, "无法读取最近使用的窗口状态"

        if info is None:
            return None, "最近使用的窗口已经关闭或句柄失效"
        if not info.visible:
            return None, "最近使用的窗口现在不可见"
        if info.minimized:
            return None, "最近使用的窗口现在已最小化"
        if not info.is_valid or not info.hwnd:
            return None, "最近使用的窗口尺寸无效"
        if is_jia_window(
            info,
            title_patterns=self._title_patterns,
            process_names=self._process_names,
        ):
            return None, "最近使用的窗口是佳自己的界面"
        if target.pid and info.pid and int(info.pid) != int(target.pid):
            return None, "最近使用的窗口的进程已经变化"
        if (
            target.process
            and info.process
            and info.process.casefold() != target.process.casefold()
        ):
            return None, "最近使用的窗口的进程已经变化"

        # 标题可能在记录之后变了（比如编辑器换了文件）。这不算「换了目标」，
        # 但 prepare 阶段会把**当前**标题固定下来给确认卡片看。
        return (
            FocusTarget(
                hwnd=int(info.hwnd),
                pid=int(info.pid or target.pid or 0),
                process=info.process or target.process,
                title=info.title or "",
                seen_at=target.seen_at,
                seen_wall=target.seen_wall,
            ),
            "",
        )

    # ------------------------------------------------------------------
    # 后台线程
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动后台追踪。重复调用是安全的。"""
        if self.is_running:
            return

        self._stop.clear()
        # 先同步观察一次，这样刚启动就能拿到当前前台的外部窗口。
        try:
            self.poll_once()
        except Exception:  # noqa: BLE001
            logger.debug("启动时观察前台窗口失败", exc_info=True)

        thread = threading.Thread(
            target=self._loop, name="jia-focus-tracker", daemon=True
        )
        with self._lock:
            self._thread = thread
        thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """停止后台追踪。重复调用是安全的。"""
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - 后台线程不能因为一次失败就退出
                logger.debug("追踪最近窗口时出错", exc_info=True)
            if self._stop.wait(self._poll):
                return


# 进程级单例。main.py 的 lifespan 负责 start/stop。
tracker = FocusTracker()


def start(vision_config=None) -> None:
    """按配置启动全局追踪器。

    vision_config 可以显式传入（测试用）；不传就惰性读取 tools.yaml。
    配置读不到时也照常启动，用默认的 120 秒 / 1 秒 / 默认过滤片段。
    """
    if vision_config is None:
        try:
            from app.settings import get_tools_config

            vision_config = get_tools_config().vision
        except Exception:  # noqa: BLE001 - 配置有问题时保留默认值
            vision_config = None

    if vision_config is not None:
        tracker.configure(
            ttl_seconds=getattr(vision_config, "focus_ttl_seconds", None),
            poll_seconds=getattr(vision_config, "focus_poll_seconds", None),
            title_patterns=getattr(vision_config, "jia_title_patterns", None),
            process_names=getattr(vision_config, "jia_process_names", None),
        )
    tracker.start()


def stop(timeout: float = 2.0) -> None:
    tracker.stop(timeout)
