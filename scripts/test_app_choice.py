"""多候选选择测试（阶段三）。

不联网、不启动任何程序、不依赖模型——直接验证「命中多个候选 → 请用户挑一个」
这条链路上的每个零件，以及它的安全底线。

重点守住三件事：
  1. prepare() 唯一命中时不打扰用户；有歧义时才给出候选
  2. choose() 只接受这个待办自己列出来的编号，伪造/猜测的一律拒
  3. 服务端注入的 resolved_id，模型自己塞会被判成非法参数

用法：
  .venv\\Scripts\\python.exe scripts\\test_app_choice.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import pending  # noqa: E402
from app.apps import index  # noqa: E402
from app.apps.model import AppRecord, LaunchKind, Source  # noqa: E402
from app.tools.app_tools import OpenApplicationTool  # noqa: E402
from app.tools.base import ToolError  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, ok, detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


# 一批造出来的应用：前两个近名（会歧义），第三个独一无二
FAKE = [
    AppRecord(name="示例工具", source=Source.START_MENU_ALL, launch_kind=LaunchKind.EXE,
              target=r"D:\Fake\demo-a.exe", identity_key=r"D:\Fake\demo-a.exe",
              resolved=r"D:\Fake\demo-a.exe"),
    AppRecord(name="示例工具助手", source=Source.START_MENU_ALL, launch_kind=LaunchKind.EXE,
              target=r"D:\Fake\demo-b.exe", identity_key=r"D:\Fake\demo-b.exe",
              resolved=r"D:\Fake\demo-b.exe"),
    AppRecord(name="独一无二应用", source=Source.START_MENU_ALL, launch_kind=LaunchKind.EXE,
              target=r"D:\Fake\only.exe", identity_key=r"D:\Fake\only.exe",
              resolved=r"D:\Fake\only.exe"),
]


class _FakeIndex:
    """临时把索引换成上面那批假数据，好让"歧义"这件事稳定复现。"""

    def __enter__(self):
        self._ensure = index.ensure_ready
        self._load = index.load_all
        index.ensure_ready = lambda *a, **k: {}
        index.load_all = lambda *a, **k: list(FAKE)
        return self

    def __exit__(self, *exc):
        index.ensure_ready = self._ensure
        index.load_all = self._load
        return False


async def main_async() -> int:
    print("=" * 68)
    print("多候选选择测试")
    print("=" * 68)
    tool = OpenApplicationTool()

    # ---------------- prepare() ----------------
    print()
    print("prepare()：什么时候该问用户，什么时候不该：")
    with _FakeIndex():
        unique = await tool.prepare({"app": "独一无二应用"})
        check("唯一命中时直接就绪，不打扰用户", not unique.needs_choice)

        vague = await tool.prepare({"app": "示例"})
        check("有歧义时给出候选", vague.needs_choice,
              f"{len(vague.choices)} 个候选")
        check("候选里带着问题", bool(vague.question), vague.question)
        check("每个候选都有 id 和名字",
              all(c.get("id") and c.get("name") for c in vague.choices))
        check("候选里能看到真实目标（点头前能核对）",
              all(c.get("target") for c in vague.choices),
              " / ".join(c["target"] for c in vague.choices))

        missing = await tool.prepare({"app": "完全不存在的东西"})
        check("一个都没找到时不弹候选（交给 run 报错）", not missing.needs_choice)

        empty = await tool.prepare({"app": ""})
        check("空名字不弹候选（交给 validate 报错）", not empty.needs_choice)

    # ---------------- 选择机制 ----------------
    print()
    print("选择机制（pending.store）：")
    action = pending.store.create_choice(
        session_id="test-session",
        tool_name="open_application",
        tool_label="打开应用",
        question="你要打开哪一个？",
        options=[{"id": "id-a", "name": "甲"}, {"id": "id-b", "name": "乙"}],
    )
    public = action.to_public_dict()
    check("待办的 kind 是 choose", public["kind"] == "choose")
    check("候选原样带给网页", [o["id"] for o in public["options"]] == ["id-a", "id-b"])

    check("不存在的待办编号会被拒", pending.store.choose("这个编号不存在", "id-a") is None)
    check("不在候选里的编号会被拒", pending.store.choose(action.id, "id-zzz") is None,
          "伪造的编号必须拒掉")

    ok_pick = pending.store.choose(action.id, "id-b")
    check("合法编号能被接受", ok_pick is not None and ok_pick.chosen == "id-b")
    picked = await pending.store.wait_choice(action, timeout=1.0)
    check("wait_choice 拿到用户选的那个", picked == "id-b", picked)

    # 没人选（超时）应当如实返回空
    lonely = pending.store.create_choice(
        session_id="test-session", tool_name="open_application",
        tool_label="打开应用", question="你要哪一个？",
        options=[{"id": "id-x", "name": "X"}],
    )
    none_picked = await pending.store.wait_choice(lonely, timeout=0.2)
    check("没人选时如实返回空（当作没选）", none_picked == "", repr(none_picked))

    # ---------------- 安全底线 ----------------
    print()
    print("安全底线：")
    try:
        tool.validate({"app": "记事本", "resolved_id": "deadbeef"})
        rejected = False
    except ToolError:
        rejected = True
    check("模型自己在参数里塞 resolved_id 会被判非法", rejected)

    check("resolved_id 不在工具的公开 schema 里",
          "resolved_id" not in tool.parameters["properties"],
          "参数只有：" + "、".join(tool.parameters["properties"]))

    real = index.load_all()   # 真实索引
    if real:
        target = real[0]
        shown = tool.describe_action({"app": "这个名字根本不存在xyz", "resolved_id": target.id})
        check("服务端注入后，卡片显示的是用户选的那条记录",
              target.name in shown, shown[:64])

    return 0


def main() -> int:
    asyncio.run(main_async())

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
