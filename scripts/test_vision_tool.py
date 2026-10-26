"""inspect_screen 工具层测试（视觉感知 · 窗口范围）。

不联网、不真截图、不动鼠标键盘。
用假窗口、假截图、假视觉客户端，重点验证：

  - prepare 在用户确认前固定目标，不截图
  - auto 选最近的非佳窗口；佳自己的页面不会成为目标
  - window 指定、歧义、没有目标、最小化、关闭、无效 HWND
  - desktop 仍然抓整个桌面
  - 确认之后目标变化 / 捕获失败 -> 中止，绝不改抓别的窗口或整个桌面
  - 成功时 output 是完整分析，persist_text 是安全摘要
  - 不写数据库、不落盘

用法：
    .venv\\Scripts\\python.exe scripts\\test_vision_tool.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from app.errors import VisionError  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.tools import vision_tools  # noqa: E402
from app.tools.base import ToolResult  # noqa: E402
from app.vision import observation as observation_mod  # noqa: E402
from app.vision import wincapture  # noqa: E402
from app.vision.client import VisionClient  # noqa: E402
from app.vision.model import ScreenMetrics, Snapshot, WindowInfo  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB = DATA / "jiaagent.db"
RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


# ----------------------------------------------------------------------
# 假对象
# ----------------------------------------------------------------------
class FakeClient(VisionClient):
    provider = "deepseek"
    model = "deepseek-chat"

    def __init__(self, reply: str = "", raise_exc: Exception | None = None) -> None:
        self.reply = reply
        self.raise_exc = raise_exc
        self.analyzed = 0
        self.repaired = 0
        self.prompts: list[str] = []

    async def analyze(self, image, prompt: str) -> str:
        self.analyzed += 1
        self.prompts.append(prompt)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.reply

    async def repair(self, previous: str) -> str:
        self.repaired += 1
        return ""


class FakeProvider:
    name = "deepseek"
    label = "DeepSeek"
    model = "deepseek-chat"


class FakeAppConfig:
    def provider(self, name=None):
        return FakeProvider()


class FakeToolsConfig:
    def __init__(self, enabled: bool = True, **overrides) -> None:
        values = dict(
            enabled=enabled,
            preview_max_side=1600,
            max_windows=200,
            provider="",
            max_side=320,
            image_format="jpeg",
            jpeg_quality=85,
            max_tokens=2000,
            max_elements=5,
            capture_timeout_seconds=1.0,
            focus_ttl_seconds=120.0,
            focus_poll_seconds=1.0,
            jia_title_patterns=[
                "JiaAgent 佳",
                "localhost:8000",
                "127.0.0.1:8000",
            ],
            jia_process_names=[],
            max_black_ratio=0.98,
            max_uniform_ratio=0.995,
            min_distinct_colors=3,
            validate_sample_side=64,
        )
        values.update(overrides)
        self.vision = SimpleNamespace(**values)


class FakeTracker:
    def __init__(self, target=None, reason: str = "没有可用的最近窗口") -> None:
        self.target = target
        self.reason = reason

    def latest(self, **_kwargs):
        return self.target, self.reason


def win_info(
    hwnd: int = 1,
    title: str = "记事本",
    process: str = "notepad.exe",
    pid: int = 42,
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


def fake_snapshot(foreground: WindowInfo | None = None) -> Snapshot:
    image = Image.new("RGB", (32, 20), (10, 10, 10))
    return Snapshot(
        image=image,
        preview=image,
        metrics=ScreenMetrics(virtual_width=32, virtual_height=20),
        window=foreground or win_info(),
        taken_at="2026-10-18T00:00:00",
        original_size=(32, 20),
        preview_size=(32, 20),
    )


def fake_window_frame(info: WindowInfo) -> wincapture.WindowFrame:
    image = Image.new("RGB", (32, 20), (10, 10, 10))
    snap = Snapshot(
        image=image,
        preview=image,
        metrics=ScreenMetrics(virtual_width=32, virtual_height=20),
        window=info,
        taken_at="2026-10-18T00:00:00",
        original_size=(32, 20),
        preview_size=(32, 20),
    )
    return wincapture.WindowFrame(
        image=image,
        preview=image,
        snapshot=snap,
        backend="wgc-window",
        content_size=(32, 20),
        stats=wincapture.ContentStats(
            width=32, height=20, black_ratio=0.0,
            uniform_ratio=0.2, distinct_colors=8,
        ),
    )


class Harness:
    """把工具的依赖全部换成假对象，并且**一定能还原**。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        reply: str = "",
        raise_exc: Exception | None = None,
        windows: list[WindowInfo] | None = None,
        lookup: dict[int, WindowInfo] | None = None,
        focus_target=None,
        focus_reason: str = "没有可用的最近窗口",
        available: bool = True,
        desktop_exc: Exception | None = None,
        window_exc: Exception | None = None,
        foreground: WindowInfo | None = None,
        config_overrides: dict | None = None,
        post_capture_lookup: dict[int, WindowInfo] | None = None,
    ) -> None:
        self.enabled = enabled
        self.reply = reply
        self.raise_exc = raise_exc
        self.windows = list(windows or [])
        self.lookup = dict(lookup or {})
        self.post_capture_lookup = dict(post_capture_lookup or {})
        self.focus_target = focus_target
        self.focus_reason = focus_reason
        self.available = available
        self.desktop_exc = desktop_exc
        self.window_exc = window_exc
        self.foreground = foreground or win_info()
        self.config_overrides = dict(config_overrides or {})
        self.calls = {"desktop": 0, "window": 0}
        self.list_calls = 0
        self.window_calls: list[tuple[int, dict]] = []
        self.client: FakeClient | None = None
        self._originals: dict[str, object] = {}

    # -- patched functions ------------------------------------------------
    def _desktop(self, *_args, **_kwargs):
        self.calls["desktop"] += 1
        if self.desktop_exc is not None:
            raise self.desktop_exc
        return fake_snapshot(self.foreground)

    def _window(self, hwnd, **kwargs):
        self.calls["window"] += 1
        self.window_calls.append((int(hwnd), dict(kwargs)))
        if self.window_exc is not None:
            raise self.window_exc
        frame = fake_window_frame(
            self.lookup.get(int(hwnd)) or win_info(hwnd=int(hwnd))
        )
        # 模拟“捕获期间目标又变了”：工具捕获后还会再读一次窗口状态。
        if self.post_capture_lookup:
            self.lookup.update(self.post_capture_lookup)
        return frame

    def _lookup(self, hwnd: int):
        return self.lookup.get(int(hwnd))

    def _list_windows(self, **_kwargs):
        self.list_calls += 1
        return list(self.windows)

    def _build_client(self, _cfg):
        self.client = FakeClient(self.reply, self.raise_exc)
        return self.client

    def _tools_config(self):
        return FakeToolsConfig(self.enabled, **self.config_overrides)

    # -- context manager ---------------------------------------------------
    def __enter__(self):
        self._originals = {
            "get_tools_config": vision_tools.get_tools_config,
            "get_config": vision_tools.get_config,
            "build_client": vision_tools.build_client,
            "snapshot": vision_tools.capture.snapshot,
            "capture_window": vision_tools.wincapture.capture_window,
            "is_available": vision_tools.wincapture.is_available,
            "get_window_info": vision_tools.window_mod.get_window_info,
            "list_windows": vision_tools.window_mod.list_windows,
            "get_foreground_window": vision_tools.window_mod.get_foreground_window,
            "tracker": vision_tools.focus.tracker,
        }

        vision_tools.get_tools_config = self._tools_config
        vision_tools.get_config = lambda: FakeAppConfig()
        vision_tools.build_client = self._build_client
        vision_tools.capture.snapshot = self._desktop
        vision_tools.wincapture.capture_window = self._window
        vision_tools.wincapture.is_available = lambda: (
            self.available,
            "" if self.available else "模拟：capturekit 不可用",
        )
        vision_tools.window_mod.get_window_info = self._lookup
        vision_tools.window_mod.list_windows = self._list_windows
        vision_tools.window_mod.get_foreground_window = lambda: self.foreground
        vision_tools.focus.tracker = FakeTracker(
            self.focus_target, self.focus_reason
        )
        return self

    def __exit__(self, *_args):
        vision_tools.get_tools_config = self._originals["get_tools_config"]
        vision_tools.get_config = self._originals["get_config"]
        vision_tools.build_client = self._originals["build_client"]
        vision_tools.capture.snapshot = self._originals["snapshot"]
        vision_tools.wincapture.capture_window = self._originals["capture_window"]
        vision_tools.wincapture.is_available = self._originals["is_available"]
        vision_tools.window_mod.get_window_info = self._originals["get_window_info"]
        vision_tools.window_mod.list_windows = self._originals["list_windows"]
        vision_tools.window_mod.get_foreground_window = self._originals[
            "get_foreground_window"
        ]
        vision_tools.focus.tracker = self._originals["tracker"]
        return False

    # -- helpers -----------------------------------------------------------
    def tool(self) -> vision_tools.InspectScreenTool:
        return vision_tools.InspectScreenTool()

    def prepare(self, args: dict):
        return asyncio.run(self.tool().prepare(args))

    def describe(self, args: dict) -> str:
        return self.tool().describe_action(args)

    def execute(self, args: dict, injected: dict):
        return asyncio.run(self.tool().execute(args, injected=injected))


