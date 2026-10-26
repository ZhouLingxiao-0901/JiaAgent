"""最近使用的非佳窗口追踪测试（视觉感知 · 窗口范围）。

全部是纯逻辑：注入假时钟、假前台窗口、假窗口查询。
不读真实窗口、不截图、不联网、不写文件。

守这些：
  - 正常外部窗口会被记录；佳自己的聊天页不会被记录
  - 佳在前台时保留上一个符合条件的外部窗口
  - 过期、最小化、关闭、不可见、进程变化 -> 明确失败
  - 目标失效时不会改选另一个窗口
  - 不按 electron.exe 这类通用进程名排除应用

用法：
    .venv\\Scripts\\python.exe scripts\\test_vision_focus.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.logging_setup import setup_logging  # noqa: E402
from app.vision import focus  # noqa: E402
from app.vision.model import WindowInfo  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def info(
    hwnd: int,
    title: str,
    process: str = "Code.exe",
    pid: int = 100,
    *,
    visible: bool = True,
    minimized: bool = False,
    rect: tuple[int, int, int, int] = (0, 0, 800, 600),
) -> WindowInfo:
    return WindowInfo(
        hwnd=hwnd,
        title=title,
        pid=pid,
        process=process,
        rect=rect,
        visible=visible,
        minimized=minimized,
    )


def make_tracker(sequence, lookup, clock, *, ttl_seconds: float = 120.0,
                 title_patterns=None, process_names=None) -> focus.FocusTracker:
    return focus.FocusTracker(
        ttl_seconds=ttl_seconds,
        poll_seconds=0.01,
        title_patterns=(
            list(focus.DEFAULT_TITLE_PATTERNS)
            if title_patterns is None else title_patterns
        ),
        process_names=[] if process_names is None else process_names,
        clock=clock,
        foreground_provider=lambda: sequence.pop(0) if sequence else None,
        window_lookup=lambda hwnd: lookup.get(int(hwnd)),
    )


# ----------------------------------------------------------------------
# 一、佳自己的界面怎么识别
# ----------------------------------------------------------------------
def test_jia_filter() -> None:
    print("\n[一] 佳自己的界面识别")

    check(
        "佳聊天页（Edge）被识别为佳自己",
        focus.is_jia_window(
            info(1, "JiaAgent 佳 - 个人 - Microsoft​ Edge", "msedge.exe")
        ),
    )
    check(
        "标题为 JiaAgent 项目的 VS Code **不**被误伤",
        not focus.is_jia_window(
            info(2, "JiaAgent MVP 项目架构 - Visual Studio Code", "Code.exe")
        ),
    )
    check(
        "localhost 页面被识别为佳自己",
        focus.is_jia_window(info(3, "127.0.0.1:8000 - Google Chrome", "chrome.exe")),
    )
    check(
        "配置里的佳进程名精确命中",
        focus.is_jia_window(
            info(4, "任意标题", "JiaAgent.exe"),
            process_names=["JiaAgent.exe"],
        ),
    )
    check(
        "electron.exe 不会因为进程名被排除",
        not focus.is_jia_window(info(5, "Clawd", "electron.exe")),
    )
    check(
        "自定义标题片段生效",
        focus.is_jia_window(
            info(6, "my-jia-window", "msedge.exe"),
            title_patterns=["my-jia"],
        ),
    )
    check("None 不算佳自己", not focus.is_jia_window(None))


# ----------------------------------------------------------------------
# 二、记录与保留
# ----------------------------------------------------------------------
def test_tracking() -> None:
    print("\n[二] 记录最近的外部窗口，佳在前台时保留上一个")

    clock = FakeClock()
    vs = info(10, "JiaAgent MVP 项目架构 - Visual Studio Code", "Code.exe", 101)
    jia = info(11, "JiaAgent 佳 - 个人 - Microsoft​ Edge", "msedge.exe", 102)
    edge = info(12, "新建标签页 - 个人 - Microsoft​ Edge", "msedge.exe", 102)
    lookup = {10: vs, 11: jia, 12: edge}

    tracker = make_tracker([vs, jia, edge], lookup, clock)
    recorded = tracker.poll_once()
    check("外部窗口被记录", recorded is not None and recorded.hwnd == 10,
          str(recorded.hwnd) if recorded else "None")

    target, reason = tracker.latest(allow_lazy_poll=False)
    check("latest 返回这个窗口", target is not None and target.hwnd == 10, reason)
    check("标题和进程都对", target is not None
          and target.title == vs.title and target.process == "Code.exe",
          f"{target.title if target else ''}")

    tracker.poll_once()   # 佳自己的页面在前台
    target, _ = tracker.latest(allow_lazy_poll=False)
    check("佳在前台时保留上一个外部窗口", target is not None and target.hwnd == 10,
          str(target.hwnd) if target else "None")

    tracker.poll_once()   # 换成另一个正常 Edge 页面
    target, _ = tracker.latest(allow_lazy_poll=False)
    check("新的外部窗口会替换旧记录", target is not None and target.hwnd == 12,
          str(target.hwnd) if target else "None")


# ----------------------------------------------------------------------
# 三、过期
# ----------------------------------------------------------------------
def test_expiry() -> None:
    print("\n[三] 过期后明确失败")

    clock = FakeClock()
    vs = info(20, "VS Code", "Code.exe", 201)
    tracker = make_tracker([vs], {20: vs}, clock, ttl_seconds=120.0)
    tracker.poll_once()

    clock.value += 121
    target, reason = tracker.latest(allow_lazy_poll=False)
    check("超过 120 秒后不再可用", target is None, str(target))
    check("原因里说明了过期", "120" in reason and "超过" in reason, reason)

    clock = FakeClock()
    tracker = make_tracker([vs], {20: vs}, clock, ttl_seconds=120.0)
    tracker.poll_once()
    tracker.configure(ttl_seconds=5.0)
    clock.value += 6
    target, reason = tracker.latest(allow_lazy_poll=False)
    check("configure 能改有效期", target is None and "5" in reason, reason)


# ----------------------------------------------------------------------
# 四、目标失效：只校验它自己，绝不改选
# ----------------------------------------------------------------------
def test_invalid_target() -> None:
    print("\n[四] 目标失效时不改选另一个窗口")

    cases = [
        ("最小化", info(30, "VS Code", "Code.exe", 301, minimized=True), "最小化"),
        ("不可见", info(30, "VS Code", "Code.exe", 301, visible=False), "不可见"),
        ("进程变化", info(30, "VS Code", "evil.exe", 999), "进程"),
        ("尺寸无效", info(30, "VS Code", "Code.exe", 301, rect=(10, 10, 10, 10)), "尺寸"),
    ]

    for label, changed, keyword in cases:
        clock = FakeClock()
        original = info(30, "VS Code", "Code.exe", 301)
        other = info(31, "另一个窗口", "other.exe", 302)
        lookup = {30: original, 31: other}
        tracker = make_tracker([original], lookup, clock)
        tracker.poll_once()
        lookup[30] = changed

        target, reason = tracker.latest(allow_lazy_poll=False)
        check(f"{label}后明确失败", target is None and keyword in reason, reason)
        check(f"{label}后没有改选另一个窗口", target is None
              and target is not other, str(target))

    clock = FakeClock()
    original = info(30, "VS Code", "Code.exe", 301)
    lookup = {30: original}
    tracker = make_tracker([original], lookup, clock)
    tracker.poll_once()
    lookup.clear()
    target, reason = tracker.latest(allow_lazy_poll=False)
    check("窗口关闭后明确失败", target is None and "关闭" in reason, reason)

    clock = FakeClock()
    original = info(30, "VS Code", "Code.exe", 301)
    renamed = info(30, "另一个文件 - Visual Studio Code", "Code.exe", 301)
    lookup = {30: renamed}
    tracker = make_tracker([original], lookup, clock)
    tracker.poll_once()
    target, reason = tracker.latest(allow_lazy_poll=False)
    check("只换标题不算换目标", target is not None and reason == "",
          f"{target.title if target else ''} / {reason}")
    check("返回的是当前标题（prepare 会固定它）",
          target is not None and target.title == renamed.title)


# ----------------------------------------------------------------------
# 五、没有可用目标
# ----------------------------------------------------------------------
def test_no_target() -> None:
    print("\n[五] 没有可用目标时明确说明")

    clock = FakeClock()
    jia = info(40, "JiaAgent 佳 - 个人 - Microsoft​ Edge", "msedge.exe", 401)
    tracker = make_tracker([jia], {40: jia}, clock)
    tracker.poll_once()
    target, reason = tracker.latest(allow_lazy_poll=False)
    check("只有佳自己时不记录目标", target is None, str(target))
    check("原因明确", "还没有记录" in reason, reason)

    clock = FakeClock()
    tracker = make_tracker([], {}, clock)
    target, reason = tracker.latest(allow_lazy_poll=False)
    check("完全没有记录时明确失败", target is None and "还没有记录" in reason, reason)


# ----------------------------------------------------------------------
# 六、后台线程生命周期
# ----------------------------------------------------------------------
def test_thread_lifecycle() -> None:
    print("\n[六] 后台线程能启动和停止")

    clock = FakeClock()
    vs = info(50, "VS Code", "Code.exe", 501)
    tracker = make_tracker([vs], {50: vs}, clock)
    tracker.start()
    check("启动后线程在跑", tracker.is_running is True)
    tracker.start()   # 重复启动安全
    time.sleep(0.05)
    target, reason = tracker.latest(allow_lazy_poll=False)
    check("启动后能拿到目标", target is not None and target.hwnd == 50, reason)
    tracker.stop()
    check("停止后线程退出", tracker.is_running is False)
    tracker.stop()    # 重复停止安全
    check("重复停止不会出错", True)


def main() -> int:
    setup_logging("ERROR")
    print("=" * 70)
    print("最近使用的非佳窗口追踪测试（视觉感知 · 窗口范围）")
    print("=" * 70)

    test_jia_filter()
    test_tracking()
    test_expiry()
    test_invalid_target()
    test_no_target()
    test_thread_lifecycle()

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 70)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for note, _, detail in failed:
        print(f"  [失败] {note}" + (f"　{detail}" if detail else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
