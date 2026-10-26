"""路径权限的边界测试。不联网、不碰任何文件，纯判断。

用法（在项目根目录下）：
    .venv\\Scripts\\python.exe scripts\\test_policy.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logging_setup import setup_logging  # noqa: E402
from app.settings import expand_path  # noqa: E402
from app.tools import read_policy as rp  # noqa: E402
from app.tools.policy import PathNotAllowed, check_path, check_writable_file  # noqa: E402

HOME = Path.home()

# (说明, 路径, 期望是否放行)
CASES: list[tuple[str, str, bool]] = [
    # ---------- 应该放行 ----------
    ("D 盘根目录",              "D:\\",                              True),
    ("项目目录本身",            "D:\\JiaAgent",                      True),
    ("项目里的一个文件",        "D:\\JiaAgent\\README.md",           True),
    ("用正斜杠写路径",        "D:/JiaAgent/README.md",           True),
    ("桌面",                    "~\\Desktop",                        True),
    ("文档目录",                "~\\Documents",                      True),
    ("D 盘下待创建的新文件",    "D:\\JiaAgent\\logs\\新文件.txt",    True),
    ("绕一圈又回到 D 盘",       "D:\\JiaAgent\\app\\..\\README.md",  True),

    # ---------- 应该拒绝 ----------
    ("系统目录",                "C:\\Windows",                       False),
    ("系统目录里的文件",        "C:\\Windows\\System32\\drivers\\etc\\hosts", False),
    ("Program Files",           "C:\\Program Files",                 False),
    ("另一个用户目录",          "C:\\Users\\Public",                 False),
    ("用户主目录本身",          "~",                                 False),
    ("AppData（黑名单）",       "~\\AppData\\Local\\Temp",           False),
    ("SSH 密钥目录（黑名单）",  "~\\.ssh\\id_rsa",                   False),
    ("项目密钥（黑名单）",      "D:\\JiaAgent\\.env",                False),
    ("虚拟环境（黑名单）",      "D:\\JiaAgent\\.venv\\Scripts\\python.exe", False),
    ("数据库目录（黑名单）",    "D:\\JiaAgent\\data\\jiaagent.db",   False),
    ("用 .. 绕到黑名单",        "D:\\JiaAgent\\app\\..\\.env",       False),
    ("从桌面往外绕到系统盘",  "~/Desktop/../../Windows",              False),
    ("主目录下但不在白名单",  "~/Pictures",                          False),
    ("空路径",                  "",                                  False),
    ("只有一个空格",            "   ",                               False),
]

WRITE_CASES: list[tuple[str, str, bool]] = [
    ("写进允许目录",            "D:\\JiaAgent\\logs\\a.txt",         True),
    ("写进桌面",                "~\\Desktop\\a.txt",                 True),
    ("想写进系统目录",          "C:\\Windows\\a.txt",                False),
    ("想写进黑名单目录",        "D:\\JiaAgent\\data\\a.txt",         False),
    ("用 .. 绕出桌面再写",    "~/Desktop/../../Windows/a.txt",        False),
]


def show(raw: str) -> str:
    """把路径显示成人能看的样子。空路径直接显示原文，免得被解析成当前目录。"""
    if not raw.strip():
        return repr(raw)
    try:
        return str(expand_path(raw))
    except Exception:
        return raw


def check_read_roots() -> tuple[int, int]:
    """检查 read_policy 的读取根边界：主目录及其祖先不能当根。"""
    print()
    print("=" * 74)
    print(" 读取根目录边界（ReadAccessPolicy）")
    print("=" * 74)

    total = 0
    failed = 0

    def record(note: str, ok: bool, detail: str = "") -> None:
        nonlocal total, failed
        total += 1
        failed += 0 if ok else 1
        mark = "[OK]  " if ok else "[!!]  "
        print(f" {mark}{note}")
        if not ok and detail:
            print(f"        {detail}")

    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError) as exc:
        record("解析用户主目录", False, f"无法准备测试：{exc}")
        return failed, total

    def expect_root(note: str, path: Path, should_reject: bool) -> None:
        reason = rp._too_broad_root(path)
        rejected = bool(reason)
        expected = "拒绝" if should_reject else "接受"
        actual = "拒绝" if rejected else "接受"
        record(
            f"{note} -> {expected}",
            rejected == should_reject,
            f"实际{actual}；路径={path}；理由={reason}",
        )

    expect_root("用户主目录本身", home, True)
    expect_root("用户主目录的直接父目录", home.parent, True)
    if home.parent.parent != home.parent:
        expect_root("用户主目录的更高层祖先", home.parent.parent, True)
    expect_root("用户主目录下的普通项目目录", home / "JiaAgent-test-project", False)
    if os.name == "nt":
        other_drive = "D:\\" if not home.drive.upper().startswith("D:") else "C:\\"
        expect_root(
            "其他盘符下的普通项目目录",
            Path(other_drive) / "JiaAgent-test-project",
            False,
        )

    for note, path in (
        ("ReadAccessPolicy 拒绝主目录本身", home),
        ("ReadAccessPolicy 拒绝主目录父目录", home.parent),
    ):
        policy = rp.ReadAccessPolicy(allowed_roots=[str(path)])
        record(note, policy._roots == (), f"实际 roots={policy._roots}")

    child = home / "JiaAgent-test-project"
    child_policy = rp.ReadAccessPolicy(allowed_roots=[str(child)])
    record(
        "ReadAccessPolicy 接受主目录下的普通项目目录",
        len(child_policy._roots) == 1,
        f"实际 roots={child_policy._roots}",
    )

    with mock.patch.object(rp.Path, "home", side_effect=OSError("模拟主目录不可用")):
        reason = rp._too_broad_root(Path("D:\\JiaAgent-test-other"))
    record(
        "Path.home 抛 OSError 时失败关闭",
        bool(reason),
        f"实际理由为空，路径被放行；理由={reason}",
    )

    fake_home = mock.MagicMock()
    fake_home.resolve.side_effect = OSError("模拟主目录解析失败")
    with mock.patch.object(rp.Path, "home", return_value=fake_home):
        reason = rp._too_broad_root(Path("D:\\JiaAgent-test-other"))
    record(
        "用户主目录无法 resolve 时失败关闭",
        bool(reason),
        f"实际理由为空，路径被放行；理由={reason}",
    )

    return failed, total


def main() -> int:
    setup_logging("ERROR")

    print("=" * 74)
    print(" 路径权限检查（读）")
    print("=" * 74)
    print(f" 主目录展开为：{HOME}")
    print()

    failed = 0
    for note, raw, should_pass in CASES:
        try:
            check_path(raw, must_exist=False)
            passed, reason = True, ""
        except PathNotAllowed as exc:
            passed, reason = False, exc.message

        ok = passed == should_pass
        failed += 0 if ok else 1
        mark = "[OK]  " if ok else "[!!]  "
        verdict = "放行" if passed else "拒绝"
        print(f" {mark}{note:<22} {verdict}  {show(raw)}")
        if not ok:
            print(f"        期望{'放行' if should_pass else '拒绝'}，实际{verdict}")
            if reason:
                print(f"        理由：{reason}")

    print()
    print("=" * 74)
    print(" 路径权限检查（写文件）")
    print("=" * 74)
    for note, raw, should_pass in WRITE_CASES:
        try:
            check_writable_file(raw)
            passed, reason = True, ""
        except PathNotAllowed as exc:
            passed, reason = False, exc.message

        ok = passed == should_pass
        failed += 0 if ok else 1
        mark = "[OK]  " if ok else "[!!]  "
        verdict = "放行" if passed else "拒绝"
        print(f" {mark}{note:<22} {verdict}  {show(raw)}")
        if not ok:
            print(f"        期望{'放行' if should_pass else '拒绝'}，实际{verdict}")
            if reason:
                print(f"        理由：{reason}")

    read_failed, read_total = check_read_roots()
    failed += read_failed

    print()
    print("=" * 74)
    total = len(CASES) + len(WRITE_CASES) + read_total
    if failed:
        print(f" 结果：{total - failed}/{total} 通过，{failed} 项不符合预期 [!!]")
        return 1
    print(f" 结果：{total}/{total} 全部通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