def valid_reply(marker: str = "") -> str:
    return json.dumps({
        "supported": True,
        "description": "一个记事本窗口" + marker,
        "elements": [{"name": "编辑区", "type": "input", "x": 0.5, "y": 0.5,
                      "confidence": 0.9}],
        "candidates": [{"label": "点击编辑区", "x": 0.5, "y": 0.5,
                        "confidence": 0.8}],
        "uncertainty": [],
    }, ensure_ascii=False)


def db_counts() -> tuple[int, int]:
    if not DB.exists():
        return (0, 0)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        messages = con.execute("select count(*) from messages").fetchone()[0]
        steps = con.execute("select count(*) from task_steps").fetchone()[0]
        return (int(messages), int(steps))
    finally:
        con.close()


def data_files() -> set[str]:
    return {item.name for item in DATA.iterdir()} if DATA.exists() else set()


def pinned_window(info: WindowInfo) -> dict:
    return {
        "_scope": "window",
        "_vision_model": "DeepSeek / deepseek-chat",
        "_target_hwnd": info.hwnd,
        "_target_pid": info.pid,
        "_target_process": info.process,
        "_target_title": info.title,
    }


# ----------------------------------------------------------------------
# 用例
# ----------------------------------------------------------------------
def test_basic_contract() -> None:
    print("\n[一] 工具契约")
    tool = vision_tools.InspectScreenTool()
    props = tool.parameters.get("properties") or {}
    check("名字是 inspect_screen", tool.name == "inspect_screen")
    check("每次都必须用户确认", tool.requires_confirmation is True,
          str(tool.requires_confirmation))
    check("没有必填参数", tool.parameters.get("required") == [])
    check("支持 question 参数", "question" in props)
    check("支持 scope 参数", "scope" in props)
    check("支持 window 参数", "window" in props)
    check("scope 只允许 auto/window/desktop",
          props.get("scope", {}).get("enum") == ["auto", "window", "desktop"],
          str(props.get("scope", {}).get("enum")))


