"""启动器测试（阶段二）。

默认**不启动任何程序**，只验证"该失败的必须失败"：
  - 指向已删除程序的条目 → 必须 FAILED，绝不能说成功
  - cmd.exe 条目 → 必须被守卫拦下（FAILED）
  - 脚本目标 → 必须被拒绝（FAILED）

加上 --allow 才会真的启动一个应用（记事本），验证三态里的"确认成功"。
**这一条会真的打开记事本窗口**，所以不默认跑。

用法：
  .venv\\Scripts\\python.exe scripts\\test_app_launch.py          # 只跑安全用例
  .venv\\Scripts\\python.exe scripts\\test_app_launch.py --allow  # 再真开一个记事本
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.apps import index, launcher, matcher  # noqa: E402
from app.apps.model import AppRecord, LaunchKind, LaunchStatus, Source  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, ok, detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


def _exe_record(name: str, target: str, hint: str) -> AppRecord:
    return AppRecord(
        name=name,
        source=Source.MANUAL,
        launch_kind=LaunchKind.EXE,
        target=target,
        process_hint=hint,
    )


async def run_safe() -> None:
    print("安全用例（不会真的启动任何东西）：")

    # 1. 指向已删除的程序：必须先失败，绝不能报成功
    missing = await launcher.launch(
        _exe_record("已删除的程序", r"C:\definitely\missing\app.exe", "missing.exe")
    )
    check("指向不存在的程序 → FAILED",
          missing.status is LaunchStatus.FAILED, missing.message[:50])

    # 2. cmd.exe：即使路径真实存在，也必须被守卫拦下
    blocked = await launcher.launch(
        _exe_record("命令提示符", r"C:\Windows\System32\cmd.exe", "cmd.exe")
    )
    check("cmd.exe → FAILED（守卫拦截）",
          blocked.status is LaunchStatus.FAILED, blocked.message[:50])

    # 3. 脚本目标：不许启动 .bat
    script = await launcher.launch(
        _exe_record("恶意脚本", r"C:\temp\evil.bat", "evil.bat")
    )
    check(".bat 脚本 → FAILED", script.status is LaunchStatus.FAILED, script.message[:50])

    # 4. 商店应用标识不按文件路径拦（不真的启动它，只证明不会被误拒）
    #    这个用 check_target 直接验证即可，不必真启动
    from app.apps import guard
    ok, _ = guard.check_target(
        "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App", LaunchKind.AUMID
    )
    check("商店应用标识不被误拦", ok)


async def run_real() -> None:
    print("真实启动用例（会真的打开记事本）：")
    index.ensure_ready()
    result = matcher.match("记事本", index.load_all())
    assert result.best is not None, "索引里找不到记事本"
    record = result.best.record
    print(f"  准备启动：{record.name}（{record.display_target()}）")
    outcome = await launcher.launch(record)
    check("记事本启动并被确认（CONFIRMED）",
          outcome.status is LaunchStatus.CONFIRMED, outcome.message[:60])


async def main_async(allow: bool) -> int:
    print("=" * 68)
    print("启动器测试")
    print("=" * 68)
    print()
    await run_safe()
    if allow:
        print()
        await run_real()

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow", action="store_true", help="真的启动一个记事本")
    args = parser.parse_args()
    return asyncio.run(main_async(args.allow))


if __name__ == "__main__":
    raise SystemExit(main())
