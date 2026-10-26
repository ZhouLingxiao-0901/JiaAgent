"""应用索引测试（阶段一）。

真的去扫这台电脑的开始菜单、注册表、商店应用，把结果建到**临时数据库**里。
不碰 data/jiaagent.db，不会污染你的正式索引。

顺便把"佳到底认识了哪些应用"打出来——这是阶段一最直观的成果。

用法：
  .venv\\Scripts\\python.exe scripts\\test_app_index.py
  .venv\\Scripts\\python.exe scripts\\test_app_index.py --show 50
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.apps import guard, index, matcher  # noqa: E402
from app.apps.model import LaunchKind, Source  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, ok, detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show", type=int, default=25, help="列多少条应用出来看看")
    args = parser.parse_args()

    print("=" * 68)
    print("应用索引测试")
    print("=" * 68)

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test_index.db"

        # ---------------- 第一次建索引 ----------------
        print()
        print("第一次刷新（全量）：")
        first = index.refresh(db_path=db)
        print(f"  耗时 {first['duration_ms']} 毫秒，得到 {first['total']} 条，"
              f"重新解析 {first['reparsed']} 条")
        for name, count in sorted(first["sources"].items()):
            reparsed = first["reparsed_by_source"].get(name, 0)
            print(f"    {name:<14} {count:>5} 条（重新解析 {reparsed}）")
        if first["errors"]:
            print(f"  来源出错：{first['errors']}")

        records = index.load_all(db_path=db)

        # ---------------- 内容检查 ----------------
        print()
        print("内容检查：")
        check("索引里有一定数量的应用", len(records) >= 100, f"实际 {len(records)} 条")
        check("每条都有名字和启动目标",
              all(r.name.strip() and r.target.strip() for r in records))
        check("每条都有来源和启动方式",
              all(isinstance(r.source, Source) and isinstance(r.launch_kind, LaunchKind)
                  for r in records))

        names = {r.name for r in records}

        def has_any(*candidates: str) -> bool:
            low = {n.casefold() for n in names}
            return any(c.casefold() in low for c in candidates)

        check("认得中文名应用（夸克/豆包/爱奇艺等）",
              has_any(*["夸克", "豆包", "爱奇艺", "学习通", "微信"]),
              "例：" + "、".join(n for n in names if any(
                  k in n for k in ("夸克", "豆包", "爱奇艺", "学习通"))) [:60])
        check("认得英文名应用（Ollama/Excel/Chrome 等）",
              has_any(*["Ollama", "Excel", "Access", "Chrome", "Git"]))
        check("认得商店应用（计算器/记事本/画图）",
              any(r.source is Source.UWP for r in records),
              f"商店应用 {sum(1 for r in records if r.source is Source.UWP)} 个")

        # ---------------- 安全门槛检查 ----------------
        print()
        print("安全门槛检查：")
        bad_exe = [r.name for r in records
                   if r.launch_kind is LaunchKind.EXE and guard.is_blocked_executable(r.target)]
        check("索引里没有危险程序（cmd/powershell/regedit…）", not bad_exe,
              f"混进来的：{bad_exe[:5]}" if bad_exe else "")

        bad_name = [r.name for r in records if guard.is_excluded_name(r.name, r.target)]
        check("索引里没有卸载程序/更新器", not bad_name,
              f"混进来的：{bad_name[:5]}" if bad_name else "")

        bad_ext = [r.target for r in records
                   if r.launch_kind is LaunchKind.EXE
                   and Path(r.target).suffix.casefold() not in (".exe", "")]
        check("没有把脚本/安装包当成可启动程序", not bad_ext,
              f"{bad_ext[:3]}" if bad_ext else "")

        # ---------------- 增量检查 ----------------
        print()
        print("增量检查（第二次刷新，应当几乎不重新解析）：")
        second = index.refresh(db_path=db)
        fs_reparsed = sum(second["reparsed_by_source"].get(n, 0)
                          for n in ("start_menu", "install_dirs"))
        print(f"  第二次耗时 {second['duration_ms']} 毫秒，"
              f"重新解析 {second['reparsed']} 条（其中文件系统来源 {fs_reparsed} 条）")
        print(f"  分来源：{second['reparsed_by_source']}")
        check("文件系统来源不再重新解析（增量生效）", fs_reparsed == 0, f"实际 {fs_reparsed} 条")
        check("第二次结果条数和第一次一致", second["total"] == first["total"],
              f"{first['total']} -> {second['total']}")
        check("第二次明显更快", second["duration_ms"] <= max(first["duration_ms"], 1) * 1.2,
              f"{first['duration_ms']} -> {second['duration_ms']} 毫秒")

        # ---------------- 配置里的别名 ----------------
        # 这些应用都是自动发现的（不是 apps: 里手写的），靠 config/tools.yaml
        # 的 aliases: 段补上英文/拼音名。这是"不必硬写 exe 路径也能加别名"的证明。
        print()
        print("配置别名检查（tools.yaml 的 aliases: 段）：")
        from app.settings import get_tools_config

        table = get_tools_config().aliases
        check("配置里读到了 aliases 段", bool(table), f"{len(table)} 组")

        for query, want in [("wechat", "微信"), ("weixin", "微信"),
                            ("douyin", "抖音"), ("bilibili", "哔哩哔哩")]:
            result = matcher.match(query, records)
            got = result.best.record.name if result.best else None
            check(f"「{query}」靠配置别名能匹配到", got == want,
                  f"{result.status} -> {got}")

        # ---------------- 用真实索引试匹配 ----------------
        print()
        print("用真实的索引试几个名字：")
        for query, want in [("记事本", "记事本"), ("notepad", "记事本"), ("夸克", "夸克")]:
            result = matcher.match(query, records)
            got = result.best.record.name if result.best else None
            ok = got == want
            detail = f"{result.status} -> {got}"
            check(f"「{query}」能匹配到", ok, detail)

        # ---------------- 列出来看看 ----------------
        print()
        print(f"索引里的前 {args.show} 条（按名字排序）：")
        for record in records[:args.show]:
            print(f"    {record.name[:26]:<28} {record.source.label:<12} {record.display_target()[:44]}")
        if len(records) > args.show:
            print(f"    …… 还有 {len(records) - args.show} 条")

    # ---------------- 汇总 ----------------
    print()
    print("=" * 68)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    if passed != total:
        print(f"结果：{passed}/{total} 通过，{total - passed} 项不通过")
        for note, ok, detail in RESULTS:
            if not ok:
                print(f"  [!!] {note}　{detail}")
        return 1
    print(f"结果：{passed}/{total} 全部通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
