"""只读文件理解（read_text_file / search_text_files）的离线测试。

覆盖：
  正常读取、正常搜索、路径/行号、缺失文件、目录外、.. 越界、
  符号链接/目录联接越界、.env/凭据/私钥、二进制/编码、大小与搜索上限、
  用户拒绝、读取异常、模型伪造注入参数、授权后文件变化、
  搜索不跟随链接、敏感内容不落库、工作流仍走同一授权闸门。

所有文件都在系统临时目录里新建，不读取、不依赖你的真实私人文件。
不联网、不启动应用、不做桌面操作、不开启真实输入。

用法：
    .venv\\Scripts\\python.exe scripts\\test_file_reading.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import gate, pending  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.tools import read_policy as rp  # noqa: E402
from app.tools.read_tools import (  # noqa: E402
    ReadTextFileTool,
    SearchTextFilesTool,
)

RESULTS: list[tuple[str, bool, str]] = []
SESSION = f"test-file-reading-{os.getpid()}-{time.time_ns()}"


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


def note(text: str) -> None:
    print(f"  [--] {text}")


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------
class CountingPolicy(rp.ReadAccessPolicy):
    """记录真正打开/读取文件的次数，用来证明拒绝/参数非法时没有读正文。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reads = 0

    def read_text_verified(self, target, *, max_bytes=None):  # noqa: ANN001
        self.reads += 1
        return super().read_text_verified(target, max_bytes=max_bytes)


class BrokenReadPolicy(rp.ReadAccessPolicy):
    """模拟文件被占用/没有权限。"""

    def read_text_verified(self, target, *, max_bytes=None):  # noqa: ANN001
        raise rp.ReadFailed("模拟：文件被占用，读取失败")


class CountingReadTool(ReadTextFileTool):
    def __init__(self, access_policy: rp.ReadAccessPolicy | None = None) -> None:
        super().__init__(access_policy)
        self.runs = 0

    async def run(self, **kwargs: Any):
        self.runs += 1
        return await super().run(**kwargs)


class CountingSearchTool(SearchTextFilesTool):
    def __init__(self, access_policy: rp.ReadAccessPolicy | None = None) -> None:
        super().__init__(access_policy)
        self.runs = 0

    async def run(self, **kwargs: Any):
        self.runs += 1
        return await super().run(**kwargs)


def policy_for(root: Path, **overrides: Any) -> CountingPolicy:
    values: dict[str, Any] = {
        "allowed_roots": [str(root)],
        "blocked_paths": [str(root / "blocked")],
        "max_file_bytes": 1000,
        "max_read_output_chars": 10_000,
        "max_search_files": 50,
        "max_search_results": 20,
        "max_search_file_bytes": 1000,
        "max_search_output_chars": 10_000,
        "max_context_lines": 3,
        "max_line_chars": 200,
        "max_query_chars": 50,
        "max_path_chars": 4096,
    }
    values.update(overrides)
    return CountingPolicy(**values)


async def prepare_and_run(tool, args: dict[str, Any]):
    """直接走工具自己的 prepare → execute（不经过确认，用于内容/边界测试）。"""
    prepared = await tool.prepare(args)
    if prepared.blocked:
        return None, prepared
    result = await tool.execute(args, injected=prepared.injected)
    return result, prepared


async def run_gate(tool, args: dict[str, Any], *, approve: bool):
    """走真实 gate.authorize_tool；和工作流引擎使用的是同一份代码。"""
    outcome = gate.GateOutcome()
    events: list[dict[str, Any]] = []
    answered: set[str] = set()

    async def answer_when_asked() -> None:
        while True:
            await asyncio.sleep(0.005)
            for action in pending.store.pending_for(SESSION):
                if action.id in answered:
                    continue
                answered.add(action.id)
                pending.store.answer(action.id, approve)

    answerer = asyncio.create_task(answer_when_asked())
    try:
        async for event in gate.authorize_tool(
            tool, args, session_id=SESSION, outcome=outcome, timeout=1.0
        ):
            events.append(event)
    finally:
        answerer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await answerer

    result = None
    if outcome.proceed:
        result = await tool.execute(args, injected=outcome.injected)
    return outcome, events, result


