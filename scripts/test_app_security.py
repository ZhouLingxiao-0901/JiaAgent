"""安全守卫测试（阶段二）。

只验证"规则本身对不对"，不启动任何程序、不碰注册表写操作。

守住的安全不变量：
  1. 危险程序黑名单按文件名比对，不区分大小写
  2. 脚本/安装包不能当启动目标
  3. "common files" 这类关键词要检查整条路径，而不是只看文件名
  4. 注册表扫描**只读**（代码里不能出现 CreateKey / SetValue / DeleteKey）
  5. 启动器绝不提权（代码里不能出现 runas）

用法：
  .venv\\Scripts\\python.exe scripts\\test_app_security.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.apps import guard  # noqa: E402
from app.apps.model import LaunchKind  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, ok, detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


def main() -> int:
    print("=" * 68)
    print("安全守卫测试")
    print("=" * 68)

    # ---------------- 黑名单 ----------------
    print()
    print("危险程序黑名单：")
    for name in ("cmd.exe", "powershell.exe", "pwsh.exe", "regedit.exe",
                 "wscript.exe", "rundll32.exe", "python.exe", "msiexec.exe"):
        check(f"{name} 在黑名单里", guard.is_blocked_executable(name))
    for name in ("notepad.exe", "charmap.exe", "Code.exe", "chrome.exe"):
        check(f"{name} 不在黑名单里", not guard.is_blocked_executable(name))
    check("黑名单比对不区分大小写", guard.is_blocked_executable("CMD.EXE"))

    # ---------------- 启动前检查 ----------------
    print()
    print("启动前检查（check_target）：")
    ok, _ = guard.check_target(r"C:\Windows\System32\cmd.exe", LaunchKind.EXE)
    check("cmd.exe 被拒绝", not ok)

    ok, _ = guard.check_target(r"C:\Windows\System32\notepad.exe", LaunchKind.EXE)
    check("notepad.exe 放行", ok)

    ok, reason = guard.check_target(r"C:\temp\evil.bat", LaunchKind.EXE)
    check(".bat 脚本被拒绝", not ok, reason[:30])

    ok, reason = guard.check_target(r"C:\temp\install.msi", LaunchKind.EXE)
    check(".msi 安装包被拒绝", not ok, reason[:30])

    ok, _ = guard.check_target("", LaunchKind.EXE)
    check("空目标被拒绝", not ok)

    # 商店应用/系统标识没有文件路径，只能靠索引本身保证，不拦
    ok, _ = guard.check_target("Microsoft.WindowsNotepad_8wekyb3d8bbwe!App", LaunchKind.AUMID)
    check("商店应用标识不按文件路径拦", ok)

    # ---------------- 收录检查 ----------------
    print()
    print("收录检查（is_worthy_of_index）：")
    check("记事本值得收录",
          guard.is_worthy_of_index("记事本", r"C:\Windows\System32\notepad.exe"))
    check("卸载程序不收",
          not guard.is_worthy_of_index("卸载", r"C:\x\uninstall.exe"))
    check("Common Files 下的组件不收（整条路径检查）",
          not guard.is_worthy_of_index("TabTip", r"C:\Program Files\Common Files\Microsoft Shared\ink\TabTip.exe"))
    check("乱码路径/非 exe 不收",
          not guard.is_worthy_of_index("某程序", r"C:\x\foo.html"))

    # ---------------- 代码不变量 ----------------
    print()
    print("代码不变量（grep）：")
    here = Path(__file__).resolve().parents[1]
    sources_src = (here / "app" / "apps" / "sources.py").read_text(encoding="utf-8")
    launcher_src = (here / "app" / "apps" / "launcher.py").read_text(encoding="utf-8")

    for token in ("CreateKey", "SetValue", "DeleteKey", "DeleteValue", "KEY_SET_VALUE"):
        check(f"注册表扫描代码里没有 {token}", token not in sources_src)
    check("启动器里没有 runas（绝不提权）", "runas" not in launcher_src.casefold())
    check("启动器里不用 shell=True", "shell=True" not in launcher_src)

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