def test_describe_action() -> None:
    print("\n[二] 确认卡片写清楚范围、目标和隐私风险")

    with Harness(foreground=win_info(1, "记事本", "notepad.exe")) as h:
        text = h.describe({
            "question": "登录按钮在哪",
            "_scope": "desktop",
            "_vision_model": "DeepSeek / deepseek-chat",
        })
        check("桌面范围说明了整个桌面", "整个桌面" in text, text)
        check("桌面范围说明发给哪个模型",
              "发送给 DeepSeek / deepseek-chat" in text, text)
        check("桌面范围提示其他窗口信息",
              "可能包含其他窗口中的信息" in text, text)
        check("桌面范围把前台窗口标成界面状态",
              "当前前台窗口" in text and "不是截取范围" in text, text)
        check("卡片带上了重点问题", "登录按钮在哪" in text)

    with Harness() as h:
        info = win_info(
            7, "JiaAgent MVP 项目架构 - Visual Studio Code", "Code.exe"
        )
        text = h.describe({
            "question": "登录按钮在哪",
            "_scope": "window",
            "_vision_model": "DeepSeek / deepseek-chat",
            "_target_hwnd": info.hwnd,
            "_target_process": info.process,
            "_target_title": info.title,
        })
        check("窗口范围写明具体窗口",
              "JiaAgent MVP 项目架构" in text and "Code.exe" in text, text)
        check("窗口范围写明你指定的窗口", "你指定的应用窗口" in text, text)
        check("窗口范围写明只截这一个窗口",
              "只有这一个应用窗口" in text, text)
        check("窗口范围明确不包括整个桌面",
              "不包括整个桌面" in text, text)
        check("窗口范围提示窗口内仍有敏感信息",
              "窗口内仍可能有敏感信息" in text, text)
        check("窗口范围没有声称将截整个桌面",
              "将截取整个桌面" not in text, text)
        check("窗口范围带上 HWND", "HWND 0x7" in text, text)

        auto_text = h.describe({
            "_scope": "auto",
            "_vision_model": "DeepSeek / deepseek-chat",
            "_target_hwnd": info.hwnd,
            "_target_process": info.process,
            "_target_title": info.title,
        })
        check("auto 范围说明是最近使用的非佳窗口",
              "最近使用的非佳窗口" in auto_text, auto_text)

        unknown = h.describe({"question": "看看"})
        check("没有固定目标时明确说不会截图",
              "尚未确定" in unknown and "不会执行截图" in unknown, unknown)
        check("没有固定目标时绝不声称截整个桌面",
              "将截取整个桌面" not in unknown, unknown)