# ----------------------------------------------------------------------
# 临时目录里的链接（只在测试目录中创建，不执行任何文件内容）
# ----------------------------------------------------------------------
def try_dir_link(link: Path, target: Path) -> str:
    """先试符号链接；Windows 没有权限时退到目录联接（mklink /J）。"""
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    if os.name != "nt":
        return ""
    try:
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and link.exists():
            return "junction"
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def try_file_link(link: Path, target: Path) -> bool:
    try:
        os.symlink(str(target), str(link))
        return True
    except (OSError, NotImplementedError):
        return False


# ----------------------------------------------------------------------
def create_layout(base: Path) -> dict[str, Path]:
    root = base / "allowed"
    outside = base / "outside"
    root.mkdir()
    outside.mkdir()

    notes = root / "notes.txt"
    notes.write_text("第一行\nneedle here\n第三行\n", encoding="utf-8")

    (root / "sub").mkdir()
    (root / "sub" / "more.txt").write_text(
        "alpha\nNEEDLE upper\nomega\n", encoding="utf-8"
    )

    # 故意不叫 secret.txt：文件名里的 secret 会先命中"敏感名称"规则，
    # 那样就测不到"目录外"这条边界了。
    (outside / "outside.txt").write_text(
        "OUTSIDE_SECRET_SHOULD_NOT_LEAK\n", encoding="utf-8"
    )
    (root / "blocked").mkdir()
    (root / "blocked" / "hidden.txt").write_text("blocked content\n", encoding="utf-8")

    # 敏感名称/目录/内容
    (root / ".env").write_text("JIA_SECRET=do-not-read\n", encoding="utf-8")
    (root / "keys.txt").write_text("plain looking but sensitive\n", encoding="utf-8")
    (root / "credentials.json").write_text('{"token":"nope"}\n', encoding="utf-8")
    (root / "server.pem").write_text("not a real key\n", encoding="utf-8")
    (root / "token.json").write_text('{"token":"nope"}\n', encoding="utf-8")
    (root / "secrets").mkdir()
    (root / "secrets" / "hidden.txt").write_text("secret dir\n", encoding="utf-8")
    (root / "fake_key.txt").write_text(
        "-----BEGIN PRIVATE KEY-----\nMIIBfake\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )

    # 二进制 / 非 UTF-8
    (root / "binary.dat").write_bytes(b"\x00\x01\x02binary")
    (root / "gbk.txt").write_bytes(b"\xd6\xd0\xce\xc4")

    # 依赖/缓存目录
    for dirname in (".git", "node_modules", "__pycache__"):
        folder = root / dirname
        folder.mkdir()
        (folder / "hidden.txt").write_text("SECRET_IN_CACHE\n", encoding="utf-8")

    # 专门给"结果上限"测试用的小目录：两个文件、五处命中。
    limits = root / "limits"
    limits.mkdir()
    (limits / "a.txt").write_text(
        "needle one\nneedle two\nneedle three\n", encoding="utf-8"
    )
    (limits / "b.txt").write_text(
        "needle four\nneedle five\n", encoding="utf-8"
    )

    return {"root": root, "outside": outside, "notes": notes, "limits": limits}


# ----------------------------------------------------------------------
async def main_async() -> None:
    # 系统临时目录在 C:\Users\...\AppData\Local\Temp；AppData 是受保护目录，
    # 读取策略会（正确地）拒绝把它当成读取根。测试需要在项目目录下建一个
    # 独立临时目录，这样既走真实的白名单逻辑，又不碰任何私人文件。
    project_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(
        prefix=".jia-read-test-", dir=project_root
    ) as tmp:
        base = Path(tmp)
        paths = create_layout(base)
        root, outside = paths["root"], paths["outside"]
        notes, limits = paths["notes"], paths["limits"]

        # ================= 0. 空白名单 / 过宽白名单 =================
        print("\n[0] 白名单失败关闭")
        empty = rp.ReadAccessPolicy(allowed_roots=[])
        try:
            empty.resolve_file(str(notes))
            empty_ok = False
        except rp.NoAllowedRoots:
            empty_ok = True
        check("allowed_roots 为空时拒绝读取", empty_ok)

        broad = rp.ReadAccessPolicy(allowed_roots=[str(Path(tmp).anchor)])
        check("整盘根目录不会被当成有效读取根", broad._roots == (), str(broad._roots))
        try:
            broad.resolve_file(str(notes))
            broad_ok = False
        except rp.NoAllowedRoots:
            broad_ok = True
        check("过宽白名单最终仍是拒绝，而不是自动放宽", broad_ok)

        home_policy = rp.ReadAccessPolicy(allowed_roots=[str(Path.home())])
        check("整个用户主目录不会被当成有效读取根",
              home_policy._roots == (), str(home_policy._roots))

        # ================= 1. 正常读取 =================
        print("\n[1] 正常读取")
        policy = policy_for(root)
        tool = CountingReadTool(policy)
        prepared = await tool.prepare({"path": str(notes)})
        check("prepare 不读正文", policy.reads == 0 and not prepared.blocked,
              f"reads={policy.reads}")
        check("prepare 固定真实路径",
              Path(prepared.injected["resolved_path"]) == notes.resolve())
        check("prepare 固定文件指纹",
              prepared.injected["file_fingerprint"]["size"] == notes.stat().st_size)

        result = await tool.execute({"path": str(notes)}, injected=prepared.injected)
        check("读取成功", result.ok and result.status == "read_ok", result.status)
        check("返回真实路径", str(notes.resolve()) in result.output)
        check("返回正文", "needle here" in result.output)
        check("带行号和行号对应", "2 | needle here" in result.output)
        check("返回编码和大小",
              "UTF-8" in result.output and "字节" in result.output)
        check("落库摘要不含正文",
              "needle here" not in result.as_persist_text(),
              result.as_persist_text())

        no_numbers = await tool.execute(
            {"path": str(notes), "include_line_numbers": False},
            injected=prepared.injected,
        )
        check("可以关闭行号", no_numbers.ok and "2 | needle here" not in no_numbers.output)

        empty_file = root / "empty.txt"
        empty_file.write_text("", encoding="utf-8")
        empty_result, _ = await prepare_and_run(tool, {"path": str(empty_file)})
        check("空文件明确说明没有正文",
              empty_result is not None and empty_result.ok
              and "空文件" in empty_result.output)

        # ================= 2. 正常搜索 =================
        print("\n[2] 正常搜索")
        search_policy = policy_for(root)
        search = CountingSearchTool(search_policy)
        search_prepared = await search.prepare(
            {"path": str(root), "query": "needle"}
        )
        check("搜索 prepare 不读正文",
              search_policy.reads == 0 and not search_prepared.blocked,
              f"reads={search_policy.reads}")
        search_result = await search.execute(
            {"path": str(root), "query": "needle"},
            injected=search_prepared.injected,
        )
        check("搜索成功", search_result is not None and search_result.ok
              and search_result.status == "search_ok", search_result.status)
        check("返回匹配文件路径", str(notes.resolve()) in search_result.output)
        check("返回行号", f"{notes.resolve()}:2" in search_result.output)
        check("包含命中行", "2 | needle here" in search_result.output)
        check("包含有限上下文",
              "第一行" in search_result.output and "第三行" in search_result.output)
        check("落库摘要不含命中正文",
              "needle here" not in search_result.as_persist_text(),
              search_result.as_persist_text())

        upper_default, _ = await prepare_and_run(
            search, {"path": str(root), "query": "needle", "case_sensitive": False}
        )
        check("默认不区分大小写", "NEEDLE upper" in upper_default.output)
        upper_sensitive, _ = await prepare_and_run(
            search, {"path": str(root), "query": "needle", "case_sensitive": True}
        )
        check("可以区分大小写", "NEEDLE upper" not in upper_sensitive.output)

        no_context, _ = await prepare_and_run(
            search, {"path": str(root), "query": "needle", "context_lines": 0}
        )
        check("context_lines=0 只显示命中行",
              "第一行" not in no_context.output
              and "2 | needle here" in no_context.output)

        # 缓存/依赖目录不进入。查询词用 "CACHE" 而不是整句标记，
        # 否则输出标题里回显的 query 会让"标记没有泄漏"的断言误报。
        cache_result, _ = await prepare_and_run(
            search, {"path": str(root), "query": "CACHE"}
        )
        check("搜索不返回 .git/node_modules/__pycache__ 内容",
              cache_result.ok and "SECRET_IN_CACHE" not in cache_result.output)
        check("搜索结果报告跳过依赖/缓存目录",
              "依赖/缓存目录" in cache_result.output, cache_result.output[:300])

        # ================= 3. 缺失 / 越界 / 遍历 =================
        print("\n[3] 缺失、目录外、.. 越界")
        missing, missing_prep = await prepare_and_run(
            tool, {"path": str(root / "nope.txt")}
        )
        check("缺失文件在确认前停止",
              missing is None and missing_prep.blocked_code == "file_not_found",
              missing_prep.blocked_code)

        outside_res, outside_prep = await prepare_and_run(
            tool, {"path": str(outside / "outside.txt")}
        )
        check("目录外文件被拒绝",
              outside_res is None and outside_prep.blocked_code == "path_not_allowed",
              outside_prep.blocked_code)

        traversal = root / "sub" / ".." / ".." / "outside" / "outside.txt"
        traversal_res, traversal_prep = await prepare_and_run(
            tool, {"path": str(traversal)}
        )
        check("用 .. 越界被拒绝",
              traversal_res is None and traversal_prep.blocked_code == "path_not_allowed",
              traversal_prep.blocked_code)

        inside_traversal, inside_prep = await prepare_and_run(
            tool, {"path": str(root / "sub" / ".." / "notes.txt")}
        )
        check("绕一圈仍回到允许目录可以读",
              inside_traversal is not None and inside_traversal.ok)

        blocked_res, blocked_prep = await prepare_and_run(
            tool, {"path": str(root / "blocked" / "hidden.txt")}
        )
        check("blocked_paths 规则生效",
              blocked_res is None and blocked_prep.blocked_code == "path_not_allowed",
              blocked_prep.blocked_code)

        # ================= 4. 链接/目录联接 =================
        print("\n[4] 符号链接 / 目录联接")
        link_kind = try_dir_link(root / "link_out", outside)
        if link_kind:
            note(f"已创建越界目录链接（{link_kind}）")
            linked_res, linked_prep = await prepare_and_run(
                tool, {"path": str(root / "link_out" / "outside.txt")}
            )
            check("通过链接读取目录外文件被拒绝",
                  linked_res is None and linked_prep.blocked_code == "path_not_allowed",
                  linked_prep.blocked_code)

            linked_search, _ = await prepare_and_run(
                search, {"path": str(root), "query": "OUTSIDE"}
            )
            check("搜索不跟随越界目录链接",
                  linked_search.ok
                  and "OUTSIDE_SECRET_SHOULD_NOT_LEAK" not in linked_search.output
                  and str(outside / "outside.txt") not in linked_search.output)
            check("搜索结果报告链接被跳过",
                  "链接/联接" in linked_search.output, linked_search.output[:300])
        else:
            note("当前环境无法创建目录符号链接/联接，链接越界测试未执行 [未验证]")

        file_link_ok = try_file_link(root / "notes_link.txt", root / ".env")
        if file_link_ok:
            note("已创建文件符号链接（notes_link.txt -> .env）")
            link_file_res, link_file_prep = await prepare_and_run(
                tool, {"path": str(root / "notes_link.txt")}
            )
            check("指向敏感文件的链接也被拒绝",
                  link_file_res is None
                  and link_file_prep.blocked_code == "sensitive_file",
                  link_file_prep.blocked_code)
        else:
            note("当前环境无法创建文件符号链接，敏感链接测试未执行 [未验证]")

        # ================= 5. 敏感文件 =================
        print("\n[5] 敏感文件/凭据/私钥")
        sensitive_cases = [
            (root / ".env", "sensitive_file", ".env"),
            (root / "keys.txt", "sensitive_file", "keys.txt"),
            (root / "credentials.json", "sensitive_file", "credentials.json"),
            (root / "server.pem", "sensitive_file", "server.pem"),
            (root / "token.json", "sensitive_file", "token.json"),
            (root / "secrets" / "hidden.txt", "sensitive_file", "secrets/ 目录"),
        ]
        for path, code, label in sensitive_cases:
            res, prep = await prepare_and_run(tool, {"path": str(path)})
            check(f"{label} 被拒绝", res is None and prep.blocked_code == code,
                  prep.blocked_code)

        fake_key_res, fake_key_prep = await prepare_and_run(
            tool, {"path": str(root / "fake_key.txt")}
        )
        check("私钥内容不会被当成正文返回",
              fake_key_res is not None and fake_key_res.ok is False
              and fake_key_res.status == "sensitive_file"
              and "BEGIN PRIVATE KEY" not in fake_key_res.as_text(),
              fake_key_res.status if fake_key_res else fake_key_prep.blocked_code)

        # ================= 6. 二进制 / 编码 =================
        print("\n[6] 二进制与编码")
        binary_res, _ = await prepare_and_run(tool, {"path": str(root / "binary.dat")})
        check("二进制文件被拒绝",
              binary_res is not None and binary_res.ok is False
              and binary_res.status == "binary_file", binary_res.status)

        gbk_res, _ = await prepare_and_run(tool, {"path": str(root / "gbk.txt")})
        check("非 UTF-8 编码被拒绝",
              gbk_res is not None and gbk_res.ok is False
              and gbk_res.status == "unsupported_encoding", gbk_res.status)

        # ================= 7. 大小/搜索上限 =================
        print("\n[7] 文件大小与搜索上限")
        big = root / "big.txt"
        big.write_text("x" * 1200, encoding="utf-8")
        big_res, big_prep = await prepare_and_run(tool, {"path": str(big)})
        check("超过单文件上限在确认前停止",
              big_res is None and big_prep.blocked_code == "file_too_large",
              big_prep.blocked_code)

        big_search, _ = await prepare_and_run(
            search, {"path": str(root), "query": "x"}
        )
        check("搜索跳过过大文件并报告",
              "过大 1" in big_search.output, big_search.output[:400])

        result_policy = policy_for(root, max_search_results=2)
        result_tool = SearchTextFilesTool(result_policy)
        limited, _ = await prepare_and_run(
            result_tool, {"path": str(limits), "query": "needle"}
        )
        check("命中数上限会截断并明确说明",
              limited.status == "search_truncated"
              and "命中数上限 2" in limited.output, limited.status)

        file_policy = policy_for(root, max_search_files=1)
        file_tool = SearchTextFilesTool(file_policy)
        file_limited, _ = await prepare_and_run(
            file_tool, {"path": str(limits), "query": "needle"}
        )
        check("文件数上限会截断并明确说明",
              file_limited.status == "search_truncated"
              and "文件数上限 1" in file_limited.output, file_limited.status)

        output_policy = policy_for(root, max_search_output_chars=500)
        output_tool = SearchTextFilesTool(output_policy)
        output_limited, _ = await prepare_and_run(
            output_tool, {"path": str(limits), "query": "needle"}
        )
        check("输出长度上限会截断并明确说明",
              output_limited.status == "search_truncated"
              and "输出长度上限" in output_limited.output, output_limited.status)

        # ================= 8. 用户拒绝 =================
        print("\n[8] 用户拒绝")
        reject_policy = policy_for(root)
        reject_tool = CountingReadTool(reject_policy)
        outcome, events, gate_result = await run_gate(
            reject_tool, {"path": str(notes)}, approve=False
        )
        check("拒绝后有确认卡片", len(events) == 1
              and events[0]["type"] == "need_confirm")
        check("拒绝后没有放行", outcome.proceed is False)
        check("拒绝后没有执行结果", gate_result is None and reject_tool.runs == 0)
        check("拒绝后没有读取正文", reject_policy.reads == 0)
        check("结果明确是用户拒绝", outcome.status == "rejected", outcome.status)

        reject_search_tool = CountingSearchTool(policy_for(root))
        outcome2, events2, result2 = await run_gate(
            reject_search_tool, {"path": str(root), "query": "needle"}, approve=False
        )
        check("搜索拒绝后也没有执行",
              outcome2.proceed is False and reject_search_tool.runs == 0
              and result2 is None)

        # ================= 9. 真实授权卡片 =================
        print("\n[9] 真实授权卡片与隐私提示")
        approve_policy = policy_for(root)
        approve_tool = CountingReadTool(approve_policy)
        outcome3, events3, result3 = await run_gate(
            approve_tool, {"path": str(notes)}, approve=True
        )
        check("授权后读取成功",
              outcome3.proceed is True and result3 is not None and result3.ok)
        check("卡片显示真实文件路径", str(notes.resolve()) in outcome3.summary,
              outcome3.summary)
        check("卡片提示内容可能发给模型提供商",
              "模型提供商" in outcome3.summary, outcome3.summary)
        check("卡片有确认事件", len(events3) == 1
              and events3[0]["action"]["kind"] == "confirm")

        search_approve_tool = CountingSearchTool(policy_for(root))
        outcome4, events4, result4 = await run_gate(
            search_approve_tool, {"path": str(root), "query": "needle"}, approve=True
        )
        check("搜索也需要确认并显示目录/关键字",
              outcome4.proceed is True
              and str(root.resolve()) in outcome4.summary
              and "needle" in outcome4.summary,
              outcome4.summary)
        check("搜索授权后命中", result4 is not None and result4.ok
              and "needle here" in result4.output)
        check("工具标记为不可自动重试",
              ReadTextFileTool.retry_safe is False
              and SearchTextFilesTool.retry_safe is False
              and ReadTextFileTool.requires_prepared_target is True
              and SearchTextFilesTool.requires_prepared_target is True)

        # ================= 10. 读取异常 =================
        print("\n[10] 读取异常")
        broken = CountingReadTool(BrokenReadPolicy(
            allowed_roots=[str(root)],
            blocked_paths=[],
            max_file_bytes=1000,
            max_search_file_bytes=1000,
        ))
        broken_row, _ = await prepare_and_run(broken, {"path": str(notes)})
        check("读取异常如实失败",
              broken_row is not None and broken_row.ok is False
              and broken_row.status == "read_failed"
              and "模拟" in broken_row.as_text(), broken_row.status)

        # ================= 11. 模型伪造 / 注入覆盖 =================
        print("\n[11] 模型不能伪造服务端参数")
        forge_policy = policy_for(root)
        forge_tool = CountingReadTool(forge_policy)
        forged = await forge_tool.execute({
            "path": str(notes),
            "resolved_path": str(outside / "secret.txt"),
        })
        check("模型伪造 resolved_path 被参数校验拒绝",
              forged.ok is False and forged.status == "invalid_argument"
              and forge_policy.reads == 0, forged.status)

        forged_fp = await forge_tool.execute({
            "path": str(notes),
            "file_fingerprint": {"path": str(notes), "size": 0,
                                 "mtime_ns": 0, "dev": 0, "ino": 0},
        })
        check("模型伪造指纹被参数校验拒绝",
              forged_fp.ok is False and forged_fp.status == "invalid_argument"
              and forge_policy.reads == 0, forged_fp.status)

        missing_injected = await forge_tool.execute({"path": str(notes)})
        check("没有服务端注入时拒绝读取",
              missing_injected.ok is False and forge_policy.reads == 0)

        bad_bool = await forge_tool.execute({
            "path": str(notes), "include_line_numbers": "yes",
        })
        check("布尔参数不做字符串猜测",
              bad_bool.ok is False and bad_bool.status == "invalid_argument")

        other = root / "other.txt"
        other.write_text("other content\n", encoding="utf-8")
        prepared_a = await forge_tool.prepare({"path": str(notes)})
        # 即使模型在 execute 时把 path 换成别的文件，run 也只认服务端注入的
        # resolved_path。这里直接传 injected，模拟闸门放行后的真实调用。
        redirected = await forge_tool.execute(
            {"path": str(other)}, injected=prepared_a.injected
        )
        check("执行目标只认服务端固定的路径",
              redirected.ok and "needle here" in redirected.output
              and "other content" not in redirected.output)

        search_forge = SearchTextFilesTool(policy_for(root))
        forged_search = await search_forge.execute({
            "path": str(root), "query": "needle",
            "resolved_dir": str(outside),
        })
        check("搜索同样拒绝伪造的 resolved_dir",
              forged_search.ok is False
              and forged_search.status == "invalid_argument", forged_search.status)

        # ================= 12. 授权后文件变化 =================
        print("\n[12] 授权后文件变化")
        changing = root / "changing.txt"
        changing.write_text("old content\n", encoding="utf-8")
        change_policy = policy_for(root)
        change_tool = CountingReadTool(change_policy)
        change_prep = await change_tool.prepare({"path": str(changing)})
        changing.write_text("new content with different size\n", encoding="utf-8")
        changed = await change_tool.execute(
            {"path": str(changing)}, injected=change_prep.injected
        )
        check("授权后文件被修改会停止",
              changed.ok is False and changed.status == "file_changed",
              changed.status)
        check("变化时没有返回任何正文",
              "old content" not in changed.as_text()
              and "new content" not in changed.as_text())

        deleted = root / "deleted.txt"
        deleted.write_text("to be deleted\n", encoding="utf-8")
        deleted_prep = await change_tool.prepare({"path": str(deleted)})
        deleted.unlink()
        deleted_result = await change_tool.execute(
            {"path": str(deleted)}, injected=deleted_prep.injected
        )
        check("授权后文件被删除会停止",
              deleted_result.ok is False
              and deleted_result.status == "file_not_found", deleted_result.status)

        # ================= 13. 工作流仍走同一闸门 =================
        print("\n[13] 工作流授权闸门")
        engine_source = (Path(__file__).resolve().parents[1]
                         / "app" / "workflow" / "engine.py").read_text(
                             encoding="utf-8"
                         )
        check("工作流引擎使用 gate.authorize_tool",
              "gate.authorize_tool" in engine_source)
        check("工作流只在 outcome.proceed 后执行工具",
              "if not outcome.proceed" in engine_source
              and "tool.execute" in engine_source)
        check("新工具默认需要用户确认",
              ReadTextFileTool.requires_confirmation is True
              and SearchTextFilesTool.requires_confirmation is True)

        # 直接走一次 gateway 的拒绝路径，确认搜索/读取都不会偷偷执行
        wf_policy = policy_for(root)
        wf_tool = CountingSearchTool(wf_policy)
        wf_outcome, wf_events, wf_result = await run_gate(
            wf_tool, {"path": str(root), "query": "needle"}, approve=False
        )
        check("工作流同一闸门拒绝后不执行",
              wf_outcome.proceed is False and wf_tool.runs == 0
              and wf_result is None and wf_policy.reads == 0)


def main() -> int:
    setup_logging("ERROR")
    print("=" * 74)
    print("只读文件理解：离线安全测试（临时目录，不碰真实私人文件）")
    print("=" * 74)
    asyncio.run(main_async())

    print()
    print("=" * 74)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    if passed != total:
        print(f"结果：{passed}/{total} 通过，{total - passed} 项不通过")
        for item, ok, detail in RESULTS:
            if not ok:
                print(f"  [!!] {item}　{detail}")
        return 1
    print(f"结果：{passed}/{total} 全部通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
