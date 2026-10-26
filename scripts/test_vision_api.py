"""屏幕感知接口的端到端测试（视觉感知 · 阶段 0）。

前提：先启动服务（另开一个窗口运行  .venv\\Scripts\\python.exe run.py）。

本脚本只调用**只读接口**：
  - 不会移动鼠标、不会按键、不会切换窗口
  - 不会把截图写进磁盘（调用前后会核对 data/ 目录没多出文件）

用法：
    .venv\\Scripts\\python.exe scripts\\test_vision_api.py
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

BASE = "http://127.0.0.1:8000"

# 本机地址绝不要走系统代理。和 test_api_flow.py 一样，这里自己设一遍。
os.environ["NO_PROXY"] = ",".join(
    filter(None, ["127.0.0.1", "localhost", os.environ.get("NO_PROXY", "")])
)
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


def get_json(path: str, timeout: float = 120) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def main() -> int:
    print("=" * 70)
    print("屏幕感知接口测试（视觉感知 · 阶段 0）")
    print("=" * 70)

    # ---- 0. 服务活着吗 ----
    try:
        status, health = get_json("/api/health", timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"\n连不上服务：{exc}")
        print("请先在另一个窗口运行： .venv\\Scripts\\python.exe run.py")
        return 1
    check("服务在线", status == 200 and health.get("status") == "ok", str(health))

    # ---- 1. context（不截图） ----
    print("\n[一] /api/vision/context")
    status, ctx = get_json("/api/vision/context")
    check("返回 200", status == 200, str(status))
    metrics = ctx.get("metrics") or {}
    check("有 metrics", bool(metrics))
    check("虚拟桌面宽高都大于 0",
          metrics.get("virtual", [0, 0, 0, 0])[2] > 0
          and metrics.get("virtual", [0, 0, 0, 0])[3] > 0,
          str(metrics.get("virtual")))
    check("DPI 大于 0", metrics.get("dpi", 0) > 0, str(metrics.get("dpi")))
    check("DPI 感知是 per_monitor", metrics.get("awareness") == "per_monitor",
          f"{metrics.get('awareness')} / {metrics.get('awareness_method')}")
    check("明确写了不落盘", ctx.get("persisted") is False)
    check("带上了四种坐标空间的说明",
          set(ctx.get("coordinate_spaces") or {}) ==
          {"physical", "logical", "normalized", "input_absolute"})
    fg = ctx.get("foreground")
    check("前台窗口要么是 None、要么字段齐全",
          fg is None or {"hwnd", "title", "rect", "pid"} <= set(fg),
          "（当前没有前台窗口）" if fg is None else str(fg.get("title")))
    if fg:
        check("前台窗口矩形有面积", fg["rect"][2] > fg["rect"][0] and fg["rect"][3] > fg["rect"][1],
              str(fg["rect"]))

    # ---- 2. windows ----
    print("\n[二] /api/vision/windows")
    status, wins = get_json("/api/vision/windows?limit=5")
    check("返回 200", status == 200, str(status))
    check("条数不超过 limit", len(wins.get("windows") or []) <= 5,
          str(len(wins.get("windows") or [])))
    check("每一条都有 hwnd 和标题",
          all(w.get("hwnd") and w.get("title") for w in wins.get("windows") or [])
          or not wins.get("windows"))

    # ---- 3. snapshot（真截图） ----
    print("\n[三] /api/vision/snapshot")
    before = {item.name for item in DATA_DIR.iterdir()} if DATA_DIR.exists() else set()
    status, snap = get_json("/api/vision/snapshot?annotate=1&max_side=640")
    after = {item.name for item in DATA_DIR.iterdir()} if DATA_DIR.exists() else set()

    check("返回 200", status == 200, str(status))
    if status != 200:
        print("       ", str(snap)[:300])
        return _summary()

    check("明确写了不落盘", snap.get("persisted") is False)
    check("data/ 目录没有多出文件", before == after, f"新增：{sorted(after - before)}")

    preview = snap.get("preview") or {}
    size = preview.get("size") or [0, 0]
    original = preview.get("original_size") or [0, 0]
    check("预览最长边不超过 640", max(size) <= 640, str(size))
    check("原图尺寸和系统度量一致",
          list(original) == list(metrics.get("virtual", [0, 0, 0, 0])[2:]),
          f"{original} vs {metrics.get('virtual')}")
    check("截图与系统度量对得上", (snap.get("metrics") or {}).get("capture_matches_metrics") is True,
          (snap.get("metrics") or {}).get("note", ""))

    image = snap.get("image") or ""
    check("图片是 PNG data URL", image.startswith("data:image/png;base64,"), image[:32])
    if image.startswith("data:image/png;base64,"):
        raw = base64.b64decode(image.split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as decoded:
            decoded.load()
            check("图片实际尺寸和 preview.size 一致", list(decoded.size) == list(size),
                  f"{decoded.size} vs {size}")

    rect = snap.get("foreground_in_preview")
    if rect:
        check("前台窗口的框在预览范围内",
              0 <= rect[0] < rect[2] <= size[0] and 0 <= rect[1] < rect[3] <= size[1],
              f"{rect} in {size}")
        check("确实画了框", preview.get("annotated") is True)
    else:
        print("       （没有前台窗口或坐标不一致，跳过标框检查）")

    # ---- 4. 参数边界 ----
    print("\n[四] 参数边界")
    status, body = get_json("/api/vision/snapshot?max_side=100", timeout=30)
    check("max_side 小于 320 被拒（422）", status == 422, f"HTTP {status}")

    status, body = get_json("/api/vision/windows?limit=0", timeout=30)
    check("limit 小于 1 被拒（422）", status == 422, f"HTTP {status}")

    return _summary()


def _summary() -> int:
    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 70)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        for note, _, detail in failed:
            print(f"  [失败] {note}" + (f"　{detail}" if detail else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
