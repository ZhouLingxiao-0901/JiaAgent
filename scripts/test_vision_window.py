"""窗口信息与异常处理的测试（视觉感知 · 阶段 0）。

会读真实的窗口列表（只读，不点、不切、不改任何窗口）。
重点覆盖"异常输入不能让程序崩"：
  - hwnd 为 0、负数、根本不存在
  - 最小化到托盘的窗口躺在一个假坐标上（本机实测 FlClash 是 -32000）
  - 前台窗口可能压根没有（锁屏、切桌面）

用法（在项目根目录下）：
    .venv\\Scripts\\python.exe scripts\\test_vision_window.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.logging_setup import setup_logging  # noqa: E402
from app.vision import dpi, window  # noqa: E402
from app.vision.model import (  # noqa: E402
    CHANGE_APPEARED,
    CHANGE_DISAPPEARED,
    CHANGE_MOVED,
    CHANGE_RETITLED,
    CHANGE_SWITCHED,
    CHANGE_UNCHANGED,
    WindowInfo,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


def _w(hwnd: int, *, title: str = "窗口", rect=(0, 0, 100, 100)) -> WindowInfo:
    return WindowInfo(hwnd=hwnd, title=title, rect=rect)


# ----------------------------------------------------------------------
# 一、WindowInfo 自身
# ----------------------------------------------------------------------
def test_model() -> None:
    print("\n[一] WindowInfo 的派生字段")
    item = WindowInfo(hwnd=1, title="记事本", rect=(10, 20, 110, 220))
    check("宽度 = right - left", item.width == 100, str(item.width))
    check("高度 = bottom - top", item.height == 200, str(item.height))
    check("有面积的矩形算有效", item.is_valid is True)

    empty = WindowInfo(hwnd=2, rect=(100, 100, 100, 100))
    check("零面积矩形算无效", empty.is_valid is False)

    inverted = WindowInfo(hwnd=3, rect=(100, 100, 50, 50))
    check("反向矩形宽度不会变成负数", inverted.width == 0 and inverted.height == 0,
          f"{inverted.width}x{inverted.height}")

    data = item.to_dict()
    expected = {
        "hwnd", "title", "pid", "process", "exe_path", "class_name",
        "rect", "size", "visible", "minimized", "maximized", "rect_source",
    }
    check("to_dict 字段齐全", set(data) == expected, str(sorted(set(data) ^ expected)))
    check("to_dict 可以直接 JSON 序列化",
          isinstance(json.dumps(data, ensure_ascii=False), str))


# ----------------------------------------------------------------------
# 二、异常 hwnd
# ----------------------------------------------------------------------
def test_bad_hwnd() -> None:
    print("\n[二] 异常句柄不能把程序弄崩")
    for bad in (0, -1, 0xDEADBEEF, 123456789):
        try:
            result = window.get_window_info(bad)
            check(f"get_window_info({bad}) 不抛异常", True)
            check(f"get_window_info({bad}) 返回 None 或有效窗口",
                  result is None or (result.hwnd == bad and result.is_valid),
                  repr(result))
        except Exception as exc:  # noqa: BLE001
            check(f"get_window_info({bad}) 不抛异常", False, f"{type(exc).__name__}: {exc}")

    try:
        items = window.list_windows(limit=0)
        check("limit=0 返回空列表而不是报错", items == [], repr(items))
    except Exception as exc:  # noqa: BLE001
        check("limit=0 返回空列表而不是报错", False, str(exc))

    try:
        items = window.list_windows(limit=-5)
        check("limit=-5 返回空列表而不是报错", items == [])
    except Exception as exc:  # noqa: BLE001
        check("limit=-5 返回空列表而不是报错", False, str(exc))


# ----------------------------------------------------------------------
# 三、真实前台窗口
# ----------------------------------------------------------------------
def test_foreground() -> None:
    print("\n[三] 真实前台窗口（只读）")
    try:
        fg = window.get_foreground_window()
    except Exception as exc:  # noqa: BLE001
        check("读前台窗口不抛异常", False, f"{type(exc).__name__}: {exc}")
        return

    check("读前台窗口不抛异常", True)
    if fg is None:
        check("当前没有前台窗口（锁屏/切桌面时正常）", True, "返回 None")
        return

    check("hwnd 大于 0", fg.hwnd > 0, str(fg.hwnd))
    check("标题是字符串", isinstance(fg.title, str), repr(fg.title))
    check("进程号不是负数", fg.pid >= 0, str(fg.pid))
    check("矩形有面积", fg.is_valid, f"{fg.rect} -> {fg.width}x{fg.height}")
    check("矩形来源是 dwm_frame 或 window_rect",
          fg.rect_source in ("dwm_frame", "window_rect"), fg.rect_source)
    print(f"       前台：{fg.title!r} / {fg.process or '（进程名拿不到）'} / {fg.rect}")


# ----------------------------------------------------------------------
# 四、窗口列表与屏幕外过滤
# ----------------------------------------------------------------------
def test_list() -> None:
    print("\n[四] 窗口列表")
    try:
        items = window.list_windows(limit=50)
    except Exception as exc:  # noqa: BLE001
        check("枚举窗口不抛异常", False, f"{type(exc).__name__}: {exc}")
        return

    check("枚举窗口不抛异常", True)
    check("条数不超过 limit", len(items) <= 50, str(len(items)))
    check("每一条都有 hwnd", all(item.hwnd > 0 for item in items))
    check("每一条都有标题（默认过滤空标题）", all(item.title.strip() for item in items))

    metrics = dpi.screen_metrics()
    bounds = (
        metrics.virtual_left,
        metrics.virtual_top,
        metrics.virtual_left + metrics.virtual_width,
        metrics.virtual_top + metrics.virtual_height,
    )
    offscreen = [item for item in items if not dpi.rect_intersects(item.rect, bounds)]
    check("列表里没有完全在屏幕外的窗口", not offscreen,
          f"残留 {[(i.title, i.rect) for i in offscreen][:3]}")

    try:
        wide = window.list_windows(limit=200, include_offscreen=True)
        check("include_offscreen=True 也能正常返回", isinstance(wide, list),
              f"{len(wide)} 条（含屏幕外）")
    except Exception as exc:  # noqa: BLE001
        check("include_offscreen=True 也能正常返回", False, str(exc))

    print(f"       共 {len(items)} 个可见窗口")


# ----------------------------------------------------------------------
# 五、前台窗口变化判定（纯逻辑）
# ----------------------------------------------------------------------
def test_compare() -> None:
    print("\n[五] 前台窗口变化判定（阶段 2 要靠它确认「还是那个窗口」）")
    a = _w(100, title="记事本", rect=(0, 0, 800, 600))
    same = _w(100, title="记事本", rect=(0, 0, 800, 600))
    moved = _w(100, title="记事本", rect=(50, 50, 850, 650))
    retitled = _w(100, title="无标题 - 记事本", rect=(0, 0, 800, 600))
    other = _w(200, title="计算器", rect=(0, 0, 800, 600))

    check("两个都为空 -> unchanged",
          window.compare_windows(None, None).kind == CHANGE_UNCHANGED)
    check("从无到有 -> appeared",
          window.compare_windows(None, a).kind == CHANGE_APPEARED)
    check("从有到无 -> disappeared",
          window.compare_windows(a, None).kind == CHANGE_DISAPPEARED)
    check("换了窗口 -> switched",
          window.compare_windows(a, other).kind == CHANGE_SWITCHED)
    check("同一窗口但位置变了 -> moved",
          window.compare_windows(a, moved).kind == CHANGE_MOVED)
    check("同一窗口但标题变了 -> retitled",
          window.compare_windows(a, retitled).kind == CHANGE_RETITLED)
    check("完全一样 -> unchanged",
          window.compare_windows(a, same).kind == CHANGE_UNCHANGED)

    change = window.compare_windows(a, other)
    check("changed 属性和 kind 对得上", change.changed is True)
    check("没变时 changed 是 False",
          window.compare_windows(a, same).changed is False)

    # 判断顺序：换了窗口时，不应该被报成"位置变了"
    both = _w(300, title="别的窗口", rect=(999, 999, 1200, 1200))
    check("换窗口优先于位置变化",
          window.compare_windows(a, both).kind == CHANGE_SWITCHED)

    check("变化结论能 JSON 序列化",
          isinstance(json.dumps(change.to_dict(), ensure_ascii=False), str))


# ----------------------------------------------------------------------
def main() -> int:
    setup_logging("ERROR")
    print("=" * 70)
    print("窗口信息与异常处理测试（视觉感知 · 阶段 0）")
    print("=" * 70)

    test_model()
    test_bad_hwnd()
    test_foreground()
    test_list()
    test_compare()

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 70)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        for note, _, detail in failed:
            print(f"  [失败] {note}" + (f"　{detail}" if detail else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