def test_prepare() -> None:
    print("\n[三] prepare：确认前固定目标，不截图")

    with Harness() as h:
        res = h.prepare({"scope": "desktop"})
        check("desktop 直接就绪", res.blocked == "", res.blocked)
        check("desktop 注入范围", res.injected.get("_scope") == "desktop",
              str(res.injected))
        check("desktop 注入模型标签", bool(res.injected.get("_vision_model")),
              str(res.injected))
        check("desktop 不注入 HWND", "_target_hwnd" not in res.injected)

        res = h.prepare({"scope": "bogus"})
        check("非法 scope 被阻止", bool(res.blocked) and "只能是" in res.blocked,
              res.blocked)

        res = h.prepare({})
        check("默认 auto 没有目标就阻止", bool(res.blocked), res.blocked)
        check("默认 auto 的原因来自 focus", "没有可用的最近窗口" in res.blocked,
              res.blocked)

        res = h.prepare({"scope": "window"})
        check("window 没给 selector 被阻止", bool(res.blocked)
              and "window" in res.blocked, res.blocked)

        check("prepare 没有截图", h.calls == {"desktop": 0, "window": 0},
              str(h.calls))

    with Harness(enabled=False) as h:
        res = h.prepare({"scope": "auto"})
        check("功能关闭时阻止", "没有打开" in res.blocked, res.blocked)

    with Harness(available=False) as h:
        res = h.prepare({"scope": "window", "window": "记事本"})
        check("窗口捕获不可用时阻止",
              "窗口捕获不可用" in res.blocked, res.blocked)

    _test_prepare_window()
    _test_prepare_auto()


def _test_prepare_window() -> None:
    print()
    print("window 范围：唯一命中才固定；歧义/没找到/最小化都阻止")

    with Harness(windows=[win_info(7, "记事本", "notepad.exe")]) as h:
        res = h.prepare({"scope": "window", "window": "记事本"})
        check("唯一命中就固定目标", res.blocked == ""
              and res.injected.get("_scope") == "window", res.blocked)
        check("固定了 HWND", res.injected.get("_target_hwnd") == 7,
              str(res.injected))
        check("固定了进程和标题",
              res.injected.get("_target_process") == "notepad.exe"
              and res.injected.get("_target_title") == "记事本",
              str(res.injected))
        check("prepare 仍然没有截图",
              h.calls == {"desktop": 0, "window": 0}, str(h.calls))

    with Harness(windows=[win_info(1, "记事本 A"), win_info(2, "记事本 B")]) as h:
        res = h.prepare({"scope": "window", "window": "记事本"})
        check("多个匹配被阻止", bool(res.blocked), res.blocked)
        check("歧义原因列出了两个窗口",
              "记事本 A" in res.blocked and "记事本 B" in res.blocked,
              res.blocked)
        check("歧义时明确不会随便挑", "不会随便挑一个" in res.blocked,
              res.blocked)
        check("歧义时不注入目标", not res.injected, str(res.injected))

    with Harness(windows=[win_info(
        9, "JiaAgent MVP 项目架构 - Visual Studio Code", "Code.exe"
    )]) as h:
        res = h.prepare({"scope": "window", "window": "Code.exe"})
        check("明确指定 VS Code 能固定目标", res.blocked == ""
              and res.injected.get("_target_hwnd") == 9, res.blocked)
        check("VS Code 的进程和标题被固定",
              res.injected.get("_target_process") == "Code.exe"
              and "Visual Studio Code" in res.injected.get("_target_title", ""),
              str(res.injected))

    with Harness(windows=[win_info(7, "记事本", "notepad.exe")]) as h:
        res = h.prepare({"scope": "window", "window": "vscode"})
        check("没找到时阻止", "没有找到" in res.blocked, res.blocked)

    with Harness(windows=[win_info(7, "记事本", minimized=True)]) as h:
        res = h.prepare({"scope": "window", "window": "记事本"})
        check("最小化窗口在 prepare 被阻止",
              "最小化" in res.blocked, res.blocked)

    with Harness(windows=[win_info(
        7, "JiaAgent 佳 - 个人 - Microsoft​ Edge", "msedge.exe"
    )]) as h:
        res = h.prepare({"scope": "window", "window": "JiaAgent"})
        check("佳自己的页面不会成为指定的目标",
              "没有找到" in res.blocked, res.blocked)


def _test_prepare_auto() -> None:
    print()
    print("auto 范围：最近的非佳窗口；失效/过期就阻止，不改选")

    target = SimpleNamespace(hwnd=7)
    info = win_info(7, "JiaAgent MVP 项目架构 - Visual Studio Code", "Code.exe")

    with Harness(focus_target=target, lookup={7: info}) as h:
        res = h.prepare({})
        check("auto 固定最近窗口", res.blocked == ""
              and res.injected.get("_scope") == "auto", res.blocked)
        check("auto 固定 HWND/进程/标题",
              res.injected.get("_target_hwnd") == 7
              and res.injected.get("_target_process") == "Code.exe"
              and res.injected.get("_target_title") == info.title,
              str(res.injected))
        check("auto prepare 没有截图",
              h.calls == {"desktop": 0, "window": 0}, str(h.calls))

    with Harness(focus_target=None,
                 focus_reason="最近使用的窗口已经超过 120 秒没有更新") as h:
        res = h.prepare({})
        check("过期目标被阻止", "120" in res.blocked, res.blocked)
        check("过期时不改抓整个桌面",
              "不会改抓" in res.blocked and "整个桌面" in res.blocked,
              res.blocked)

    with Harness(focus_target=target, lookup={}) as h:
        res = h.prepare({})
        check("目标已关闭被阻止", "关闭" in res.blocked, res.blocked)

    with Harness(focus_target=target,
                 lookup={7: win_info(7, "VS Code", "Code.exe", minimized=True)}) as h:
        res = h.prepare({})
        check("目标最小化被阻止", "最小化" in res.blocked, res.blocked)
        check("最小化时不改选", "不会改抓另一个窗口" in res.blocked,
              res.blocked)

    with Harness(focus_target=target,
                 lookup={7: win_info(
                     7, "JiaAgent 佳 - 个人 - Microsoft​ Edge", "msedge.exe"
                 )}) as h:
        res = h.prepare({})
        check("佳自己的页面不能成为默认目标",
              "佳自己" in res.blocked, res.blocked)


