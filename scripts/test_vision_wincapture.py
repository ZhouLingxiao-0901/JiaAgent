"""按 HWND 捕获窗口的封装测试（Windows Graphics Capture）。

**完全不抓真实窗口**：用一个假的 capturekit 模块和内存里的 numpy 帧。
覆盖：
  - 有效帧 -> 成功，预览尺寸正确
  - 全黑 / 可疑单色 / 颜色过少 / 格式错误 / 画面太小 -> 明确失败
  - 无效 HWND、不可见、最小化、禁止捕获 -> 在打开捕获会话前失败
  - 超时、窗口关闭 -> 明确失败
  - 捕获期间进程变化 -> 明确失败
  - with 一定会释放捕获会话
  - 失败时绝不退回整桌截图

用法：
    .venv\\Scripts\\python.exe scripts\\test_vision_wincapture.py
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.errors import VisionError  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.vision import wincapture  # noqa: E402
from app.vision.model import ScreenMetrics, WindowInfo  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


# ----------------------------------------------------------------------
# 假 capturekit
# ----------------------------------------------------------------------
class CaptureError(Exception):
    pass


class TargetNotFoundError(CaptureError):
    pass


class TargetLostError(CaptureError):
    pass


class WindowClosedError(TargetLostError):
    pass


class CaptureTimeoutError(CaptureError):
    pass


class AccessDeniedError(CaptureError):
    pass


class CapabilityUnavailableError(CaptureError):
    pass


class FakeRect:
    def __init__(self, width: int = 640, height: int = 480) -> None:
        self.x = 0
        self.y = 0
        self.width = width
        self.height = height

    def __repr__(self) -> str:
        return f"FakeRect(0, 0, {self.width}, {self.height})"


class FakeWindow:
    def __init__(
        self,
        handle: int = 7,
        *,
        is_visible: bool = True,
        is_minimized: bool = False,
        capture_excluded: bool = False,
        width: int = 640,
        height: int = 480,
    ) -> None:
        self.handle = handle
        self.title = "记事本"
        self.class_name = "Notepad"
        self.pid = 42
        self.process_name = "notepad.exe"
        self.rect = FakeRect(width, height)
        self.is_visible = is_visible
        self.is_minimized = is_minimized
        self.capture_excluded = capture_excluded


class FakeCapability:
    """模拟 capturekit.Capability：支持时 bool 为 True。"""

    def __init__(self, ok: bool = True, reason: str = "") -> None:
        self.ok = ok
        self.reason = reason

    def __bool__(self) -> bool:
        return self.ok


class FakeWindowSource:
    def __init__(self, window, **kwargs) -> None:
        self.window = window
        self.kwargs = kwargs


class FakeScreenCapture:
    def __init__(self, source) -> None:
        self.source = source
        self.closed = False
        self.backend_name = "wgc-window"
        STATE.last_capture = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True
        return False

    def grab(self, timeout: float = 2.0):
        if STATE.exc is not None:
            raise STATE.exc
        return STATE.frame


class State:
    def __init__(self) -> None:
        self.window = None
        self.frame = None
        self.exc = None
        self.last_capture = None


STATE = State()


def fake_capturekit():
    module = types.ModuleType("capturekit")
    module.__version__ = "0.1.0"
    module.WindowSource = FakeWindowSource
    module.ScreenCapture = FakeScreenCapture

    def get_window(handle: int):
        if STATE.window is None:
            raise TargetNotFoundError(f"window {handle:#x} does not exist")
        return STATE.window

    module.get_window = get_window
    for cls in (
        TargetNotFoundError,
        TargetLostError,
        WindowClosedError,
        CaptureTimeoutError,
        AccessDeniedError,
        CapabilityUnavailableError,
    ):
        setattr(module, cls.__name__, cls)
    return module


def install_fake():
    original = sys.modules.get("capturekit")
    sys.modules["capturekit"] = fake_capturekit()
    return original


def restore_fake(original) -> None:
    if original is None:
        sys.modules.pop("capturekit", None)
    else:
        sys.modules["capturekit"] = original


# ----------------------------------------------------------------------
# 假画面 / 假窗口信息
# ----------------------------------------------------------------------
def bgra(width: int = 64, height: int = 48, mode: str = "gradient") -> np.ndarray:
    array = np.zeros((height, width, 4), dtype=np.uint8)
    array[:, :, 3] = 255
    if mode == "gradient":
        x = np.arange(width, dtype=np.uint8)
        array[:, :, 2] = x[None, :]           # R
        array[:, :, 1] = x[::-1][None, :]     # G
        array[:, :, 0] = 128                  # B
    elif mode == "uniform":
        array[:, :, :3] = 128
    elif mode == "black":
        array[:, :, :3] = 0
    return array


def make_frame(mode: str = "gradient", *, width: int = 64, height: int = 48):
    data = bgra(width, height, mode)
    return SimpleNamespace(
        data=data, content_size=(width, height), timestamp_ns=0, index=0
    )


def win_info(
    hwnd: int = 7,
    *,
    pid: int = 42,
    process: str = "notepad.exe",
    title: str = "记事本",
    visible: bool = True,
    minimized: bool = False,
    rect: tuple[int, int, int, int] = (0, 0, 640, 480),
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


def data_files() -> set[str]:
    return {item.name for item in DATA_DIR.iterdir()} if DATA_DIR.exists() else set()


def run_capture(
    infos,
    *,
    frame_obj=None,
    ck_window=None,
    exc: Exception | None = None,
    max_side: int = 32,
):
    """在受控环境里跑一次 capture_window，返回 (结果或异常, 状态)。"""
    original_lookup = wincapture.window_mod.get_window_info
    original_metrics = wincapture.dpi.screen_metrics
    original_grab_screen = wincapture.capture.grab_screen
    desktop_called = {"value": False}

    sequence = list(infos)

    def lookup(_hwnd: int):
        if len(sequence) > 1:
            return sequence.pop(0)
        return sequence[0] if sequence else None

    def desktop_boom(*_args, **_kwargs):
        desktop_called["value"] = True
        raise AssertionError("窗口捕获失败时不应退回整桌截图")

    wincapture.window_mod.get_window_info = lookup
    wincapture.dpi.screen_metrics = lambda: ScreenMetrics(
        virtual_width=640, virtual_height=480
    )
    wincapture.capture.grab_screen = desktop_boom

    STATE.window = ck_window if ck_window is not None else FakeWindow()
    STATE.frame = frame_obj
    STATE.exc = exc
    STATE.last_capture = None

    original_module = install_fake()
    try:
        result = wincapture.capture_window(7, timeout=0.5, max_side=max_side,
                                           sample_side=64)
        error = None
    except VisionError as caught:
        result = None
        error = caught
    finally:
        restore_fake(original_module)
        wincapture.window_mod.get_window_info = original_lookup
        wincapture.dpi.screen_metrics = original_metrics
        wincapture.capture.grab_screen = original_grab_screen

    return result, error, desktop_called["value"]


# ----------------------------------------------------------------------
# 一、成功路径
# ----------------------------------------------------------------------
def test_success() -> None:
    print("\n[一] 有效画面")

    before = data_files()
    result, error, desktop = run_capture([win_info()], frame_obj=make_frame())
    after = data_files()

    check("捕获成功", error is None and result is not None,
          error.message if error else "")
    if result is None:
        return
    check("图像尺寸正确", result.image.size == (64, 48), str(result.image.size))
    check("预览不超过上限", max(result.preview.size) <= 32, str(result.preview.size))
    check("backend 是 WGC 窗口", result.backend == "wgc-window", result.backend)
    check("content_size 正确", result.content_size == (64, 48), str(result.content_size))
    check("颜色统计合理", result.stats.distinct_colors > 3
          and result.stats.black_ratio < 0.5,
          str(result.stats.to_dict()))
    check("没有退回整桌截图", desktop is False)
    check("没有落盘文件", before == after, f"新增：{sorted(after - before)}")
    check("捕获会话被关闭", STATE.last_capture is not None
          and STATE.last_capture.closed is True)


# ----------------------------------------------------------------------
# 二、画面无效
# ----------------------------------------------------------------------
def test_invalid_content() -> None:
    print("\n[二] 全黑 / 单色 / 格式错误都不算成功")

    _, error, _ = run_capture([win_info()], frame_obj=make_frame("black"))
    check("全黑画面失败", error is not None and "全黑" in error.message,
          error.message if error else "")
    check("全黑时也释放了捕获会话",
          STATE.last_capture is not None and STATE.last_capture.closed is True)

    _, error, _ = run_capture([win_info()], frame_obj=make_frame("uniform"))
    check("可疑单色失败", error is not None and "单色" in error.message,
          error.message if error else "")

    broken = SimpleNamespace(data=np.zeros((10, 10), dtype=np.uint8),
                             content_size=(10, 10))
    _, error, _ = run_capture([win_info()], frame_obj=broken)
    check("画面格式不对失败", error is not None and "格式" in error.message,
          error.message if error else "")

    tiny = make_frame(width=4, height=4)
    _, error, _ = run_capture([win_info()], frame_obj=tiny)
    check("画面太小失败", error is not None and "太小" in error.message,
          error.message if error else "")

    _, error, _ = run_capture([win_info()], frame_obj=None)
    check("没有返回画面失败", error is not None and "没有返回" in error.message,
          error.message if error else "")


# ----------------------------------------------------------------------
# 三、窗口状态不对
# ----------------------------------------------------------------------
def test_bad_window() -> None:
    print("\n[三] 无效 / 不可见 / 最小化 / 禁止捕获")

    _, error, _ = run_capture([None], frame_obj=make_frame())
    check("无效 HWND 失败", error is not None and "失效" in error.message,
          error.message if error else "")
    check("无效 HWND 不会打开捕获会话", STATE.last_capture is None)

    _, error, _ = run_capture([win_info(visible=False)], frame_obj=make_frame())
    check("不可见窗口失败", error is not None and "不可见" in error.message,
          error.message if error else "")

    _, error, _ = run_capture([win_info(minimized=True)], frame_obj=make_frame())
    check("最小化窗口失败", error is not None and "最小化" in error.message,
          error.message if error else "")
    check("最小化时不会打开捕获会话", STATE.last_capture is None)

    _, error, _ = run_capture(
        [win_info()], frame_obj=make_frame(),
        ck_window=FakeWindow(capture_excluded=True),
    )
    check("禁止捕获的窗口失败", error is not None and "不允许" in error.message,
          error.message if error else "")
    check("禁止捕获时不会打开捕获会话", STATE.last_capture is None)


# ----------------------------------------------------------------------
# 四、捕获过程出错
# ----------------------------------------------------------------------
def test_capture_errors() -> None:
    print("\n[四] 超时 / 关闭 / 捕获期间身份变化")

    _, error, desktop = run_capture(
        [win_info()], frame_obj=make_frame(),
        exc=CaptureTimeoutError("no frame within 0.5s"),
    )
    check("超时失败", error is not None and "限定时间" in error.message,
          error.message if error else "")
    check("失败时没有退回整桌截图", desktop is False)
    check("超时后会话被关闭",
          STATE.last_capture is not None and STATE.last_capture.closed is True)

    _, error, _ = run_capture(
        [win_info()], frame_obj=make_frame(),
        exc=WindowClosedError("closed"),
    )
    check("窗口关闭失败", error is not None and "关闭" in error.message,
          error.message if error else "")

    before = win_info(pid=42, process="notepad.exe")
    after = win_info(pid=999, process="evil.exe")
    _, error, _ = run_capture([before, after], frame_obj=make_frame())
    check("捕获期间进程变化失败", error is not None and "变化" in error.message,
          error.message if error else "")


# ----------------------------------------------------------------------
# 五、纯统计函数
# ----------------------------------------------------------------------
def test_stats() -> None:
    print("\n[五] 画面统计（纯函数）")

    from PIL import Image

    black = Image.new("RGB", (32, 32), (0, 0, 0))
    stats = wincapture.measure_content(black, sample_side=16)
    check("全黑图黑色比例是 100%", stats.black_ratio == 1.0, str(stats.black_ratio))
    check("全黑图只有 1 种颜色", stats.distinct_colors == 1, str(stats.distinct_colors))

    uniform = Image.new("RGB", (32, 32), (128, 128, 128))
    stats = wincapture.measure_content(uniform, sample_side=16)
    check("单色图最常见颜色占 100%", stats.uniform_ratio == 1.0,
          str(stats.uniform_ratio))

    gradient = Image.new("RGB", (32, 32))
    pixels = gradient.load()
    for x in range(32):
        for y in range(32):
            pixels[x, y] = (x * 8, y * 8, 128)
    stats = wincapture.measure_content(gradient, sample_side=32)
    check("渐变图颜色数足够", stats.distinct_colors > 3, str(stats.distinct_colors))
    check("渐变图不是单色", stats.uniform_ratio < 0.995, str(stats.uniform_ratio))


# ----------------------------------------------------------------------
# 六、组件可用性
# ----------------------------------------------------------------------
def test_availability() -> None:
    print("\n[六] 组件可用性检查")

    original_module = install_fake()
    try:
        ok, why = wincapture.is_available()
        check("假 capturekit 可用", ok is True and why == "", f"{ok} / {why}")
    finally:
        restore_fake(original_module)

    # 有 get_capabilities 时，要按系统的窗口捕获能力判断
    module = fake_capturekit()
    module.get_capabilities = lambda: SimpleNamespace(
        window_capture=FakeCapability(True)
    )
    original = sys.modules.get("capturekit")
    sys.modules["capturekit"] = module
    try:
        ok, why = wincapture.is_available()
        check("系统支持窗口捕获时可用", ok is True and why == "", f"{ok} / {why}")
    finally:
        restore_fake(original)

    module = fake_capturekit()
    module.get_capabilities = lambda: SimpleNamespace(
        window_capture=FakeCapability(False, "模拟：系统不支持 WGC")
    )
    original = sys.modules.get("capturekit")
    sys.modules["capturekit"] = module
    try:
        ok, why = wincapture.is_available()
        check("系统不支持窗口捕获时明确不可用",
              ok is False and "不支持" in why, why)
    finally:
        restore_fake(original)

    module = fake_capturekit()

    def probe_boom():
        raise RuntimeError("模拟能力探测失败")

    module.get_capabilities = probe_boom
    original = sys.modules.get("capturekit")
    sys.modules["capturekit"] = module
    try:
        ok, why = wincapture.is_available()
        check("能力探测失败按不可用处理（fail closed）",
              ok is False and "无法确认" in why, why)
    finally:
        restore_fake(original)

    original = wincapture._load_capturekit

    def boom():
        raise VisionError("窗口捕获组件不可用", detail="模拟缺失")

    wincapture._load_capturekit = boom
    try:
        ok, why = wincapture.is_available()
        check("组件缺失时明确不可用", ok is False and "不可用" in why, why)
    finally:
        wincapture._load_capturekit = original


def main() -> int:
    setup_logging("ERROR")
    print("=" * 70)
    print("窗口捕获封装测试（Windows Graphics Capture）")
    print("=" * 70)

    test_success()
    test_invalid_content()
    test_bad_window()
    test_capture_errors()
    test_stats()
    test_availability()

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 70)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for note, _, detail in failed:
        print(f"  [失败] {note}" + (f"　{detail}" if detail else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
