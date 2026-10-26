"""DPI 与坐标换算的测试（视觉感知 · 阶段 0）。

全部是**纯逻辑**：不联网、不截图、不改任何系统设置。
最后几项会读一次真实屏幕度量，但也只读不写。

这里守的是最容易悄悄错位的东西：
  - 150% 缩放时逻辑坐标和物理坐标差 1.5 倍
  - 归一化坐标 1.0 必须落在最后一个像素上，不能越界
  - 截图尺寸和系统度量对不上时必须**如实标出来**，不能假装一致

用法（在项目根目录下）：
    .venv\\Scripts\\python.exe scripts\\test_vision_dpi.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.logging_setup import setup_logging  # noqa: E402
from app.vision import dpi  # noqa: E402
from app.vision.model import ScreenMetrics  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# ----------------------------------------------------------------------
# 一、缩放倍数与取整
# ----------------------------------------------------------------------
def test_scale() -> None:
    print("\n[一] 缩放倍数")
    cases = [(96, 1.0), (120, 1.25), (144, 1.5), (192, 2.0), (0, 1.0), (-10, 1.0)]
    for dpi_value, expected in cases:
        got = dpi.scale_for_dpi(dpi_value)
        check(f"DPI {dpi_value} -> 缩放 {expected}", approx(got, expected), f"实际 {got}")

    print("\n[二] 取整规则（不能是银行家舍入）")
    rounding = [(0.5, 1), (1.5, 2), (2.5, 3), (-0.5, -1), (-1.5, -2), (0.49, 0), (-0.49, 0)]
    for value, expected in rounding:
        got = dpi.round_half_up(value)
        check(f"round_half_up({value}) = {expected}", got == expected, f"实际 {got}")


# ----------------------------------------------------------------------
# 三、逻辑 <-> 物理
# ----------------------------------------------------------------------
def test_logical_physical() -> None:
    print("\n[三] 逻辑像素 <-> 物理像素（本机 150%）")
    scale = 1.5
    check("100x200 逻辑 -> 150x300 物理",
          dpi.logical_to_physical(100, 200, scale) == (150, 300))
    check("150x300 物理 -> 100x200 逻辑",
          dpi.physical_to_logical(150, 300, scale) == (100, 200))
    check("负数坐标也能正确缩放",
          dpi.logical_to_physical(-100, -200, scale) == (-150, -300),
          str(dpi.logical_to_physical(-100, -200, scale)))

    # 本机的真实场景：未感知时系统报 1707x1067，乘 1.5 应该回到 2560x1600 附近
    physical = dpi.logical_to_physical(1707, 1067, scale)
    check("1707x1067 逻辑放大回物理尺寸（误差 ≤1 像素）",
          abs(physical[0] - 2560) <= 1 and abs(physical[1] - 1600) <= 1,
          f"实际 {physical}")

    ok = True
    detail = ""
    for point in [(0, 0), (1, 1), (123, 456), (2559, 1599)]:
        back = dpi.physical_to_logical(*dpi.logical_to_physical(*point, scale), scale)
        if abs(back[0] - point[0]) > 1 or abs(back[1] - point[1]) > 1:
            ok = False
            detail = f"{point} -> {back}"
            break
    check("逻辑->物理->逻辑 往返误差 ≤1 像素", ok, detail)

    for bad in (0, -1):
        try:
            dpi.logical_to_physical(1, 1, bad)
            raised = False
        except ValueError:
            raised = True
        check(f"缩放 {bad} 会被拒绝", raised)


# ----------------------------------------------------------------------
# 四、归一化坐标
# ----------------------------------------------------------------------
def test_normalized() -> None:
    print("\n[四] 归一化坐标（将来给视觉模型用）")
    check("(0,0) -> 左上角", dpi.normalized_to_physical(0, 0, 100, 50) == (0, 0))
    check("(1,1) -> 右下角最后一个像素，不越界",
          dpi.normalized_to_physical(1, 1, 100, 50) == (99, 49),
          str(dpi.normalized_to_physical(1, 1, 100, 50)))
    check("(0.5,0.5) 落在正中",
          dpi.normalized_to_physical(0.5, 0.5, 101, 51) == (50, 25),
          str(dpi.normalized_to_physical(0.5, 0.5, 101, 51)))

    clamped = dpi.normalized_to_physical(-5, 9, 100, 50)
    check("越界的归一化值会被夹回 0~1", clamped == (0, 49), str(clamped))

    origin = dpi.normalized_to_physical(0, 0, 100, 50, origin_x=-1920, origin_y=-100)
    check("带负原点的坐标（多显示器）", origin == (-1920, -100), str(origin))

    check("(0,0) 的归一化是 (0.0, 0.0)",
          dpi.physical_to_normalized(0, 0, 100, 50) == (0.0, 0.0))
    corner = dpi.physical_to_normalized(99, 49, 100, 50)
    check("右下角的归一化是 (1.0, 1.0)", approx(corner[0], 1.0) and approx(corner[1], 1.0),
          str(corner))
    check("尺寸为 0 时不会除零",
          dpi.physical_to_normalized(10, 10, 0, 0) == (0.0, 0.0))

    ok = True
    detail = ""
    for nx, ny in [(0.0, 0.0), (0.25, 0.75), (0.5, 0.5), (1.0, 1.0)]:
        px, py = dpi.normalized_to_physical(nx, ny, 2560, 1600)
        bx, by = dpi.physical_to_normalized(px, py, 2560, 1600)
        if abs(bx - nx) > 0.001 or abs(by - ny) > 0.001:
            ok = False
            detail = f"({nx},{ny}) -> ({px},{py}) -> ({bx},{by})"
            break
    check("归一化->物理->归一化 往返误差 ≤0.001", ok, detail)


# ----------------------------------------------------------------------
# 五、输入绝对坐标（0~65535，阶段 0 只换算不发送）
# ----------------------------------------------------------------------
def test_input_absolute() -> None:
    print("\n[五] 输入绝对坐标（SendInput 用的 0~65535）")
    metrics = ScreenMetrics(virtual_width=2560, virtual_height=1600)

    check("左上角 -> (0, 0)", dpi.physical_to_input_absolute(0, 0, metrics) == (0, 0))
    check("右下角 -> (65535, 65535)",
          dpi.physical_to_input_absolute(2559, 1599, metrics) == (65535, 65535),
          str(dpi.physical_to_input_absolute(2559, 1599, metrics)))
    check("超出屏幕的坐标会被夹住",
          dpi.physical_to_input_absolute(-500, 9999, metrics) == (0, 65535),
          str(dpi.physical_to_input_absolute(-500, 9999, metrics)))

    dual = ScreenMetrics(virtual_left=-1920, virtual_width=4480, virtual_height=1080)
    check("负原点虚拟桌面：最左边 -> 0",
          dpi.physical_to_input_absolute(-1920, 0, dual)[0] == 0,
          str(dpi.physical_to_input_absolute(-1920, 0, dual)))

    ok = True
    detail = ""
    for point in [(0, 0), (640, 400), (1280, 800), (2559, 1599)]:
        absolute = dpi.physical_to_input_absolute(*point, metrics)
        back = dpi.input_absolute_to_physical(*absolute, metrics)
        if abs(back[0] - point[0]) > 2 or abs(back[1] - point[1]) > 2:
            ok = False
            detail = f"{point} -> {absolute} -> {back}"
            break
    check("物理->绝对->物理 往返误差 ≤2 像素", ok, detail)


# ----------------------------------------------------------------------
# 六、矩形运算
# ----------------------------------------------------------------------
def test_rects() -> None:
    print("\n[六] 矩形裁剪与相交")
    check("部分重叠会裁到边界",
          dpi.clip_rect((10, 10, 200, 200), (0, 0, 100, 100)) == (10, 10, 100, 100))
    check("完全在外面返回 None",
          dpi.clip_rect((200, 200, 300, 300), (0, 0, 100, 100)) is None)
    check("刚好贴着边（没有面积）也返回 None",
          dpi.clip_rect((100, 0, 200, 100), (0, 0, 100, 100)) is None)
    check("完全包含时原样返回",
          dpi.clip_rect((10, 10, 20, 20), (0, 0, 100, 100)) == (10, 10, 20, 20))

    screen = (0, 0, 2560, 1600)
    check("正常窗口在屏幕内",
          dpi.rect_intersects((100, 100, 500, 400), screen) is True)
    check("半截露在屏幕外也算在屏幕上",
          dpi.rect_intersects((-100, 100, 300, 400), screen) is True)
    # 本机实测：FlClash 最小化到托盘后矩形是 -31994,-32000 —— 那是假坐标
    check("最小化到托盘的假坐标不算在屏幕上",
          dpi.rect_intersects((-31994, -32000, -31769, -31967), screen) is False)


# ----------------------------------------------------------------------
# 七、预览坐标换算
# ----------------------------------------------------------------------
def test_preview() -> None:
    print("\n[七] 物理像素 -> 预览图")
    full = dpi.physical_rect_to_preview(
        (0, 0, 2560, 1600),
        virtual_left=0, virtual_top=0,
        original_size=(2560, 1600), preview_size=(800, 500),
    )
    check("整屏 -> 整个预览", full == (0, 0, 800, 500), str(full))

    half = dpi.physical_rect_to_preview(
        (1280, 800, 2560, 1600),
        virtual_left=0, virtual_top=0,
        original_size=(2560, 1600), preview_size=(800, 500),
    )
    check("右下四分之一 -> 预览的右下四分之一", half == (400, 250, 800, 500), str(half))

    check("原图尺寸为 0 时明确报错，不除零",
          _raises_value_error(lambda: dpi.physical_rect_to_preview(
              (0, 0, 10, 10), virtual_left=0, virtual_top=0,
              original_size=(0, 0), preview_size=(800, 500))))


def _raises_value_error(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False


# ----------------------------------------------------------------------
# 八、截图尺寸核对
# ----------------------------------------------------------------------
def test_check_capture() -> None:
    print("\n[八] 截图尺寸 vs 系统度量")
    good = ScreenMetrics(virtual_width=2560, virtual_height=1600)
    ok_metrics = dpi.check_capture(good, (2560, 1600))
    check("尺寸一致 -> consistent=True", ok_metrics.consistent is True)
    check("一致时不写多余的备注", ok_metrics.note == "")

    # 未感知时的典型错位：度量是逻辑像素 1707x1067，截图是物理 2560x1600
    unaware = ScreenMetrics(virtual_width=1707, virtual_height=1067)
    bad = dpi.check_capture(unaware, (2560, 1600))
    check("尺寸不一致 -> consistent=False", bad.consistent is False)
    check("不一致时备注里说明了原因", "1.5" in bad.note or "不一致" in bad.note or "错位" in bad.note,
          bad.note)
    check("不一致时备注里带着两个尺寸", "2560x1600" in bad.note and "1707x1067" in bad.note,
          bad.note)

    unknown = dpi.check_capture(ScreenMetrics(), (100, 100))
    check("拿不到系统尺寸 -> consistent=None（不瞎判）", unknown.consistent is None)


# ----------------------------------------------------------------------
# 九、真实系统度量（只读）
# ----------------------------------------------------------------------
def test_real_system() -> None:
    print("\n[九] 真实系统度量（只读，不改任何设置）")
    info = dpi.ensure_awareness()
    check("DPI 感知设置没有报错", info.get("error") == "", str(info))
    check("感知级别是 per_monitor", info.get("awareness") == "per_monitor",
          str(info.get("awareness")))

    metrics = dpi.screen_metrics()
    check("虚拟桌面宽高都大于 0",
          metrics.virtual_width > 0 and metrics.virtual_height > 0,
          f"{metrics.virtual_width}x{metrics.virtual_height}")
    check("DPI 大于 0", metrics.dpi > 0, str(metrics.dpi))
    check("缩放倍数和 DPI 一致",
          approx(metrics.scale, dpi.scale_for_dpi(metrics.dpi)))

    print(f"       本机实测：物理 {metrics.primary_width}x{metrics.primary_height}，"
          f"DPI {metrics.dpi}，缩放 {metrics.scale}，显示器 {metrics.monitor_count} 块")

    spaces = dpi.describe_spaces(metrics)
    check("四种坐标空间都有说明",
          set(spaces) == {"physical", "logical", "normalized", "input_absolute"},
          str(sorted(spaces)))


# ----------------------------------------------------------------------
def main() -> int:
    setup_logging("ERROR")
    print("=" * 70)
    print("DPI 与坐标换算测试（视觉感知 · 阶段 0）")
    print("=" * 70)

    test_scale()
    test_logical_physical()
    test_normalized()
    test_input_absolute()
    test_rects()
    test_preview()
    test_check_capture()
    test_real_system()

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 70)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        for note, _, detail in failed:
            print(f"  [失败] {note}" + (f"　{detail}" if detail else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