def test_success_desktop() -> None:
    print("\n[四] desktop 成功路径：完整内容给模型，安全摘要给数据库")

    marker = "FULL-DESKTOP-MARKER"
    before_counts = db_counts()
    before_files = data_files()
    observation_mod.store.clear()

    with Harness(reply=valid_reply(marker),
                 foreground=win_info(1, "记事本", "notepad.exe")) as h:
        result = h.execute(
            {"question": "按钮在哪", "scope": "desktop"},
            {"_scope": "desktop", "_vision_model": "DeepSeek / deepseek-chat"},
        )
        check("执行成功", result.ok is True, result.as_text()[:80])
        check("只截了一次整桌", h.calls["desktop"] == 1, str(h.calls))
        check("没有走窗口捕获", h.calls["window"] == 0, str(h.calls))
        check("只调用了一次视觉模型",
              h.client is not None and h.client.analyzed == 1)
        check("模型拿到的提示词是整屏截图",
              h.client is not None and "整屏截图" in h.client.prompts[0],
              h.client.prompts[0][:60] if h.client and h.client.prompts else "")
        check("output 是完整分析", marker in result.output)
        check("persist_text 是安全摘要",
              result.persist_text == "查看屏幕：记事本（notepad.exe）",
              result.persist_text)
        check("持久化文本里没有分析内容", marker not in result.persist_text)
        check("as_persist_text 与 persist_text 一致",
              result.as_persist_text() == result.persist_text)
        check("as_text 仍然是完整输出", result.as_text() == result.output)
        check("桌面观察也登记了一条记录",
              observation_mod.store.count() == 1,
              str(observation_mod.store.count()))
        desktop_record = observation_mod.store.all()[0]
        check("桌面记录范围正确", desktop_record.scope == "desktop",
              desktop_record.scope)
        check("桌面观察不能用于定位点击", desktop_record.operable is False)
        check("输出里带观察编号",
              desktop_record.observation_id in result.output)
        check("输出明确说明不会点击",
              "不会产生任何鼠标或键盘操作" in result.output)

    after_counts = db_counts()
    after_files = data_files()
    check("没有新增数据库记录", before_counts == after_counts,
          f"{before_counts} -> {after_counts}")
    check("没有落盘任何文件", before_files == after_files,
          f"新增：{sorted(after_files - before_files)}")


def test_success_window() -> None:
    print("\n[五] window 成功路径：固定 HWND、WGC 捕获、摘要不含画面")

    marker = "FULL-WINDOW-MARKER"
    info = win_info(7, "记事本", "notepad.exe", 42)
    observation_mod.store.clear()

    with Harness(reply=valid_reply(marker), lookup={7: info}) as h:
        result = h.execute(
            {"question": "按钮在哪", "scope": "window", "window": "记事本"},
            pinned_window(info),
        )
        check("执行成功", result.ok is True, result.as_text()[:80])
        check("走了一次窗口捕获", h.calls["window"] == 1, str(h.calls))
        check("没有退回整桌截图", h.calls["desktop"] == 0, str(h.calls))
        check("捕获的是固定 HWND",
              bool(h.window_calls) and h.window_calls[0][0] == 7,
              str(h.window_calls))
        check("模型拿到的提示词是单个窗口",
              h.client is not None
              and "一个应用窗口" in h.client.prompts[0]
              and "不是整个桌面" in h.client.prompts[0],
              h.client.prompts[0][:80] if h.client and h.client.prompts else "")
        check("output 是完整分析", marker in result.output)
        check("persist_text 是窗口安全摘要",
              result.persist_text == "查看屏幕：窗口「记事本」（notepad.exe）",
              result.persist_text)
        check("持久化文本里没有分析内容", marker not in result.persist_text)
        check("窗口观察登记了一条记录",
              observation_mod.store.count() == 1,
              str(observation_mod.store.count()))
        window_record = observation_mod.store.all()[0]
        check("记录绑定了固定 HWND",
              window_record.window is not None
              and window_record.window.hwnd == 7,
              str(window_record.window.hwnd if window_record.window else None))
        check("记录保存了真实窗口矩形",
              window_record.window_rect == info.rect,
              str(window_record.window_rect))
        check("记录保存了元素编号",
              [item.element_id for item in window_record.elements] == ["e1"],
              str([item.element_id for item in window_record.elements]))
        check("输出里带 observation_id 和元素编号",
              window_record.observation_id in result.output
              and "e1" in result.output)
        check("输出没有宣称真的点击了",
              "点击成功" not in result.output and "已点击" not in result.output)
        check("观察记录不进入数据库安全摘要",
              window_record.observation_id not in result.persist_text,
              result.persist_text)


