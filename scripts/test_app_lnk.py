"""快捷方式（.lnk）解析测试。

重点守住两件事：
  1. 解析器绝不崩、绝不瞎猜——读不出来就老实说 resolved_ok=False
  2. EnvironmentVariableDataBlock 里那 520 字节 Unicode 要按 UTF-16 读，
     而不是把 ANSI 那 260 字节当成 UTF-16 解（那会解出一串中文乱码，
     让"危险程序黑名单"按文件名比对时完全失效）

用法：
  .venv\\Scripts\\python.exe scripts\\test_app_lnk.py
"""

from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.apps import lnk  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, ok, detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


def _minimal_header(flags: int = 0) -> bytes:
    """拼一个 76 字节的合法 .lnk 文件头（HeaderSize + CLSID + LinkFlags）。"""
    header = struct.pack("<I", lnk._HEADER_SIZE)
    header += lnk._LNK_CLSID
    header += struct.pack("<I", flags)
    header += b"\x00" * (lnk._HEADER_SIZE - 20)
    return header


def main() -> int:
    print("=" * 68)
    print("快捷方式解析测试")
    print("=" * 68)

    # ---------------- 基本健壮性 ----------------
    print()
    print("健壮性：")

    # 不存在的文件：不抛异常，老实说读不到
    info = lnk.parse_lnk(r"C:\definitely\not\here.lnk")
    check("不存在的快捷方式不崩、不瞎猜", info.resolved_ok is False and bool(info.note), info.note[:40])

    # 一个没有目标的最短合法头：没有候选目标，说明原因
    info2 = lnk._parse(_minimal_header(), Path("x.lnk"))
    check("没有目标的快捷方式如实说明", info2.resolved_ok is False and not info2.target,
          info2.note[:40])

    # 乱字节：格式看不懂，而不是抛异常
    with tempfile.TemporaryDirectory() as tmp:
        junk = Path(tmp) / "junk.lnk"
        junk.write_bytes(b"\x00\x01\x02\x03" * 40)
        info3 = lnk.parse_lnk(junk)
    check("乱字节文件不崩、如实说明", info3.resolved_ok is False and bool(info3.note), info3.note[:40])

    # ---------------- 参数拆分 ----------------
    print()
    print("参数拆分：")
    check("带引号的参数不被空格拆坏",
          lnk._split_args('--foo "bar baz" --qux') == ["--foo", "bar baz", "--qux"])
    check("连续空格被吞掉", lnk._split_args("  a   b  ") == ["a", "b"])

    # ---------------- 环境变量块（乱码修复） ----------------
    print()
    print("EnvironmentVariableDataBlock（乱码修复）：")
    target = r"C:\Windows\system32\cmd.exe"
    ansi = target.encode("mbcs") if sys.platform == "win32" else target.encode("latin-1")
    ansi = ansi.ljust(lnk._ENV_ANSI_LEN, b"\x00")
    unicode_ = target.encode("utf-16-le").ljust(lnk._ENV_UNICODE_LEN, b"\x00")
    block = struct.pack("<II", 8 + lnk._ENV_ANSI_LEN + lnk._ENV_UNICODE_LEN, lnk._ENV_VAR_BLOCK)
    block += ansi + unicode_
    got = lnk._scan_extra_data(block, 0)
    check("环境变量块解出正确的路径（不是乱码）", got == target, f"实际 {got!r}")

    # ---------------- 真实开始菜单里的快捷方式 ----------------
    print()
    print("真实快捷方式（烟测）：")
    roots = []
    appdata = __import__("os").environ.get("APPDATA")
    programdata = __import__("os").environ.get("ProgramData")
    if appdata:
        roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    if programdata:
        roots.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")

    found = 0
    for root in roots:
        if not root.is_dir():
            continue
        for p in list(root.rglob("*.lnk"))[:30]:
            parsed = lnk.parse_lnk(p)
            # 只要能返回、且"要么有目标要么有说明"就算健壮
            ok = bool(parsed.target) or bool(parsed.note)
            if ok:
                found += 1
            if found >= 3:
                break
        if found >= 3:
            break
    check("真实开始菜单快捷方式能解析", found >= 3, f"抽验了 {found} 个")

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
