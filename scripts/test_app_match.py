"""名字匹配测试（阶段一）。

纯逻辑，不联网、不碰磁盘、没有任何副作用——
用一批造出来的假应用，验证中文名、英文名、别名、简称、拼写错误、
以及"多个候选"和"找不到"这两种必须说清楚的情况。

用法：
  .venv\\Scripts\\python.exe scripts\\test_app_match.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.apps.matcher import match, search  # noqa: E402
from app.apps.model import AppRecord, LaunchKind, Source, normalize  # noqa: E402


def rec(name, *, aliases=(), source=Source.START_MENU_ALL, target=None, kind=LaunchKind.EXE):
    return AppRecord(
        name=name,
        source=source,
        launch_kind=kind,
        target=target or f"C:\\Fake\\{name}.exe",
        identity_key=f"C:\\Fake\\{name}.exe",
        aliases=list(aliases),
    )


# 一批假的应用，覆盖中文名、英文名、别名的情况
APPS = [
    rec("记事本", aliases=["notepad", "文本编辑器"], target=r"C:\Windows\System32\notepad.exe"),
    rec("计算器", aliases=["calc", "calculation"], target=r"C:\Windows\System32\calc.exe"),
    rec("计算器帮助", target=r"C:\Fake\calc_help.exe"),
    rec("Visual Studio Code", aliases=["vscode", "vs code", "code"], target=r"D:\Fake\Code.exe"),
    rec("夸克", aliases=["quark", "夸克浏览器"], target=r"D:\Fake\Quark.exe"),
    rec("豆包", aliases=["doubao"], target=r"D:\Fake\Doubao.exe"),
    rec("Microsoft Store 应用商店", source=Source.UWP, kind=LaunchKind.AUMID,
        target="Microsoft.WindowsStore_8wekyb3d8bbwe!App"),
    rec("开黑啦", aliases=["kook"], target=r"D:\Fake\KOOK.exe"),
]

# (说明, 查询词, 期望状态, 期望的最佳名字)
CASES: list[tuple[str, str, str, str | None]] = [
    ("中文名，完全一样",        "记事本",         "unique",    "记事本"),
    ("英文别名",                "notepad",        "unique",    "记事本"),
    ("别名带空格",              "vs code",        "unique",    "Visual Studio Code"),
    ("简称",                    "vscode",         "unique",    "Visual Studio Code"),
    ("英文名全小写",            "visual studio code", "unique", "Visual Studio Code"),
    ("中文别名",                "夸克浏览器",      "unique",    "夸克"),
    ("拼音别名",                "doubao",         "unique",    "豆包"),
    ("商店应用，按中文子串找",   "应用商店",        "unique",    "Microsoft Store 应用商店"),
    ("商店应用，按名字找",      "Microsoft Store", "unique",   "Microsoft Store 应用商店"),
    ("商店应用，按别名找",      "kook",           "unique",    "开黑啦"),
    ("全角输入应当也能匹配",    "ＮＯＴＥＰＡＤ",   "unique",    "记事本"),
    ("带上 .exe 后缀",          "notepad.exe",    "unique",    "记事本"),
    ("拼写错误（少一个字母）",  "noteapd",        "unique",    "记事本"),
    ("拼写错误（vscdoe）",      "vscdoe",         "unique",    "Visual Studio Code"),
    ("词序颠倒但高度相似",      "code studio visual", "unique", "Visual Studio Code"),
    ("多个候选：计算",          "计算",           "ambiguous", None),
    ("找不到：不存在的软件",    "天顶星办公套件",   "none",      None),
    ("找不到：空查询",          "   ",            "empty",     None),
]


def main() -> int:
    print("=" * 64)
    print("名字匹配测试")
    print("=" * 64)
    print(f"假应用 {len(APPS)} 个：" + "、".join(a.name for a in APPS))
    print()

    passed = failed = 0
    for note, query, want_status, want_name in CASES:
        result = match(query, APPS)
        got_name = result.best.record.name if result.best else None
        # want_name 留空表示"只要求状态对"，不校验具体匹配到哪一条
        ok = result.status == want_status and (want_name is None or got_name == want_name)
        mark = "[OK]" if ok else "[!!]"
        detail = f"{result.status}" + (f" -> {got_name}" if got_name else "")
        if not ok:
            detail += f"   （期望 {want_status}" + (f" -> {want_name}" if want_name else "") + "）"
        print(f"{mark} {note:<24} 「{query}」  {detail}")
        if result.candidates and result.status == "ambiguous":
            for c in result.candidates:
                print(f"        · {c.record.name}（{c.score:.2f}，{c.reason}）")
        if ok:
            passed += 1
        else:
            failed += 1

    # 手工配置的同名应用必须压过其它来源（这是阶段二暴露过的真实 bug：
    # 手工加分被 min(value, 1.0) 抹平后，和商店应用打成 1.0 平手）。
    print()
    print("手工配置优先（同名压过其它来源）：")
    manual_notepad = rec("记事本", aliases=["notepad"],
                         source=Source.MANUAL, target=r"C:\Windows\System32\notepad.exe")
    uwp_notepad = rec("记事本", source=Source.UWP, kind=LaunchKind.AUMID,
                      target="Microsoft.WindowsNotepad_8wekyb3d8bbwe!App")
    result = match("记事本", [uwp_notepad, manual_notepad])
    ok = result.status == "unique" and result.best is not None \
        and result.best.record.source is Source.MANUAL
    print(f"  {'[OK]' if ok else '[!!]'} 同名时手工配置胜出 -> "
          f"{result.best.record.source.value if result.best else None}"
          f"（{result.best.score:.2f}）" if result.best else "")
    passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)

    # 归一化本身也要对
    print()
    print("归一化检查：")
    for raw, want in [("ＮＯＴＥＰＡＤ", "notepad"), ("VS_Code", "vs code"),
                      ("notepad.exe", "notepad"), ("  记事本  ", "记事本"),
                      ("A-B", "a b")]:
        got = normalize(raw)
        ok = got == want
        print(f"  {'[OK]' if ok else '[!!]'} {raw!r:<20} -> {got!r}")
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)

    # search() 是给模型用的：只排序，不判断唯不唯一
    print()
    print("搜索排序检查（只排序，不判断唯一）：")
    hits = search("code", APPS)
    order = [c.record.name for c in hits]
    ok = bool(hits) and order[0] == "Visual Studio Code"
    print(f"  {'[OK]' if ok else '[!!]'} search('code') -> {order}")
    passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)

    print()
    print("=" * 64)
    total = passed + failed
    if failed:
        print(f"结果：{passed}/{total} 通过，{failed} 项不通过")
        return 1
    print(f"结果：{passed}/{total} 全部通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