def _expect_window_failure(
    label: str,
    *,
    expected: str,
    injected: dict,
    lookup: dict[int, WindowInfo],
    enabled: bool = True,
    window_exc: Exception | None = None,
    window_attempted: bool = False,
) -> None:
    with Harness(lookup=lookup, enabled=enabled, window_exc=window_exc) as h:
        result = h.execute(
            {"question": "按钮在哪", "scope": "window", "window": "记事本"},
            injected,
        )
        check(label, result.ok is False and expected in result.error,
              result.error[:120])
        if window_attempted:
            check(f"{label}：只尝试了固定窗口一次",
                  h.calls["window"] == 1, str(h.calls))
        else:
            check(f"{label}：没有截图", h.calls["window"] == 0, str(h.calls))
        check(f"{label}：没有改抓整个桌面", h.calls["desktop"] == 0,
              str(h.calls))
        check(f"{label}：没有调用视觉模型",
              h.client is None or h.client.analyzed == 0,
              "0" if h.client is None else str(h.client.analyzed))


def test_execute_refusals() -> None:
    print("\n[六] 确认之后目标变化 / 没有固定目标：中止并重新确认")

    info = win_info(7, "记事本", "notepad.exe", 42)
    pinned = pinned_window(info)

    with Harness(lookup={7: info}) as h:
        result = h.execute({"question": "看看"}, {})
        check("没有经过确认流程就拒绝",
              result.ok is False and "没有经过确认流程" in result.error,
              result.error)
        check("没有经过确认流程时没有截图",
              h.calls == {"desktop": 0, "window": 0}, str(h.calls))

    with Harness(lookup={7: info}) as h:
        bad = dict(pinned)
        bad["_target_hwnd"] = 0
        result = h.execute({"question": "看看"}, bad)
        check("没有固定 HWND 时拒绝",
              result.ok is False and "没有固定" in result.error, result.error)
        check("没有固定 HWND 时没有截图",
              h.calls == {"desktop": 0, "window": 0}, str(h.calls))

    _expect_window_failure(
        "窗口已关闭 -> 中止",
        expected="失效", injected=pinned, lookup={},
    )
    _expect_window_failure(
        "窗口最小化 -> 中止",
        expected="最小化", injected=pinned,
        lookup={7: win_info(7, "记事本", "notepad.exe", 42, minimized=True)},
    )
    _expect_window_failure(
        "HWND 被复用（PID 变化）-> 中止",
        expected="进程", injected=pinned,
        lookup={7: win_info(7, "记事本", "notepad.exe", 99)},
    )
    _expect_window_failure(
        "进程名变化 -> 中止",
        expected="进程", injected=pinned,
        lookup={7: win_info(7, "记事本", "evil.exe", 42)},
    )
    _expect_window_failure(
        "标题变化 -> 中止",
        expected="标题", injected=pinned,
        lookup={7: win_info(7, "另一个窗口", "notepad.exe", 42)},
    )
    _expect_window_failure(
        "目标变成佳自己的界面 -> 中止",
        expected="佳自己",
        injected=pinned,
        lookup={7: win_info(7, "JiaAgent 佳", "msedge.exe", 42)},
    )

    with Harness(lookup={7: info}, enabled=False) as h:
        result = h.execute({"question": "看看"}, pinned)
        check("功能关闭时拒绝",
              result.ok is False and "没有打开" in result.error, result.error)
        check("功能关闭时没有截图",
              h.calls == {"desktop": 0, "window": 0}, str(h.calls))

    # 捕获成功之后、分析之前目标又变了：画面必须丢弃，不能上传。
    changed = win_info(7, "另一个窗口", "notepad.exe", 42)
    with Harness(lookup={7: info}, post_capture_lookup={7: changed}) as h:
        result = h.execute({"question": "看看"}, pinned)
        check("捕获期间标题变化 -> 丢弃画面",
              result.ok is False and "标题" in result.error, result.error)
        check("捕获期间变化时只尝试了固定窗口一次",
              h.calls["window"] == 1, str(h.calls))
        check("捕获期间变化时没有退回整桌",
              h.calls["desktop"] == 0, str(h.calls))
        check("捕获期间变化时没有调用视觉模型",
              h.client is None or h.client.analyzed == 0,
              "0" if h.client is None else str(h.client.analyzed))


