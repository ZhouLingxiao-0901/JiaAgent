"""截图、预览与失败处理的测试（视觉感知 · 阶段 0）。

会真的截一张屏（只读，不移动鼠标、不按键、不落盘）。
另外用"注入故障"的方式覆盖几条不能靠真实情况复现的路径：
  - 抓屏直接抛异常
  - 抓屏返回 None
  - 抓屏返回 0x0 的图
  - 系统度量故意报错成 1707x1067（模拟 DPI 感知失效）

最后一条最要紧：尺寸对不上时**必须拒绝画框**，
否则会画出一个看起来对、其实整体偏移 1.5 倍的框。

用法（在项目根目录下）：
    .venv\\Scripts\\python.exe scripts\\test_vision_capture.py
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from app.errors import VisionError  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.vision import capture, dpi  # noqa: E402
from app.vision.model import ScreenMetrics  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


def _fake_snapshot_size(side: int = 320):
    """在不抓屏的前提下造一张假图，用来测纯图像处理。"""
    return Image.new("RGB", (side * 2, side), (30, 30, 30))


# ----------------------------------------------------------------------
# 一、真实抓屏
# ----------------------------------------------------------------------
def test_grab() -> None:
    print("\n[一] 真实抓屏（只读）")
    try:
        image = capture.grab_screen()
    except VisionError as exc:
        check("抓屏成功", False, f"{exc.message} / {exc.detail}")
        return
    except Exception as exc:  # noqa: BLE001
        check("抓屏成功", False, f"{type(exc).__name__}: {exc}")
        return

    check("抓屏成功", True, f"{image.size[0]}x{image.size[1]}")
    check("图像模式是 RGB", image.mode == "RGB", image.mode)

    metrics = dpi.screen_metrics()
    consistent = dpi.check_capture(metrics, image.size)
    check("截图尺寸和系统度量一致（DPI 感知生效）",
          consistent.consistent is True,
          consistent.note or f"截图 {image.size} / 度量 "
          f"{metrics.virtual_width}x{metrics.virtual_height}")


# ----------------------------------------------------------------------
# 二、预览缩放
# ----------------------------------------------------------------------
def test_preview() -> None:
    print("\n[二] 预览缩放")
    big = Image.new("RGB", (2560, 1600), (20, 20, 20))

    preview = capture.make_preview(big, 800)
    check("最长边不超过上限", max(preview.size) <= 800, str(preview.size))
    check("比例基本不变",
          abs(preview.size[0] / preview.size[1] - 2560 / 1600) < 0.01,
          f"{preview.size[0]}x{preview.size[1]}")

    same = capture.make_preview(big, 4000)
    check("上限比原图大时不放大", same.size == big.size, str(same.size))

    zero = capture.make_preview(big, 0)
    check("上限为 0 时原样返回", zero.size == big.size, str(zero.size))
    check("返回的是副本，不是同一个对象", zero is not big)


# ----------------------------------------------------------------------
# 三、标注
# ----------------------------------------------------------------------
def test_annotate() -> None:
    print("\n[三] 窗口标框")
    base = Image.new("RGB", (400, 300), (10, 10, 10))
    marked = capture.annotate_rect(base, (50, 40, 350, 260))

    check("尺寸不变", marked.size == base.size, str(marked.size))
    check("返回新图，不改原图", marked is not base)
    check("像素确实被改过", marked.tobytes() != base.tobytes())

    # 框贴着边界、标签没地方放时也不能崩
    try:
        capture.annotate_rect(base, (0, 0, 400, 300))
        capture.annotate_rect(base, (2, 2, 6, 6))
        check("极端矩形不会崩", True)
    except Exception as exc:  # noqa: BLE001
        check("极端矩形不会崩", False, f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------
# 四、完整快照 + 不落盘
# ----------------------------------------------------------------------
def test_snapshot() -> None:
    print("\n[四] 完整快照（不落盘）")
    before = {item.name for item in DATA_DIR.iterdir()} if DATA_DIR.exists() else set()

    try:
        snap = capture.snapshot(annotate=True, max_side=640)
    except VisionError as exc:
        check("快照成功", False, f"{exc.message} / {exc.detail}")
        return

    after = {item.name for item in DATA_DIR.iterdir()} if DATA_DIR.exists() else set()

    check("快照成功", True, f"原图 {snap.original_size} / 预览 {snap.preview_size}")
    check("预览最长边不超过上限", max(snap.preview_size) <= 640, str(snap.preview_size))
    check("预览缩放倍数在 0~1 之间", 0 < snap.preview_scale <= 1, str(snap.preview_scale))
    check("时间戳非空", bool(snap.taken_at), snap.taken_at)
    check("截图没有落盘", before == after, f"新增：{sorted(after - before)}")

    if snap.window is not None and snap.metrics.consistent is not False:
        rect = snap.window_in_preview
        check("前台窗口的框在预览范围内",
              rect is not None
              and 0 <= rect[0] < rect[2] <= snap.preview_size[0]
              and 0 <= rect[1] < rect[3] <= snap.preview_size[1],
              f"{rect} in {snap.preview_size}")
        check("确实画了框", snap.annotated is True)
    else:
        print("       （当前没有前台窗口或坐标不一致，跳过标框检查）")

    url = capture.data_url(snap.preview)
    check("data URL 前缀正确", url.startswith("data:image/png;base64,"))
    raw = base64.b64decode(url.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as decoded:
        decoded.load()
        check("data URL 能解回同尺寸的 PNG", decoded.size == snap.preview_size,
              f"{decoded.size} vs {snap.preview_size}")


# ----------------------------------------------------------------------
# 五、故障注入
# ----------------------------------------------------------------------
def test_failures() -> None:
    print("\n[五] 故障注入：截图失败必须如实报错")

    original_grab = capture.ImageGrab.grab

    def _expect_vision_error(label: str, replacement) -> None:
        capture.ImageGrab.grab = replacement
        try:
            capture.grab_screen()
            check(label, False, "居然没有抛 VisionError")
        except VisionError as exc:
            check(label, True, f"{exc.message} / {exc.detail}")
        except Exception as exc:  # noqa: BLE001
            check(label, False, f"抛了别的异常：{type(exc).__name__}: {exc}")
        finally:
            capture.ImageGrab.grab = original_grab

    def _boom(*_args, **_kwargs):
        raise OSError("模拟：屏幕被锁了")

    class _FakeEmpty:
        mode = "RGB"
        size = (0, 0)

        def convert(self, _mode):
            return self

    _expect_vision_error("抓屏抛异常 -> VisionError", _boom)
    _expect_vision_error("抓屏返回 None -> VisionError", lambda *a, **k: None)
    _expect_vision_error("抓屏返回 0x0 -> VisionError", lambda *a, **k: _FakeEmpty())


# ----------------------------------------------------------------------
# 六、坐标不一致时必须拒绝画框
# ----------------------------------------------------------------------
def test_mismatch_guard() -> None:
    print("\n[六] 坐标不一致时不画框（防「看起来对、其实偏 1.5 倍」）")

    original_metrics = capture.dpi.screen_metrics
    # 假装 DPI 感知没设成功：系统度量是逻辑像素 1707x1067
    capture.dpi.screen_metrics = lambda: ScreenMetrics(
        virtual_width=1707, virtual_height=1067, dpi=96, scale=1.0
    )
    try:
        snap = capture.snapshot(annotate=True, max_side=400)
    except VisionError as exc:
        check("不一致时依然能出图", False, f"{exc.message} / {exc.detail}")
        return
    finally:
        capture.dpi.screen_metrics = original_metrics

    check("识别出尺寸不一致", snap.metrics.consistent is False,
          snap.metrics.note)
    check("不一致时没有画框", snap.annotated is False)
    check("说明了为什么没画框", "对不上" in snap.annotation_note or "错位" in snap.annotation_note,
          snap.annotation_note)
    check("不一致时也不给出预览框坐标", snap.window_in_preview is None)


# ----------------------------------------------------------------------
def main() -> int:
    setup_logging("ERROR")
    print("=" * 70)
    print("截图、预览与失败处理测试（视觉感知 · 阶段 0）")
    print("=" * 70)

    test_grab()
    test_preview()
    test_annotate()
    test_snapshot()
    test_failures()
    test_mismatch_guard()

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 70)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        for note, _, detail in failed:
            print(f"  [失败] {note}" + (f"　{detail}" if detail else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