def test_capture_failures() -> None:
    print("\n[七] 捕获失败：真实错误，不退回整桌")

    info = win_info(7, "记事本", "notepad.exe", 42)
    pinned = pinned_window(info)

    _expect_window_failure(
        "窗口捕获失败 -> 明确报错",
        expected="模拟全黑", injected=pinned, lookup={7: info},
        window_exc=VisionError("窗口捕获失败", detail="模拟全黑"),
        window_attempted=True,
    )

    with Harness(lookup={7: info},
                 window_exc=VisionError("窗口捕获失败", detail="模拟全黑")) as h:
        result = h.execute({"question": "看看"}, pinned)
        check("失败信息说明不会退回整桌",
              "不会退回" in result.error, result.error[:120])
        check("失败落库摘要不含细节",
              result.as_persist_error() == "窗口捕获失败：窗口捕获失败",
              result.as_persist_error())

    with Harness(desktop_exc=VisionError("截屏失败", detail="模拟屏幕被锁")) as h:
        result = h.execute(
            {"question": "看看"},
            {"_scope": "desktop", "_vision_model": "DeepSeek / deepseek-chat"},
        )
        check("desktop 截图失败时明确报错",
              result.ok is False and "模拟屏幕被锁" in result.error, result.error)
        check("desktop 失败时没有走窗口捕获",
              h.calls["window"] == 0, str(h.calls))

    marker = "RAW-MODEL-OUTPUT-MARKER"
    with Harness(reply=marker + " 这不是 JSON", lookup={7: info}) as h:
        result = h.execute({"question": "看看"}, pinned)
        check("模型坏 JSON -> 执行失败", result.ok is False)
        check("错误里说明了无法解析", "无法解析" in result.error,
              result.error[:120])
        check("错误里没有模型原始输出", marker not in result.error,
              result.error[:120])
        safe = result.as_persist_error()
        check("落库摘要没有原始输出", marker not in safe, safe)
        check("落库摘要保留了错误类型和模型名",
              "视觉分析失败" in safe and "deepseek-chat" in safe, safe)


def test_observation_failure() -> None:
    print("\n[七点五] 观察登记失败：如实失败，不把结果当成可信观察")

    info = win_info(7, "记事本", "notepad.exe", 42)
    observation_mod.store.clear()
    original = observation_mod.store.create_from_capture

    def _boom(*_args, **_kwargs):
        raise RuntimeError("模拟登记失败")

    try:
        observation_mod.store.create_from_capture = _boom
        with Harness(reply=valid_reply("SHOULD-NOT-BE-TRUSTED"),
                     lookup={7: info}) as h:
            result = h.execute(
                {"question": "按钮在哪", "scope": "window", "window": "记事本"},
                pinned_window(info),
            )
            check("登记失败时工具如实失败",
                  result.ok is False and "观察记录建立失败" in result.error,
                  result.error[:120])
            check("登记失败时没有登记任何记录",
                  observation_mod.store.count() == 0,
                  str(observation_mod.store.count()))
            check("安全摘要说明结果不能作为可信观察",
                  "不能作为可信观察" in result.as_persist_error(),
                  result.as_persist_error())
    finally:
        observation_mod.store.create_from_capture = original


def test_missing_injected() -> None:
    print("\n[八] 确认前固定的目标不完整：安全中止，不临时找目标")

    info = win_info(7, "记事本", "notepad.exe", 42)
    model = "DeepSeek / deepseek-chat"

    cases = [
        ("桌面范围缺少模型信息", {"_scope": "desktop"}, "模型"),
        (
            "auto 缺少固定 HWND",
            {"_scope": "auto", "_vision_model": model},
            "没有固定",
        ),
        (
            "window 缺少固定 HWND",
            {"_scope": "window", "_vision_model": model},
            "没有固定",
        ),
        (
            "window 缺少窗口身份信息",
            {
                "_scope": "window",
                "_vision_model": model,
                "_target_hwnd": 7,
                "_target_pid": 0,
                "_target_process": "",
                "_target_title": "",
            },
            "身份",
        ),
        (
            "auto 缺少模型信息",
            {
                "_scope": "auto",
                "_target_hwnd": 7,
                "_target_pid": 42,
                "_target_process": "notepad.exe",
                "_target_title": "记事本",
            },
            "模型",
        ),
    ]

    for label, injected, keyword in cases:
        with Harness(lookup={7: info}) as h:
            result = h.execute(
                {"question": "看看", "scope": "window", "window": "记事本"},
                injected,
            )
            check(f"{label} -> 中止",
                  result.ok is False and keyword in result.error,
                  result.error[:120])
            check(f"{label}：没有截图",
                  h.calls == {"desktop": 0, "window": 0}, str(h.calls))
            check(f"{label}：没有临时找窗口",
                  h.list_calls == 0, str(h.list_calls))
            check(f"{label}：没有调用视觉模型",
                  h.client is None or h.client.analyzed == 0,
                  "0" if h.client is None else str(h.client.analyzed))


def test_injected_authority() -> None:
    print("\n[九] 执行以确认前固定的目标为准，模型参数不能覆盖")

    info = win_info(7, "记事本", "notepad.exe", 42)
    pinned = pinned_window(info)

    with Harness(lookup={7: info}, reply=valid_reply("WINDOW")) as h:
        result = h.execute(
            {"question": "看看", "scope": "desktop", "window": "别的窗口"},
            pinned,
        )
        check("模型说 desktop，但固定的是 window -> 仍走窗口",
              result.ok is True and h.calls["window"] == 1
              and h.calls["desktop"] == 0, str(h.calls))
        check("捕获的是固定的 HWND",
              bool(h.window_calls) and h.window_calls[0][0] == 7,
              str(h.window_calls))

    desktop_pin = {
        "_scope": "desktop",
        "_vision_model": "DeepSeek / deepseek-chat",
    }
    with Harness(reply=valid_reply("DESKTOP")) as h:
        result = h.execute(
            {"question": "看看", "scope": "window", "window": "记事本"},
            desktop_pin,
        )
        check("模型说 window，但固定的是 desktop -> 仍走整桌",
              result.ok is True and h.calls["desktop"] == 1
              and h.calls["window"] == 0, str(h.calls))

    with Harness(lookup={7: info}) as h:
        result = h.execute(
            {
                "question": "看看",
                "scope": "window",
                "window": "记事本",
                "_target_hwnd": 999,
            },
            pinned,
        )
        check("模型伪造 _target_hwnd 会被参数校验拒绝",
              result.ok is False and "不认识的参数" in result.error,
              result.error[:120])
        check("伪造参数时没有截图",
              h.calls == {"desktop": 0, "window": 0}, str(h.calls))
        check("伪造参数时没有调用模型", h.client is None)


def test_post_capture_changes() -> None:
    print("\n[十] 捕获成功后目标又变了：丢弃画面，不分析、不退回整桌")

    info = win_info(7, "记事本", "notepad.exe", 42)
    pinned = pinned_window(info)

    cases = [
        ("窗口关闭", {7: None}, "失效"),
        (
            "HWND 被复用（PID 变化）",
            {7: win_info(7, "记事本", "notepad.exe", 99)},
            "进程",
        ),
        (
            "进程名变化",
            {7: win_info(7, "记事本", "evil.exe", 42)},
            "进程",
        ),
        (
            "窗口最小化",
            {7: win_info(7, "记事本", "notepad.exe", 42, minimized=True)},
            "最小化",
        ),
        (
            "标题/身份变化",
            {7: win_info(7, "另一个窗口", "notepad.exe", 42)},
            "标题",
        ),
    ]

    for label, after, keyword in cases:
        with Harness(lookup={7: info}, post_capture_lookup=after) as h:
            result = h.execute({"question": "看看"}, pinned)
            check(f"{label} -> 丢弃画面",
                  result.ok is False and keyword in result.error,
                  result.error[:120])
            check(f"{label}：只尝试了固定窗口一次",
                  h.calls["window"] == 1, str(h.calls))
            check(f"{label}：没有退回整桌",
                  h.calls["desktop"] == 0, str(h.calls))
            check(f"{label}：没有调用视觉模型",
                  h.client is None or h.client.analyzed == 0,
                  "0" if h.client is None else str(h.client.analyzed))


def test_toolresult_compat() -> None:
    print("\n[十一] 没设置持久化字段时，行为与以前完全一致")
    ok_result = ToolResult(ok=True, output="普通输出")
    check("成功：as_persist_text == as_text",
          ok_result.as_persist_text() == ok_result.as_text() == "普通输出")

    empty_result = ToolResult(ok=True)
    check("成功但没输出：仍是原来的兜底文案",
          empty_result.as_persist_text() == "（执行完成，没有输出）")

    err_result = ToolResult(ok=False, error="普通错误")
    check("失败：as_persist_error 等于 error", err_result.as_persist_error() == "普通错误")

    err_output = ToolResult(ok=False, output="只有 output")
    check("失败且 error 为空：仍退回 output（与旧逻辑一致）",
          err_output.as_persist_error() == "只有 output")

    empty_err = ToolResult(ok=False)
    check("失败且两者都空：仍是「未知错误」",
          empty_err.as_persist_error() == "未知错误")


def main() -> int:
    setup_logging("ERROR")
    print("=" * 70)
    print("inspect_screen 工具层测试（视觉感知 · 窗口范围）")
    print("=" * 70)
    test_basic_contract()
    test_describe_action()
    test_prepare()
    test_success_desktop()
    test_success_window()
    test_execute_refusals()
    test_capture_failures()
    test_observation_failure()
    test_missing_injected()
    test_injected_authority()
    test_post_capture_changes()
    test_toolresult_compat()

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 70)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for note, _, detail in failed:
        print(f"  [失败] {note}" + (f"　{detail}" if detail else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
