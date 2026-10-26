"""CC 人工交接的离线测试。

覆盖：正常准备/验证、用户拒绝、敏感/缓存/链接/日志/二进制过滤、
文件数与大小上限、路径越界、授权后文件变化、任务包被篡改、
错误 schema/task_id/status、越权文件引用、伪造成功、结果过大、
结果在确认后变化、模型伪造服务端字段、真实鼠标开关未被改动。

所有源文件都在项目目录下的独立临时目录里新建；不读取真实私人文件，
不联网、不启动 CC、不调用模型、不执行任何项目脚本、不产生桌面操作。

用法：
    .venv\\Scripts\\python.exe scripts\\test_cc_handoff.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import gate, pending  # noqa: E402
from app.cc import protocol, service as cc_service, snapshot  # noqa: E402
from app.cc.service import CcService  # noqa: E402
from app.settings import CcConfig, get_tools_config  # noqa: E402
from app.tools import read_policy as rp  # noqa: E402
from app.tools.cc_tools import PrepareCcHandoffTool, ValidateCcResultTool  # noqa: E402
from app.tools.registry import registry  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
SESSION = f"test-cc-handoff-{os.getpid()}-{time.time_ns()}"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


def note(text: str) -> None:
    print(f"  [--] {text}")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ----------------------------------------------------------------------
# 临时目录、链接与测试替身
# ----------------------------------------------------------------------
def try_dir_link(link: Path, target: Path) -> str:
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


class CountingValidateService(CcService):
    """记录真正执行结果验证的次数，用来证明用户拒绝时没有读取结果。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.validations = 0

    def validate_result(self, **kwargs: Any):  # noqa: ANN003
        self.validations += 1
        return super().validate_result(**kwargs)


async def run_gate(tool, args: dict[str, Any], *, approve: bool, suffix: str = ""):
    """走真实 gate.authorize_tool；和工作流引擎使用的是同一份代码。"""
    session = f"{SESSION}-{suffix or time.time_ns()}"
    outcome = gate.GateOutcome()
    events: list[dict[str, Any]] = []
    answered: set[str] = set()

    async def answer_when_asked() -> None:
        while True:
            await asyncio.sleep(0.005)
            for action in pending.store.pending_for(session):
                if action.id in answered:
                    continue
                answered.add(action.id)
                pending.store.answer(action.id, approve)

    answerer = asyncio.create_task(answer_when_asked())
    try:
        async for event in gate.authorize_tool(
            tool, args, session_id=session, outcome=outcome, timeout=1.0
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


async def make_task(tool: PrepareCcHandoffTool, paths: list[Path], goal: str) -> tuple[str, Any]:
    args = {"goal": goal, "paths": [str(path) for path in paths]}
    prepared = await tool.prepare(args)
    if prepared.blocked:
        raise AssertionError(f"prepare unexpectedly blocked: {prepared.blocked}")
    result = await tool.execute(args, injected=prepared.injected)
    if not result.ok:
        raise AssertionError(f"task creation failed: {result.error}")
    return prepared.injected["task_id"], result


async def validate_direct(tool: ValidateCcResultTool, task_id: str):
    prepared = await tool.prepare({"task_id": task_id})
    if prepared.blocked:
        return None, prepared
    result = await tool.execute({"task_id": task_id}, injected=prepared.injected)
    return result, prepared


def write_result(handoff: Path, task_id_value: str, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id_value,
        "status": "succeeded",
        "summary": "订单状态从 CREATED 经由 PAID 到 SHIPPED；异常路径缺少回滚。",
        "files": [{"path": "root_1/src/Main.java", "note": "状态流转入口"}],
        "error": "",
        "finished_at": now_iso(),
        "truncated": False,
    }
    data.update(overrides)
    (handoff / "result.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return data


def make_config(root: Path, **overrides: Any) -> CcConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "allowed_roots": [str(root)],
        "blocked_paths": [str(root / "blocked")],
        "max_files": 10,
        "max_total_bytes": 5_000,
        "max_file_bytes": 2_000,
        "max_result_bytes": 5_000,
        "max_paths": 10,
        "max_goal_chars": 500,
        "deadline_hours": 1,
    }
    values.update(overrides)
    return CcConfig(**values)


# ----------------------------------------------------------------------
# 主测试
# ----------------------------------------------------------------------
async def main() -> None:
    print("\n== CC 人工交接离线测试 ==")
    with tempfile.TemporaryDirectory(prefix=".jia-cc-test-", dir=PROJECT_ROOT) as tmp:
        base = Path(tmp)
        src = base / "src"
        src.mkdir()
        (src / "Main.java").write_text(
            "class Main { // state machine\n  // CREATED -> PAID -> SHIPPED\n}\n",
            encoding="utf-8",
        )
        (src / "sub").mkdir()
        (src / "sub" / "Util.java").write_text(
            "class Util { static String name = \"helper\"; }\n", encoding="utf-8"
        )
        (src / ".env").write_text("JIA_DEEPSEEK_API_KEY=SHOULD_NOT_LEAK\n", encoding="utf-8")
        (src / "keys.txt").write_text("SHOULD_NOT_LEAK_KEY\n", encoding="utf-8")
        (src / ".git").mkdir()
        (src / ".git" / "config").write_text("token = SHOULD_NOT_LEAK\n", encoding="utf-8")
        (src / "logs").mkdir()
        (src / "logs" / "app.log").write_text("SHOULD_NOT_LEAK_LOG\n", encoding="utf-8")
        (src / "node_modules").mkdir()
        (src / "node_modules" / "dep.js").write_text("SHOULD_NOT_LEAK_DEP\n", encoding="utf-8")
        (src / "binary.bin").write_bytes(b"\x00\x01SHOULD_NOT_LEAK_BIN")
        (src / "blocked").mkdir()
        (src / "blocked" / "Blocked.java").write_text("blocked\n", encoding="utf-8")
        outside = base / "outside"
        outside.mkdir()
        (outside / "outside.java").write_text("OUTSIDE_SENTINEL\n", encoding="utf-8")
        link_dir_kind = try_dir_link(src / "link_out", outside)
        link_file_ok = try_file_link(src / "link_file.txt", outside / "outside.java")
        note(f"链接测试：目录链接 {link_dir_kind or '不可用'}，文件链接 {'可用' if link_file_ok else '不可用'}")

        handoff_root = base / "handoff"
        config = make_config(src)
        service = CcService(config, handoff_root=handoff_root)
        prepare_tool = PrepareCcHandoffTool(service)
        validate_tool = ValidateCcResultTool(service)
        goal = "分析订单状态流转"

        # ---- 默认配置失败关闭 ----
        actual = get_tools_config()
        check("默认 cc.enabled=false", actual.cc.enabled is False)
        check("默认 cc.allowed_roots 为空", actual.cc.allowed_roots == [])
        check("真实鼠标输入仍关闭", actual.input.real_mouse_enabled is False)
        try:
            CcService().plan([str(src)])
            disabled_ok = False
        except Exception as exc:  # noqa: BLE001
            disabled_ok = getattr(exc, "code", "") == "cc_disabled"
        check("默认配置拒绝创建交接任务", disabled_ok)
        check(
            "工具已注册且需要确认/不自动重试",
            registry.get("prepare_cc_handoff") is not None
            and registry.get("validate_cc_result") is not None
            and registry.get("prepare_cc_handoff").requires_confirmation
            and not registry.get("prepare_cc_handoff").retry_safe
            and not registry.get("validate_cc_result").retry_safe,
        )

        # ---- 正常准备（走真实闸门）----
        source_before = (src / "Main.java").read_bytes()
        outcome, events, created = await run_gate(
            prepare_tool,
            {"goal": goal, "paths": [str(src)]},
            approve=True,
            suffix="prepare-ok",
        )
        check("用户同意后闸门放行", outcome.proceed is True)
        check(
            "确认卡片包含目标、只读和模型提供商提示",
            "分析订单状态流转" in outcome.summary
            and "只读" in outcome.summary
            and "模型提供商" in outcome.summary,
            outcome.summary[:160],
        )
        check("确认事件已产生", any(e.get("type") == "need_confirm" for e in events))
        check("准备工具返回成功", bool(created and created.ok))
        check("准备状态为 cc_handoff_ready", bool(created and created.status == "cc_handoff_ready"))
        check(
            "真实源文件没有被修改",
            (src / "Main.java").read_bytes() == source_before,
        )

        task_dirs = [p for p in handoff_root.iterdir() if p.is_dir() and not p.name.startswith(".tmp-")]
        check("生成且只生成一个交接目录", len(task_dirs) == 1, str(task_dirs))
        handoff = task_dirs[0]
        task_id = handoff.name
        task = json.loads((handoff / "task.json").read_text(encoding="utf-8"))
        manifest = json.loads((handoff / "manifest.json").read_text(encoding="utf-8"))
        manifest_paths = [item["path"] for item in task["manifest"]]

        check("task_id 由服务端生成且格式正确", bool(protocol.TASK_ID_RE.match(task_id)))
        check("task.json schema/read_only 正确", task["schema_version"] == 1 and task["read_only"] is True)
        check("task.json 包含 goal/created_at/deadline", task["goal"] == goal and task["created_at"] and task["deadline"])
        check("manifest 只包含两个授权的 Java 文件", sorted(manifest_paths) == ["root_1/src/Main.java", "root_1/src/sub/Util.java"], str(manifest_paths))
        check("manifest.json 与 task.json 一致", manifest["files"] == task["manifest"])
        check("工作区文件存在", (handoff / "workspace" / "root_1" / "src" / "Main.java").is_file())
        check("没有生成 result.json", not (handoff / "result.json").exists())
        check("交接包不含原始绝对路径", str(src) not in (handoff / "task.json").read_text(encoding="utf-8") and str(base) not in (handoff / "task.json").read_text(encoding="utf-8"))
        all_package_text = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in handoff.rglob("*")
            if p.is_file() and p.suffix in {".json", ".java", ".txt"}
        )
        check("敏感内容没有进入交接包", "SHOULD_NOT_LEAK" not in all_package_text)
        check("日志/依赖/二进制/链接目标没有进入工作区", not any("logs" in p or "node_modules" in p or "binary.bin" in p or "link_out" in p or "link_file" in p for p in manifest_paths), str(manifest_paths))
        check("准备结果说明人工交接且没有启动 CC", "人工" in (created.output if created else "") and "没有启动 CC" in (created.output if created else ""))

        # ---- 正常验证 ----
        write_result(handoff, task_id)
        valid, prep_valid = await validate_direct(validate_tool, task_id)
        check("正常结果验证成功", bool(valid and valid.ok and valid.status == "cc_result_ok"), valid.error if valid else str(prep_valid))
        check("验证结果明确标注不可信", bool(valid and "不可信数据" in valid.output))
        check("验证结果包含摘要和引用文件", bool(valid and "CREATED" in valid.output and "Main.java" in valid.output))
        check("验证结果落库摘要不含正文", bool(valid and "CREATED" not in valid.persist_text), valid.persist_text if valid else "")

        # ---- 阶段 A：协议基础 ----
        def task_copy(**changes: Any) -> dict[str, Any]:
            data = json.loads(json.dumps(task))
            data.update(changes)
            return data

        def validate_task_data(data: dict[str, Any]) -> protocol.TaskDocument:
            return protocol.validate_task_document(
                data,
                expected_task_id=task_id,
                max_files=10,
                max_total_bytes=5_000,
            )

        def expect_task_error(label: str, **changes: Any) -> None:
            try:
                validate_task_data(task_copy(**changes))
            except protocol.CcValidationError:
                check(label, True)
            except Exception as exc:  # noqa: BLE001
                check(label, False, f"{type(exc).__name__}: {exc}")
            else:
                check(label, False)

        normalized = validate_task_data(
            task_copy(
                created_at="2026-10-21T15:00:00Z",
                deadline="2026-10-21 23:40:00+08:00",
            )
        )
        check(
            "时间字段规范化为带时区 ISO",
            normalized.created_at == "2026-10-21T15:00:00+00:00"
            and normalized.deadline == "2026-10-21T23:40:00+08:00",
            f"{normalized.created_at} / {normalized.deadline}",
        )
        expect_task_error("拒绝 naive created_at", created_at="2026-10-21T15:00:00")
        expect_task_error("拒绝 naive deadline", deadline="2026-10-21T16:00:00")
        expect_task_error(
            "混合时区不抛 TypeError",
            created_at="2026-10-21T15:00:00",
            deadline="2026-10-21T16:00:00+08:00",
        )
        check(
            "TASK_ID_RE 拒绝尾随换行",
            not bool(protocol.TASK_ID_RE.match(task_id + "\n")),
        )
        expect_task_error("拒绝 task_id 额外字符", task_id=task_id + "\n")

        valid_text = "分析\n\t😀"
        with_valid_text = validate_task_data(task_copy(goal=valid_text))
        check("goal 允许中文、换行、制表符和 emoji", with_valid_text.goal == valid_text)
        for label, bad_text in [
            ("拒绝 goal 中的 ESC", "\x1b[31m"),
            ("拒绝 goal 中的 DEL", "\x7f"),
            ("拒绝 goal 中的 C1 控制字符", "\x80"),
            ("拒绝 goal 中的未配对代理项", "\ud800"),
        ]:
            expect_task_error(label, goal=bad_text)

        result_base: dict[str, Any] = {
            "schema_version": 1,
            "task_id": task_id,
            "status": "succeeded",
            "summary": "正常结果",
            "files": [],
            "error": "",
            "finished_at": "2026-10-21T15:10:00Z",
            "truncated": False,
        }
        normalized_result = protocol.validate_result_document(result_base, normalized)
        check(
            "finished_at 规范化为带时区 ISO",
            normalized_result.finished_at == "2026-10-21T15:10:00+00:00",
            normalized_result.finished_at,
        )

        def expect_result_error(label: str, **changes: Any) -> None:
            data = dict(result_base)
            data.update(changes)
            try:
                protocol.validate_result_document(data, normalized)
            except protocol.CcValidationError:
                check(label, True)
            except Exception as exc:  # noqa: BLE001
                check(label, False, f"{type(exc).__name__}: {exc}")
            else:
                check(label, False)

        expect_result_error(
            "拒绝 naive finished_at",
            finished_at="2026-10-21T15:10:00",
        )
        expect_result_error("拒绝 summary 中的控制字符", summary="bad\x7f")
        expect_result_error(
            "拒绝 error 中的控制字符",
            status="failed",
            summary="",
            error="bad\x1b",
        )
        expect_result_error(
            "拒绝 note 中的未配对代理项",
            files=[{"path": "root_1/src/Main.java", "note": "\ud800"}],
        )
        valid_result = protocol.validate_result_document(
            {
                **result_base,
                "summary": "分析\n\t😀",
                "files": [{"path": "root_1/src/Main.java", "note": "说明😀"}],
            },
            normalized,
        )
        check("summary/note 允许中文、换行、制表符和 emoji", "😀" in valid_result.summary)

        try:
            protocol.parse_json_strict("[" * 3_000 + "0" + "]" * 3_000)
        except protocol.CcValidationError:
            check("深层 JSON 转换为协议错误", True)
        except RecursionError:
            check("深层 JSON 转换为协议错误", False, "RecursionError 未转换")
        except Exception as exc:  # noqa: BLE001
            check("深层 JSON 转换为协议错误", False, f"{type(exc).__name__}: {exc}")
        else:
            check("深层 JSON 转换为协议错误", False, "没有拒绝")

        # 超大整数：Python 3.11+ 的 int 转换限制会抛 ValueError，
        # 不能让它越过 cc_result_invalid 协议错误映射。
        huge_integer = "9" * 4_301
        try:
            protocol.parse_json_strict('{"value": ' + huge_integer + "}")
        except protocol.CcValidationError:
            check("超大 JSON 整数转换为协议错误", True)
        except Exception as exc:  # noqa: BLE001
            check(
                "超大 JSON 整数转换为协议错误",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            check("超大 JSON 整数转换为协议错误", False, "没有拒绝")

        huge_root = base / "huge-json-root"
        huge_root.mkdir()
        (huge_root / "A.java").write_text("class A {}\n", encoding="utf-8")

        huge_result_handoff = base / "handoff-huge-result"
        huge_result_service = CcService(
            make_config(huge_root, allowed_roots=[str(huge_root)]),
            handoff_root=huge_result_handoff,
        )
        huge_result_tool = PrepareCcHandoffTool(huge_result_service)
        huge_result_task_id, _ = await make_task(
            huge_result_tool, [huge_root], "超大整数 result 测试"
        )
        (huge_result_handoff / huge_result_task_id / "result.json").write_text(
            '{"schema_version": ' + huge_integer + "}",
            encoding="utf-8",
        )
        huge_result_output, huge_result_prepared = await validate_direct(
            ValidateCcResultTool(huge_result_service), huge_result_task_id
        )
        check(
            "超大整数 result.json 映射为 cc_result_invalid",
            huge_result_output is not None
            and not huge_result_output.ok
            and huge_result_output.status == "cc_result_invalid",
            "prepare_blocked="
            f"{huge_result_prepared.blocked_code or '-'}; "
            f"execute="
            f"{'None' if huge_result_output is None else huge_result_output.status}",
        )

        huge_task_handoff = base / "handoff-huge-task"
        huge_task_service = CcService(
            make_config(huge_root, allowed_roots=[str(huge_root)]),
            handoff_root=huge_task_handoff,
        )
        huge_task_id, _ = await make_task(
            PrepareCcHandoffTool(huge_task_service),
            [huge_root],
            "超大整数 task 测试",
        )
        write_result(huge_task_handoff / huge_task_id, huge_task_id)
        (huge_task_handoff / huge_task_id / "task.json").write_text(
            '{"schema_version": ' + huge_integer + "}",
            encoding="utf-8",
        )
        huge_task_output, huge_task_prepared = await validate_direct(
            ValidateCcResultTool(huge_task_service), huge_task_id
        )
        check(
            "超大整数 task.json 映射为 cc_result_invalid",
            huge_task_output is None
            and huge_task_prepared.blocked_code == "cc_result_invalid",
            f"prepare_blocked={huge_task_prepared.blocked_code or '-'}",
        )

        huge_manifest_handoff = base / "handoff-huge-manifest"
        huge_manifest_service = CcService(
            make_config(huge_root, allowed_roots=[str(huge_root)]),
            handoff_root=huge_manifest_handoff,
        )
        huge_manifest_id, _ = await make_task(
            PrepareCcHandoffTool(huge_manifest_service),
            [huge_root],
            "超大整数 manifest 测试",
        )
        huge_manifest_dir = huge_manifest_handoff / huge_manifest_id
        write_result(huge_manifest_dir, huge_manifest_id)
        (huge_manifest_dir / "manifest.json").write_text(
            '{"schema_version": ' + huge_integer + "}",
            encoding="utf-8",
        )
        huge_manifest_output, huge_manifest_prepared = await validate_direct(
            ValidateCcResultTool(huge_manifest_service), huge_manifest_id
        )
        check(
            "超大整数 manifest.json 映射为 cc_result_invalid",
            huge_manifest_output is not None
            and not huge_manifest_output.ok
            and huge_manifest_output.status == "cc_result_invalid",
            "prepare_blocked="
            f"{huge_manifest_prepared.blocked_code or '-'}; "
            f"execute="
            f"{'None' if huge_manifest_output is None else huge_manifest_output.status}",
        )

        bad_goal_prep = await prepare_tool.prepare(
            {"goal": "bad\ud800", "paths": [str(src)]}
        )
        check(
            "工具准备阶段拒绝非法 goal",
            bad_goal_prep.blocked_code == "invalid_argument",
            bad_goal_prep.blocked_code,
        )

        # ---- 阶段 B：路径与策略加固 ----
        # 链接检查：不存在的路径是正常情况；其他 OSError 无法证明安全，必须失败关闭。
        with mock.patch.object(
            snapshot.os, "lstat", side_effect=FileNotFoundError("missing")
        ):
            check(
                "链接检查：不存在的路径返回 False",
                snapshot.path_is_link_like(Path("missing")) is False,
            )
        with mock.patch.object(
            snapshot.os, "lstat", side_effect=PermissionError("denied")
        ):
            try:
                snapshot.path_is_link_like(Path("denied"))
            except snapshot.CcPackageInvalid:
                check("链接检查：OSError 默认失败关闭", True)
            except Exception as exc:  # noqa: BLE001
                check("链接检查：OSError 默认失败关闭", False, f"{type(exc).__name__}: {exc}")
            else:
                check("链接检查：OSError 默认失败关闭", False, "返回了 False")

            try:
                snapshot.path_is_link_like(Path("denied"), error=rp.ReadFailed)
            except rp.ReadFailed:
                check("链接检查：可指定正确的错误类型", True)
            except Exception as exc:  # noqa: BLE001
                check("链接检查：可指定正确的错误类型", False, f"{type(exc).__name__}: {exc}")
            else:
                check("链接检查：可指定正确的错误类型", False, "返回了 False")

        class BrokenDirEntry:
            def is_symlink(self) -> bool:
                return False

            def stat(self, *, follow_symlinks: bool = True):
                raise OSError("denied")

        check(
            "ReadAccessPolicy 链接检查失败关闭",
            rp.is_link_like(BrokenDirEntry()) is True,
        )

        # CC 策略必须只使用 cc.allowed_roots，不能借用通用读取白名单。
        cc_policy = snapshot.CcPolicy.from_config(
            make_config(src, allowed_roots=[str(src)])
        )
        check(
            "CC 策略只使用 cc.allowed_roots",
            cc_policy.read.roots == (src.resolve(),),
            repr(cc_policy.read.roots),
        )
        for sensitive_name in (".env", "keys.txt"):
            try:
                cc_policy.read.resolve_file(str(src / sensitive_name))
            except rp.SensitiveFile:
                check(f"CC 策略拒绝敏感文件 {sensitive_name}", True)
            except Exception as exc:  # noqa: BLE001
                check(
                    f"CC 策略拒绝敏感文件 {sensitive_name}",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                check(f"CC 策略拒绝敏感文件 {sensitive_name}", False, "被放行")
        try:
            cc_policy.read.resolve_file(str(src / ".git" / "config"))
        except rp.SensitiveFile:
            check("CC 策略拒绝敏感目录 .git", True)
        except Exception as exc:  # noqa: BLE001
            check("CC 策略拒绝敏感目录 .git", False, f"{type(exc).__name__}: {exc}")
        else:
            check("CC 策略拒绝敏感目录 .git", False, "被放行")

        empty_policy = snapshot.CcPolicy.from_config(
            make_config(src, allowed_roots=[])
        )
        try:
            empty_policy.ensure_can_create()
        except rp.NoAllowedRoots:
            check("空 allowed_roots 在创建前失败", True)
        except Exception as exc:  # noqa: BLE001
            check("空 allowed_roots 在创建前失败", False, f"{type(exc).__name__}: {exc}")
        else:
            check("空 allowed_roots 在创建前失败", False, "没有拒绝")

        invalid_policy = snapshot.CcPolicy.from_config(
            make_config(src, allowed_roots=["relative/path"])
        )
        try:
            invalid_policy.ensure_can_create()
        except rp.NoAllowedRoots:
            check("无效 allowed_roots 在创建前失败", True)
        except Exception as exc:  # noqa: BLE001
            check("无效 allowed_roots 在创建前失败", False, f"{type(exc).__name__}: {exc}")
        else:
            check("无效 allowed_roots 在创建前失败", False, "没有拒绝")

        missing_policy = snapshot.CcPolicy.from_config(
            make_config(src, allowed_roots=[str(base / "missing-root")])
        )
        try:
            missing_policy.ensure_can_create()
        except rp.NoAllowedRoots:
            check("不存在的 allowed_roots 在创建前失败", True)
        except Exception as exc:  # noqa: BLE001
            check(
                "不存在的 allowed_roots 在创建前失败",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            check("不存在的 allowed_roots 在创建前失败", False, "没有拒绝")

        empty_handoff = base / "handoff-empty-create"
        empty_service = CcService(
            make_config(src, allowed_roots=[]), handoff_root=empty_handoff
        )
        empty_collection = snapshot.CcCollection((), (), {}, 0)
        try:
            empty_service.create_handoff(
                task_id="cc-" + "a" * 32,
                goal="空根测试",
                collection=empty_collection,
            )
        except rp.NoAllowedRoots:
            check("直接创建交接任务时也检查根目录", True)
        except Exception as exc:  # noqa: BLE001
            check("直接创建交接任务时也检查根目录", False, f"{type(exc).__name__}: {exc}")
        else:
            check("直接创建交接任务时也检查根目录", False, "没有拒绝")
        check("空根失败时没有创建交接根目录", not empty_handoff.exists())

        invalid_config = make_config(src, allowed_roots=["relative/path"])
        invalid_tool = PrepareCcHandoffTool(
            CcService(invalid_config, handoff_root=base / "handoff-invalid")
        )
        invalid_prep = await invalid_tool.prepare(
            {"goal": "无效根测试", "paths": [str(src)]}
        )
        check(
            "无效 allowed_roots 的工具准备失败关闭",
            invalid_prep.blocked_code == "no_allowed_roots",
            invalid_prep.blocked_code,
        )

        # 严格根模式：CC 只要有一项 allowed_roots 被过滤，就不能静默使用剩余根。
        mixed_invalid_policy = snapshot.CcPolicy.from_config(
            make_config(src, allowed_roots=[str(src), "relative/path"])
        )
        try:
            mixed_invalid_policy.ensure_can_create()
        except rp.ReadPolicyConfigError:
            check("有效根 + 无效根混合配置在创建前失败", True)
        except Exception as exc:  # noqa: BLE001
            check(
                "有效根 + 无效根混合配置在创建前失败",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            check("有效根 + 无效根混合配置在创建前失败", False, "没有拒绝")

        over_broad_root = str(Path(src).resolve().anchor)
        mixed_broad_policy = snapshot.CcPolicy.from_config(
            make_config(src, allowed_roots=[str(src), over_broad_root])
        )
        try:
            mixed_broad_policy.ensure_can_create()
        except rp.ReadPolicyConfigError:
            check("有效根 + 过宽根混合配置在创建前失败", True)
        except Exception as exc:  # noqa: BLE001
            check(
                "有效根 + 过宽根混合配置在创建前失败",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            check("有效根 + 过宽根混合配置在创建前失败", False, "没有拒绝")

        home_parent = str(Path.home().parent)
        mixed_home_parent_policy = snapshot.CcPolicy.from_config(
            make_config(src, allowed_roots=[str(src), home_parent])
        )
        try:
            mixed_home_parent_policy.ensure_can_create()
        except rp.ReadPolicyConfigError:
            check("有效根 + 用户主目录祖先混合配置在创建前失败", True)
        except Exception as exc:  # noqa: BLE001
            check(
                "有效根 + 用户主目录祖先混合配置在创建前失败",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            check("有效根 + 用户主目录祖先混合配置在创建前失败", False, "没有拒绝")

        all_valid_policy = snapshot.CcPolicy.from_config(
            make_config(
                src,
                allowed_roots=[str(src), str(src / "sub")],
            )
        )
        try:
            all_valid_policy.ensure_can_create()
            check(
                "全部有效根通过严格检查",
                {str(item) for item in all_valid_policy.read.roots}
                == {str(src.resolve()), str((src / "sub").resolve())},
                repr(all_valid_policy.read.roots),
            )
        except Exception as exc:  # noqa: BLE001
            check("全部有效根通过严格检查", False, f"{type(exc).__name__}: {exc}")

        filesystem_default_policy = rp.ReadAccessPolicy(
            allowed_roots=[str(src), "relative/path"]
        )
        try:
            filesystem_default_policy.ensure_ready()
            check(
                "filesystem 默认非严格模式保持原行为",
                filesystem_default_policy.roots == (src.resolve(),),
                repr(filesystem_default_policy.roots),
            )
        except Exception as exc:  # noqa: BLE001
            check(
                "filesystem 默认非严格模式保持原行为",
                False,
                f"{type(exc).__name__}: {exc}",
            )

        mixed_handoff = base / "handoff-mixed-roots"
        mixed_service = CcService(
            make_config(src, allowed_roots=[str(src), "relative/path"]),
            handoff_root=mixed_handoff,
        )
        try:
            mixed_service.create_handoff(
                task_id="cc-" + "d" * 32,
                goal="混合根测试",
                collection=snapshot.CcCollection((), (), {}, 0),
            )
        except rp.ReadPolicyConfigError:
            check("混合根在创建交接目录前失败", True)
        except Exception as exc:  # noqa: BLE001
            check(
                "混合根在创建交接目录前失败",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            check("混合根在创建交接目录前失败", False, "没有拒绝")
        check("混合根失败时没有创建交接根目录", not mixed_handoff.exists())

        # enabled 必须是真正的布尔 True；字符串 "false"（以及 "true"）都不能启用。
        for raw_enabled in ("false", "true", 1, None):
            strict_policy = snapshot.CcPolicy.from_config(
                CcConfig.model_construct(enabled=raw_enabled)
            )
            check(
                f"enabled={raw_enabled!r} 严格按布尔处理",
                strict_policy.enabled is False,
            )

        check(
            "CcResultInvalid 已定义并复用协议错误码",
            hasattr(cc_service, "CcResultInvalid")
            and cc_service.CcResultInvalid.code == "cc_result_invalid",
        )

        linked_task_id = "cc-" + "c" * 32
        linked_result_dir = handoff_root / linked_task_id
        linked_result_dir.mkdir()
        (linked_result_dir / "task.json").write_text("{}", encoding="utf-8")
        outside_result = base / "outside-result.json"
        outside_result.write_text("{}", encoding="utf-8")
        if try_file_link(linked_result_dir / "result.json", outside_result):
            try:
                service.prepare_result(linked_task_id)
            except cc_service.CcResultInvalid:
                check("result.json 链接分支抛出已定义的错误", True)
            except Exception as exc:  # noqa: BLE001
                check(
                    "result.json 链接分支抛出已定义的错误",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                check("result.json 链接分支抛出已定义的错误", False, "被放行")
        else:
            note("result.json 文件链接不可用，跳过链接分支测试")

        permission_task_id = "cc-" + "e" * 32
        permission_dir = handoff_root / permission_task_id
        permission_dir.mkdir()
        (permission_dir / "task.json").write_text("{}", encoding="utf-8")
        real_lstat = snapshot.os.lstat

        def deny_result_lstat(path):
            if Path(path).name == "result.json":
                raise PermissionError("denied")
            return real_lstat(path)

        with mock.patch.object(snapshot.os, "lstat", side_effect=deny_result_lstat):
            try:
                service.prepare_result(permission_task_id)
            except cc_service.CcResultInvalid as exc:
                check(
                    "result.json 链接检查的系统错误映射为 cc_result_invalid",
                    exc.code == "cc_result_invalid",
                    exc.code,
                )
            except Exception as exc:  # noqa: BLE001
                check(
                    "result.json 链接检查的系统错误映射为 cc_result_invalid",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                check(
                    "result.json 链接检查的系统错误映射为 cc_result_invalid",
                    False,
                    "没有拒绝",
                )

        # 工作区路径键：Windows 大小写不敏感，POSIX 区分大小写。
        check(
            "a/b 与 a/B 的比较键按平台区分",
            (
                snapshot.relpath_key("root_1/Main.java")
                == snapshot.relpath_key("root_1/main.java")
            )
            == (os.name == "nt"),
        )

        main_stat = os.stat(src / "Main.java")

        def case_candidate(relpath: str) -> snapshot.CcCandidate:
            return snapshot.CcCandidate(
                source=str(src / "Main.java"),
                relpath=relpath,
                size=int(main_stat.st_size),
                mtime_ns=int(main_stat.st_mtime_ns),
                dev=int(main_stat.st_dev),
                ino=int(main_stat.st_ino),
            )

        case_candidates = (
            case_candidate("root_1/src/Main.java"),
            case_candidate("root_1/src/main.java"),
        )
        case_collection = snapshot.CcCollection(
            candidates=case_candidates,
            allowed_paths=("root_1/src",),
            skipped={},
            total_bytes=sum(item.size for item in case_candidates),
        )
        if os.name == "nt":
            case_workspace = base / "case-workspace"
            try:
                snapshot.copy_workspace(cc_policy, case_collection, case_workspace)
            except snapshot.CcSnapshotFailed:
                check("大小写冲突在创建 workspace 前发现", True)
            except Exception as exc:  # noqa: BLE001
                check("大小写冲突在创建 workspace 前发现", False, f"{type(exc).__name__}: {exc}")
            else:
                check("大小写冲突在创建 workspace 前发现", False, "没有拒绝")
            check("冲突失败时没有创建 workspace", not case_workspace.exists())

            case_handoff = base / "handoff-case"
            case_service = CcService(make_config(src), handoff_root=case_handoff)
            try:
                case_service.create_handoff(
                    task_id="cc-" + "b" * 32,
                    goal="大小写冲突",
                    collection=case_collection,
                )
            except snapshot.CcSnapshotFailed:
                check("大小写冲突在创建交接根前发现", True)
            except Exception as exc:  # noqa: BLE001
                check("大小写冲突在创建交接根前发现", False, f"{type(exc).__name__}: {exc}")
            else:
                check("大小写冲突在创建交接根前发现", False, "没有拒绝")
            check("冲突失败时没有创建交接根目录", not case_handoff.exists())
        else:
            note("Windows 大小写冲突的复制前检查仅在 Windows 上运行")

        # 直接调用 copy_workspace() 时，也必须先过 CC 授权检查再建目录。
        empty_copy_policy = snapshot.CcPolicy.from_config(
            make_config(src, allowed_roots=[])
        )
        copy_candidate = case_candidate("root_1/Main.java")
        empty_copy_collection = snapshot.CcCollection(
            candidates=(copy_candidate,),
            allowed_paths=("root_1",),
            skipped={},
            total_bytes=copy_candidate.size,
        )
        empty_copy_workspace = base / "copy-empty-workspace"
        try:
            snapshot.copy_workspace(
                empty_copy_policy,
                empty_copy_collection,
                empty_copy_workspace,
            )
        except rp.NoAllowedRoots:
            check("copy_workspace 在创建 workspace 前检查授权根", True)
        except Exception as exc:  # noqa: BLE001
            check(
                "copy_workspace 在创建 workspace 前检查授权根",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            check("copy_workspace 在创建 workspace 前检查授权根", False, "没有拒绝")
        check(
            "授权失败时没有创建 workspace 目录",
            not empty_copy_workspace.exists(),
        )

        # 排除规则应相对授权根判断，不能被授权根路径里的 logs 祖先误伤。
        logs_parent = base / "logs"
        logs_project = logs_parent / "project"
        logs_project.mkdir(parents=True)
        (logs_project / "Main.java").write_text(
            "class Main {}\n", encoding="utf-8"
        )
        logs_config = make_config(logs_project, allowed_roots=[str(logs_project)])
        logs_tool = PrepareCcHandoffTool(
            CcService(logs_config, handoff_root=base / "handoff-logs-root")
        )
        logs_prep = await logs_tool.prepare(
            {"goal": "logs 祖先测试", "paths": [str(logs_project)]}
        )
        check(
            "授权根位于 logs 祖先下时不被误排除",
            not logs_prep.blocked,
            logs_prep.blocked_code,
        )

        log_named_file = logs_project / "log"
        log_named_file.write_text("not a log directory\n", encoding="utf-8")
        log_file_prep = await logs_tool.prepare(
            {"goal": "名为 log 的文件", "paths": [str(log_named_file)]}
        )
        check(
            "名为 log 的普通文件不被目录排除规则误伤",
            not log_file_prep.blocked,
            log_file_prep.blocked_code,
        )

        (logs_project / "logs").mkdir()
        (logs_project / "logs" / "inner.java").write_text(
            "class Inner {}\n", encoding="utf-8"
        )
        logs_child_prep = await logs_tool.prepare(
            {"goal": "真正的 logs 子目录", "paths": [str(logs_project / "logs")]}
        )
        check(
            "授权根内部的 logs 子目录仍被排除",
            logs_child_prep.blocked_code == "path_not_allowed",
            logs_child_prep.blocked_code,
        )

        # ---- 阶段 C1：deadline ----
        deadline_root = base / "deadline-root"
        deadline_root.mkdir()
        (deadline_root / "Deadline.java").write_text(
            "class Deadline {}\n", encoding="utf-8"
        )
        deadline_handoff = base / "handoff-deadline"
        deadline_service = CcService(
            make_config(
                deadline_root,
                allowed_roots=[str(deadline_root)],
                deadline_hours=0.1,
            ),
            handoff_root=deadline_handoff,
        )
        deadline_task_id, _ = await make_task(
            PrepareCcHandoffTool(deadline_service),
            [deadline_root],
            "deadline 测试",
        )
        deadline_doc = json.loads(
            (deadline_handoff / deadline_task_id / "task.json").read_text(
                encoding="utf-8"
            )
        )
        deadline_at = datetime.fromisoformat(deadline_doc["deadline"])
        check(
            "deadline 是带时区的 datetime",
            deadline_at.tzinfo is not None
            and deadline_at.utcoffset() is not None,
        )
        write_result(
            deadline_handoff / deadline_task_id,
            deadline_task_id,
            files=[
                {"path": deadline_doc["manifest"][0]["path"], "note": ""}
            ],
        )

        labels: list[str] = []
        real_read_json_capped = snapshot.read_json_capped

        def tracking_read_json_capped(path, **kwargs):
            labels.append(kwargs.get("label", ""))
            return real_read_json_capped(path, **kwargs)

        with mock.patch.object(
            snapshot, "read_json_capped", side_effect=tracking_read_json_capped
        ):
            try:
                deadline_service.prepare_result(
                    deadline_task_id,
                    now=(deadline_at + timedelta(seconds=1)).astimezone(
                        timezone.utc
                    ),
                )
            except cc_service.CcTaskExpired as exc:
                check("过期任务在 prepare_result 被拒绝", True, exc.code)
            except Exception as exc:  # noqa: BLE001
                check(
                    "过期任务在 prepare_result 被拒绝",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                check("过期任务在 prepare_result 被拒绝", False, "没有拒绝")
        check(
            "过期时没有读取 result.json 正文",
            "result.json" not in labels,
            repr(labels),
        )

        try:
            deadline_service.prepare_result(deadline_task_id, now=deadline_at)
        except cc_service.CcTaskExpired:
            check("deadline 边界按已过期处理", True)
        except Exception as exc:  # noqa: BLE001
            check(
                "deadline 边界按已过期处理",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            check("deadline 边界按已过期处理", False, "没有拒绝")

        before_deadline = deadline_at.astimezone(timezone.utc) - timedelta(
            seconds=1
        )
        deadline_target = deadline_service.prepare_result(
            deadline_task_id, now=before_deadline
        )
        check(
            "未过期任务仍可准备验证",
            deadline_target.task_id == deadline_task_id,
        )
        deadline_validation = deadline_service.validate_result(
            task_id=deadline_task_id,
            result_fingerprint=deadline_target.fingerprint,
            now=before_deadline,
        )
        check(
            "未过期任务在确认后可验证",
            deadline_validation.result.status == "succeeded",
            deadline_validation.result.status,
        )
        try:
            deadline_service.validate_result(
                task_id=deadline_task_id,
                result_fingerprint=deadline_target.fingerprint,
                now=(deadline_at + timedelta(seconds=1)).astimezone(
                    timezone.utc
                ),
            )
        except cc_service.CcTaskExpired:
            check("过期任务在 validate_result 再次被拒绝", True)
        except Exception as exc:  # noqa: BLE001
            check(
                "过期任务在 validate_result 再次被拒绝",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            check("过期任务在 validate_result 再次被拒绝", False, "没有拒绝")

        # 手工改 task.json 让任务过期，工具准备阶段也必须失败关闭。
        stale_now = datetime.now().astimezone()
        deadline_doc["created_at"] = (stale_now - timedelta(hours=2)).isoformat()
        deadline_doc["deadline"] = (stale_now - timedelta(hours=1)).isoformat()
        (deadline_handoff / deadline_task_id / "task.json").write_text(
            json.dumps(deadline_doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        stale_prep = await ValidateCcResultTool(deadline_service).prepare(
            {"task_id": deadline_task_id}
        )
        check(
            "工具准备阶段拒绝过期任务",
            stale_prep.blocked_code == "cc_task_expired",
            stale_prep.blocked_code,
        )

        # ---- 阶段 C2：结果文件读取窗口 ----
        c2_normal = base / "c2-normal.json"
        c2_normal.write_text('{"ok": true}', encoding="utf-8")
        check(
            "普通 result.json 仍可正常读取",
            snapshot.read_json_capped(
                c2_normal, max_bytes=1_000, label="result.json"
            )
            == {"ok": True},
        )

        c2_replaced = base / "c2-replaced.json"
        c2_replaced.write_text('{"ok": true}', encoding="utf-8")
        c2_replacement = base / "c2-replacement.json"
        c2_replacement.write_text('{"ok": false}', encoding="utf-8")
        real_os_open = snapshot.os.open
        replaced = False

        def replace_then_open(path, flags, *args, **kwargs):
            nonlocal replaced
            if not replaced and Path(path) == c2_replaced:
                replaced = True
                os.replace(c2_replacement, c2_replaced)
            return real_os_open(path, flags, *args, **kwargs)

        with mock.patch.object(
            snapshot.os, "open", side_effect=replace_then_open
        ):
            try:
                snapshot.read_json_capped(
                    c2_replaced, max_bytes=1_000, label="result.json"
                )
            except snapshot.CcPackageInvalid as exc:
                check(
                    "打开前被替换成另一个普通文件时拒绝读取",
                    True,
                    str(exc),
                )
            except Exception as exc:  # noqa: BLE001
                check(
                    "打开前被替换成另一个普通文件时拒绝读取",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                check(
                    "打开前被替换成另一个普通文件时拒绝读取",
                    False,
                    "读取了替换后的文件",
                )

        c2_link = base / "c2-link.json"
        c2_link.write_text('{"ok": true}', encoding="utf-8")
        c2_outside = base / "c2-outside.json"
        c2_outside.write_text('{"outside": true}', encoding="utf-8")
        c2_probe = base / "c2-probe-link.json"
        if try_file_link(c2_probe, c2_outside):
            c2_probe.unlink()
            linked = False

            def symlink_then_open(path, flags, *args, **kwargs):
                nonlocal linked
                if not linked and Path(path) == c2_link:
                    linked = True
                    os.unlink(c2_link)
                    os.symlink(c2_outside, c2_link)
                return real_os_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                snapshot.os, "open", side_effect=symlink_then_open
            ):
                try:
                    snapshot.read_json_capped(
                        c2_link, max_bytes=1_000, label="result.json"
                    )
                except snapshot.CcPackageInvalid as exc:
                    check(
                        "打开前被替换成符号链接时拒绝读取",
                        True,
                        str(exc),
                    )
                except Exception as exc:  # noqa: BLE001
                    check(
                        "打开前被替换成符号链接时拒绝读取",
                        False,
                        f"{type(exc).__name__}: {exc}",
                    )
                else:
                    check(
                        "打开前被替换成符号链接时拒绝读取",
                        False,
                        "读取了链接目标",
                    )
        else:
            note("符号链接不可用，跳过 C2 链接替换测试")

        # ---- 阶段 C2b：确认指纹必须绑定到最终读取 ----
        fingerprint_root = base / "fingerprint-root"
        fingerprint_root.mkdir()
        (fingerprint_root / "Fingerprint.java").write_text(
            "class Fingerprint {}\n", encoding="utf-8"
        )
        fingerprint_handoff = base / "handoff-fingerprint"
        fingerprint_service = CcService(
            make_config(
                fingerprint_root,
                allowed_roots=[str(fingerprint_root)],
            ),
            handoff_root=fingerprint_handoff,
        )
        fingerprint_task_id, _ = await make_task(
            PrepareCcHandoffTool(fingerprint_service),
            [fingerprint_root],
            "确认后替换结果测试",
        )
        fingerprint_task = json.loads(
            (
                fingerprint_handoff / fingerprint_task_id / "task.json"
            ).read_text(encoding="utf-8")
        )
        fingerprint_manifest_path = fingerprint_task["manifest"][0]["path"]
        original_result = write_result(
            fingerprint_handoff / fingerprint_task_id,
            fingerprint_task_id,
            files=[{"path": fingerprint_manifest_path, "note": ""}],
        )
        confirmed = fingerprint_service.prepare_result(fingerprint_task_id)
        replacement_result = dict(
            original_result,
            summary="REPLACED CONTENT MUST NOT BE RETURNED",
            finished_at=now_iso(),
        )
        replacement_file = base / "fingerprint-replacement.json"
        replacement_file.write_text(
            json.dumps(replacement_result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        real_read_task_package = snapshot.read_task_package
        replaced_after_fingerprint = False

        def replace_after_fingerprint_check(handoff_dir, task_id_value):
            nonlocal replaced_after_fingerprint
            task = real_read_task_package(handoff_dir, task_id_value)
            if not replaced_after_fingerprint:
                replaced_after_fingerprint = True
                # validate_result() 此时已经完成确认指纹检查，但尚未读取
                # result.json。替换成另一份内容合法、task_id 匹配的结果。
                os.replace(
                    replacement_file,
                    Path(handoff_dir) / "result.json",
                )
            return task

        with mock.patch.object(
            snapshot,
            "read_task_package",
            side_effect=replace_after_fingerprint_check,
        ):
            try:
                validation = fingerprint_service.validate_result(
                    task_id=fingerprint_task_id,
                    result_fingerprint=confirmed.fingerprint,
                )
            except snapshot.CcResultChanged as exc:
                check(
                    "确认后替换 result.json 被拒绝为 cc_result_changed",
                    exc.code == "cc_result_changed",
                    exc.code,
                )
            except Exception as exc:  # noqa: BLE001
                check(
                    "确认后替换 result.json 被拒绝为 cc_result_changed",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                check(
                    "确认后替换 result.json 被拒绝为 cc_result_changed",
                    False,
                    "替换后的内容被当作有效结果返回："
                    + validation.result.summary,
                )
        check(
            "测试确实在指纹检查后、读取前替换了 result.json",
            replaced_after_fingerprint,
        )

        # ---- 阶段 C4：私钥正文过滤 ----
        key_root = base / "private-key-root"
        key_root.mkdir()
        (key_root / "notes.txt").write_text(
            "-----BEGIN RSA PRIVATE KEY-----\nSECRET\n",
            encoding="utf-8",
        )
        key_handoff = base / "handoff-private-key"
        key_tool = PrepareCcHandoffTool(
            CcService(
                make_config(key_root, allowed_roots=[str(key_root)]),
                handoff_root=key_handoff,
            )
        )
        key_args = {"goal": "私钥正文测试", "paths": [str(key_root)]}
        key_prep = await key_tool.prepare(key_args)
        check(
            "私钥正文不会在 prepare 阶段被提前读取",
            not key_prep.blocked,
            key_prep.blocked,
        )
        if key_prep.blocked:
            key_result = None
        else:
            key_result = await key_tool.execute(
                key_args, injected=key_prep.injected
            )
        check(
            "普通文件中的私钥正文导致交接失败",
            key_result is not None
            and not key_result.ok
            and key_result.status == "sensitive_file",
            "None"
            if key_result is None
            else f"{key_result.ok}/{key_result.status}/{key_result.error}",
        )
        key_task_id = key_prep.injected.get("task_id", "")
        check(
            "私钥拒绝后没有发布正式任务目录",
            not key_task_id or not (key_handoff / key_task_id).exists(),
        )
        check(
            "私钥拒绝后临时目录已清理",
            not key_handoff.exists()
            or not list(key_handoff.glob(".tmp-*")),
        )

        # 标记跨越复制块边界时也必须检测到。
        chunk_size = snapshot._COPY_CHUNK_BYTES
        boundary_root = base / "private-key-boundary-root"
        boundary_root.mkdir()
        marker = b"-----BEGIN RSA PRIVATE KEY-----"
        (boundary_root / "boundary.txt").write_bytes(
            b"A" * (chunk_size - 10) + marker + b"\n"
        )
        boundary_handoff = base / "handoff-private-key-boundary"
        boundary_tool = PrepareCcHandoffTool(
            CcService(
                make_config(
                    boundary_root,
                    allowed_roots=[str(boundary_root)],
                    max_file_bytes=1_500_000,
                    max_total_bytes=2_000_000,
                ),
                handoff_root=boundary_handoff,
            )
        )
        boundary_args = {
            "goal": "跨块私钥正文测试",
            "paths": [str(boundary_root)],
        }
        boundary_prep = await boundary_tool.prepare(boundary_args)
        check(
            "跨块私钥正文不会在 prepare 阶段被提前读取",
            not boundary_prep.blocked,
            boundary_prep.blocked,
        )
        if boundary_prep.blocked:
            boundary_result = None
        else:
            boundary_result = await boundary_tool.execute(
                boundary_args, injected=boundary_prep.injected
            )
        check(
            "跨越复制块边界的私钥正文仍会导致交接失败",
            boundary_result is not None
            and not boundary_result.ok
            and boundary_result.status == "sensitive_file",
            "None"
            if boundary_result is None
            else f"{boundary_result.ok}/{boundary_result.status}/"
            f"{boundary_result.error}",
        )
        boundary_task_id = boundary_prep.injected.get("task_id", "")
        check(
            "跨块私钥拒绝后没有发布正式任务目录",
            not boundary_task_id
            or not (boundary_handoff / boundary_task_id).exists(),
        )
        check(
            "跨块私钥拒绝后临时目录已清理",
            not boundary_handoff.exists()
            or not list(boundary_handoff.glob(".tmp-*")),
        )

        # ---- 意外异常：未发布的临时目录必须清理 ----
        cleanup_root = base / "cleanup-root"
        cleanup_root.mkdir()
        (cleanup_root / "Keep.java").write_text(
            "class Keep {}\n", encoding="utf-8"
        )
        cleanup_handoff = base / "handoff-cleanup"
        cleanup_service = CcService(
            make_config(cleanup_root, allowed_roots=[str(cleanup_root)]),
            handoff_root=cleanup_handoff,
        )
        existing_collection = cleanup_service.plan([str(cleanup_root)])
        existing_task_id = cleanup_service.new_task_id()
        _, existing_dir = cleanup_service.create_handoff(
            task_id=existing_task_id,
            goal="异常清理测试的已有任务",
            collection=existing_collection,
        )
        existing_task_bytes = (existing_dir / "task.json").read_bytes()
        existing_manifest = json.loads(
            (existing_dir / "manifest.json").read_text(encoding="utf-8")
        )
        existing_relpath = existing_manifest["files"][0]["path"]
        existing_workspace_file = (
            existing_dir / "workspace" / Path(*existing_relpath.split("/"))
        )
        existing_workspace_bytes = existing_workspace_file.read_bytes()

        failing_task_id = cleanup_service.new_task_id()
        failing_collection = cleanup_service.plan([str(cleanup_root)])

        def fail_after_partial_copy(policy, collection, workspace):
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "partial.txt").write_bytes(b"partial-copy")
            raise RuntimeError("模拟复制中途异常")

        caught: RuntimeError | None = None
        with mock.patch.object(
            snapshot,
            "copy_workspace",
            side_effect=fail_after_partial_copy,
        ):
            try:
                cleanup_service.create_handoff(
                    task_id=failing_task_id,
                    goal="异常清理测试",
                    collection=failing_collection,
                )
            except RuntimeError as exc:
                caught = exc

        check(
            "复制中途的意外异常原样抛出",
            caught is not None and str(caught) == "模拟复制中途异常",
            "None" if caught is None else str(caught),
        )
        check(
            "意外异常后没有发布正式任务目录",
            not (cleanup_handoff / failing_task_id).exists(),
        )
        check(
            "意外异常后没有遗留临时交接目录",
            not list(cleanup_handoff.glob(".tmp-*")),
            "、".join(p.name for p in cleanup_handoff.glob(".tmp-*")),
        )
        check(
            "已有交接任务不受异常清理影响",
            (existing_dir / "task.json").read_bytes() == existing_task_bytes
            and existing_workspace_file.read_bytes() == existing_workspace_bytes,
        )

        # 清理本身失败时，原始异常不能被覆盖；清理错误要留下日志。
        cleanup_failure_id = cleanup_service.new_task_id()
        cleanup_failure_collection = cleanup_service.plan([str(cleanup_root)])
        cleanup_failure_caught: RuntimeError | None = None
        with (
            mock.patch.object(
                snapshot,
                "copy_workspace",
                side_effect=fail_after_partial_copy,
            ),
            mock.patch.object(
                cc_service.shutil,
                "rmtree",
                side_effect=OSError("模拟清理失败"),
            ),
            mock.patch.object(cc_service.logger, "error") as cleanup_log,
        ):
            try:
                cleanup_service.create_handoff(
                    task_id=cleanup_failure_id,
                    goal="清理失败测试",
                    collection=cleanup_failure_collection,
                )
            except RuntimeError as exc:
                cleanup_failure_caught = exc

        check(
            "清理失败不会覆盖原始异常",
            cleanup_failure_caught is not None
            and str(cleanup_failure_caught) == "模拟复制中途异常",
            "None" if cleanup_failure_caught is None else str(cleanup_failure_caught),
        )
        check("清理失败会记录日志", cleanup_log.called)

        # 临时目录路径碰撞时，不能把别人的同名目录当成自己的临时目录删掉。
        collision_handoff = base / "handoff-cleanup-collision"
        collision_service = CcService(
            make_config(cleanup_root, allowed_roots=[str(cleanup_root)]),
            handoff_root=collision_handoff,
        )
        collision_collection = collision_service.plan([str(cleanup_root)])
        collision_task_id = collision_service.new_task_id()
        collision_suffix = "deadbeef"
        collision_temp = collision_handoff / (
            f".tmp-{collision_task_id}-{collision_suffix}"
        )
        collision_handoff.mkdir(parents=True)
        collision_temp.mkdir()
        collision_marker = collision_temp / "keep-me.txt"
        collision_marker.write_text("preexisting-marker", encoding="utf-8")
        collision_target = collision_handoff / collision_task_id
        collision_error: BaseException | None = None
        with mock.patch.object(
            cc_service.uuid,
            "uuid4",
            return_value=mock.Mock(hex=collision_suffix + "00000000"),
        ):
            try:
                collision_service.create_handoff(
                    task_id=collision_task_id,
                    goal="临时目录碰撞测试",
                    collection=collision_collection,
                )
            except BaseException as exc:  # noqa: BLE001
                collision_error = exc

        check(
            "临时目录碰撞时仍抛出 FileExistsError",
            isinstance(collision_error, FileExistsError),
            "None" if collision_error is None else type(collision_error).__name__,
        )
        check(
            "临时目录碰撞时保留原有目录和标记文件",
            collision_temp.is_dir()
            and collision_marker.is_file()
            and collision_marker.read_text(encoding="utf-8")
            == "preexisting-marker",
            f"temp_exists={collision_temp.exists()} "
            f"marker_exists={collision_marker.exists()}",
        )
        check(
            "临时目录碰撞时没有发布正式任务目录",
            not collision_target.exists(),
            str(collision_target),
        )

        # ---- 阶段 C3：单文件大小限制 ----
        exact_root = base / "c3-exact"
        exact_root.mkdir()
        (exact_root / "Exact.java").write_text("a" * 100, encoding="utf-8")
        exact_handoff = base / "handoff-c3-exact"
        exact_service = CcService(
            make_config(
                exact_root,
                allowed_roots=[str(exact_root)],
                max_file_bytes=100,
                max_total_bytes=1_000,
            ),
            handoff_root=exact_handoff,
        )
        exact_task_id, exact_result = await make_task(
            PrepareCcHandoffTool(exact_service),
            [exact_root],
            "C3 恰好上限",
        )
        exact_manifest = json.loads(
            (exact_handoff / exact_task_id / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        check(
            "恰好达到 max_file_bytes 的文件被允许",
            bool(exact_result and exact_result.ok)
            and exact_manifest["files"][0]["size"] == 100,
        )

        limit_root = base / "c3-limit"
        limit_root.mkdir()
        limit_file = limit_root / "Limit.java"
        limit_file.write_text("x" * 80, encoding="utf-8")
        limit_handoff = base / "handoff-c3-limit"
        limit_create_service = CcService(
            make_config(
                limit_root,
                allowed_roots=[str(limit_root)],
                max_file_bytes=100,
                max_total_bytes=1_000,
            ),
            handoff_root=limit_handoff,
        )
        limit_task_id, _ = await make_task(
            PrepareCcHandoffTool(limit_create_service),
            [limit_root],
            "C3 单文件上限",
        )
        limit_handoff_dir = limit_handoff / limit_task_id
        limit_manifest = json.loads(
            (limit_handoff_dir / "manifest.json").read_text(encoding="utf-8")
        )
        limit_relpath = limit_manifest["files"][0]["path"]
        write_result(
            limit_handoff_dir,
            limit_task_id,
            files=[{"path": limit_relpath, "note": ""}],
        )

        small_service = CcService(
            make_config(
                limit_root,
                allowed_roots=[str(limit_root)],
                max_file_bytes=50,
                max_total_bytes=1_000,
            ),
            handoff_root=limit_handoff,
        )
        try:
            small_service.validate_result(task_id=limit_task_id)
        except snapshot.CcLimitExceeded:
            check("读取 manifest 后按当前 max_file_bytes 拒绝单文件超限", True)
        except Exception as exc:  # noqa: BLE001
            check(
                "读取 manifest 后按当前 max_file_bytes 拒绝单文件超限",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            check(
                "读取 manifest 后按当前 max_file_bytes 拒绝单文件超限",
                False,
                "没有拒绝",
            )

        workspace_file = (
            limit_handoff_dir / "workspace" / Path(*limit_relpath.split("/"))
        )
        workspace_file.write_bytes(workspace_file.read_bytes() + b"y" * 120)
        workspace_limit_service = CcService(
            make_config(
                limit_root,
                allowed_roots=[str(limit_root)],
                max_file_bytes=100,
                max_total_bytes=1_000,
            ),
            handoff_root=limit_handoff,
        )
        try:
            workspace_limit_service.validate_result(task_id=limit_task_id)
        except snapshot.CcPackageInvalid as exc:
            check(
                "工作区实际文件超过 max_file_bytes 时停止读取",
                "单文件" in str(exc),
                str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            check(
                "工作区实际文件超过 max_file_bytes 时停止读取",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            check(
                "工作区实际文件超过 max_file_bytes 时停止读取",
                False,
                "没有拒绝",
            )

        # ---- 用户拒绝准备：不能生成交接包，也不能读取正文 ----
        dirs_before = {p.name for p in handoff_root.iterdir()}
        rejected_outcome, _rejected_events, rejected_result = await run_gate(
            prepare_tool,
            {"goal": "拒绝测试", "paths": [str(src)]},
            approve=False,
            suffix="prepare-reject",
        )
        check("用户拒绝准备后不放行", rejected_outcome.proceed is False and rejected_outcome.status == "rejected")
        check("用户拒绝准备后不执行", rejected_result is None)
        check("用户拒绝准备后不生成目录", {p.name for p in handoff_root.iterdir()} == dirs_before)

        # ---- 用户拒绝验证：不读取 result.json ----
        counting_service = CountingValidateService(config, handoff_root=handoff_root)
        counting_tool = ValidateCcResultTool(counting_service)
        rejected_validate_outcome, _, rejected_validate_result = await run_gate(
            counting_tool,
            {"task_id": task_id},
            approve=False,
            suffix="validate-reject",
        )
        check("用户拒绝验证后不放行", rejected_validate_outcome.proceed is False)
        check("用户拒绝验证后未执行验证", rejected_validate_result is None and counting_service.validations == 0)

        # ---- 错误协议 / 越权引用 / 伪造成功 ----
        bad_cases: list[tuple[str, dict[str, Any]]] = [
            ("schema_version 错误", {"schema_version": 99}),
            ("task_id 不匹配", {"task_id": "cc-" + "0" * 32}),
            ("status 非法", {"status": "done"}),
            ("absolute path 越权", {"files": [{"path": "D:/outside.java"}]}),
            ("traversal 越权", {"files": [{"path": "root_1/../outside.java"}]}),
            ("manifest 之外的文件", {"files": [{"path": "root_1/src/Other.java"}]}),
            ("伪造成功（error 非空）", {"error": "something failed"}),
            ("缺少字段", {"status": None}),
        ]
        for label, overrides in bad_cases:
            if overrides.get("status", "x") is None:
                # 构造真正缺少字段的 JSON，而不是 status=null
                data = write_result(handoff, task_id)
                data.pop("status")
                (handoff / "result.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            else:
                write_result(handoff, task_id, **overrides)
            bad, prep_bad = await validate_direct(validate_tool, task_id)
            check(
                f"拒绝{label}",
                bad is not None and not bad.ok and bad.status == "cc_result_invalid",
                (bad.status if bad else prep_bad.blocked_code),
            )

        # 重复 JSON 键
        raw = (
            '{"schema_version":1,"task_id":"%s","status":"failed","status":"succeeded",'
            '"summary":"x","files":[],"error":"","finished_at":"%s","truncated":false}'
            % (task_id, now_iso())
        )
        (handoff / "result.json").write_text(raw, encoding="utf-8")
        bad, _ = await validate_direct(validate_tool, task_id)
        check("拒绝重复 JSON 键", bad is not None and not bad.ok and bad.status == "cc_result_invalid")

        # 结果过大：在确认前就挡住
        (handoff / "result.json").write_text("x" * 6_000, encoding="utf-8")
        too_large_prep = await validate_tool.prepare({"task_id": task_id})
        check(
            "超大 result.json 在确认前被拒绝",
            bool(too_large_prep.blocked and too_large_prep.blocked_code == "cc_result_too_large"),
            too_large_prep.blocked_code,
        )

        # CC 明确失败 / 截断成功
        write_result(handoff, task_id, status="failed", summary="", error="compile error")
        failed, _ = await validate_direct(validate_tool, task_id)
        check("CC 报告 failed 不被当成成功", bool(failed and not failed.ok and failed.status == "cc_result_failed"))
        write_result(handoff, task_id, truncated=True)
        truncated, _ = await validate_direct(validate_tool, task_id)
        check("截断的成功结果有单独状态", bool(truncated and truncated.ok and truncated.status == "cc_result_truncated"))

        # 确认后 result.json 变化
        write_result(handoff, task_id)
        changed_prep = await validate_tool.prepare({"task_id": task_id})
        (handoff / "result.json").write_text(
            (handoff / "result.json").read_text(encoding="utf-8").replace(
                "订单状态", "确认后变化"
            ),
            encoding="utf-8",
        )
        changed = await validate_tool.execute(
            {"task_id": task_id}, injected=changed_prep.injected
        )
        check("result.json 在确认后变化会失败关闭", not changed.ok and changed.status == "cc_result_changed", changed.status)

        # 伪造服务端注入字段
        forged = await validate_tool.execute(
            {"task_id": task_id, "handoff_dir": str(handoff)}
        )
        check("模型伪造 handoff_dir 被参数校验拒绝", not forged.ok and forged.status == "invalid_argument", forged.status)
        forged_prepare = await prepare_tool.execute(
            {"goal": "伪造", "paths": [str(src)], "task_id": "cc-" + "1" * 32}
        )
        check("模型伪造 task_id 被参数校验拒绝", not forged_prepare.ok and forged_prepare.status == "invalid_argument")

        # 任务包被篡改
        task_id2, _ = await make_task(prepare_tool, [src], "篡改测试")
        handoff2 = handoff_root / task_id2
        write_result(handoff2, task_id2)
        target_file = handoff2 / "workspace" / "root_1" / "src" / "Main.java"
        target_file.write_text("tampered\n", encoding="utf-8")
        tampered, _ = await validate_direct(validate_tool, task_id2)
        check("工作区被篡改后拒绝结果", bool(tampered and not tampered.ok and tampered.status == "cc_package_invalid"), tampered.status if tampered else "")

        # ---- 授权后文件变化：不生成交接包 ----
        changed_prepare = await prepare_tool.prepare(
            {"goal": "变化测试", "paths": [str(src)]}
        )
        check("变化测试 prepare 成功", not changed_prepare.blocked)
        (src / "Main.java").write_text("changed after authorization\n", encoding="utf-8")
        changed_task_dir = handoff_root / changed_prepare.injected["task_id"]
        changed_create = await prepare_tool.execute(
            {"goal": "变化测试", "paths": [str(src)]},
            injected=changed_prepare.injected,
        )
        check(
            "授权后源文件变化会失败关闭",
            not changed_create.ok and changed_create.status == "cc_snapshot_changed",
            changed_create.status,
        )
        check("变化后没有发布交接目录", not changed_task_dir.exists())
        (src / "Main.java").write_bytes(source_before)

        # ---- 路径/敏感/上限 ----
        sensitive_prep = await prepare_tool.prepare(
            {"goal": "敏感文件", "paths": [str(src / ".env")]}
        )
        check("显式敏感文件被拒绝", sensitive_prep.blocked_code == "sensitive_file", sensitive_prep.blocked_code)

        logs_prep = await prepare_tool.prepare(
            {"goal": "日志目录", "paths": [str(src / "logs")]}
        )
        check(
            "日志目录被拒绝",
            bool(logs_prep.blocked and logs_prep.blocked_code == "path_not_allowed"),
            logs_prep.blocked_code,
        )

        outside_prep = await prepare_tool.prepare(
            {"goal": "越界", "paths": [str(outside)]}
        )
        check("允许范围外路径被拒绝", outside_prep.blocked_code == "path_not_allowed", outside_prep.blocked_code)

        if link_dir_kind or link_file_ok:
            link_path = src / "link_out" if link_dir_kind else src / "link_file.txt"
            link_prep = await prepare_tool.prepare({"goal": "链接", "paths": [str(link_path)]})
            check(
                "顶层链接/目录联接被拒绝",
                bool(link_prep.blocked and link_prep.blocked_code == "path_not_allowed"),
                link_prep.blocked_code,
            )

        # 文件数上限
        many = base / "many"
        many.mkdir()
        for index in range(3):
            (many / f"F{index}.java").write_text("class F {}\n", encoding="utf-8")
        many_config = make_config(many, allowed_roots=[str(many)], max_files=2)
        many_tool = PrepareCcHandoffTool(CcService(many_config, handoff_root=base / "handoff-many"))
        many_prep = await many_tool.prepare({"goal": "数量", "paths": [str(many)]})
        check("文件数超过上限被拒绝", many_prep.blocked_code == "cc_limit_exceeded", many_prep.blocked_code)

        # 总大小上限
        total_root = base / "total"
        total_root.mkdir()
        (total_root / "A.java").write_text("a" * 80, encoding="utf-8")
        (total_root / "B.java").write_text("b" * 80, encoding="utf-8")
        total_config = make_config(total_root, allowed_roots=[str(total_root)], max_total_bytes=100)
        total_tool = PrepareCcHandoffTool(CcService(total_config, handoff_root=base / "handoff-total"))
        total_prep = await total_tool.prepare({"goal": "总量", "paths": [str(total_root)]})
        check("总大小超过上限被拒绝", total_prep.blocked_code == "cc_limit_exceeded", total_prep.blocked_code)

        # 单文件上限
        large_root = base / "large"
        large_root.mkdir()
        (large_root / "Large.java").write_text("x" * 300, encoding="utf-8")
        large_config = make_config(large_root, allowed_roots=[str(large_root)], max_file_bytes=100)
        large_tool = PrepareCcHandoffTool(CcService(large_config, handoff_root=base / "handoff-large"))
        large_prep = await large_tool.prepare({"goal": "大文件", "paths": [str(large_root)]})
        check("单文件超过上限被拒绝", large_prep.blocked_code == "cc_limit_exceeded", large_prep.blocked_code)

        # 空白名单
        empty_config = make_config(src, allowed_roots=[])
        empty_tool = PrepareCcHandoffTool(CcService(empty_config, handoff_root=base / "handoff-empty"))
        empty_prep = await empty_tool.prepare({"goal": "空", "paths": [str(src)]})
        check("空 allowed_roots 失败关闭", empty_prep.blocked_code == "no_allowed_roots", empty_prep.blocked_code)

        # ---- 任务协议本身也严格校验 ----
        task_data = task
        task_bad_cases = [
            ("task schema_version 错误", {"schema_version": 2}),
            ("task task_id 不匹配", {"task_id": "cc-" + "2" * 32}),
            ("task read_only=false", {"read_only": False}),
            ("task manifest 越权路径", {
                "manifest": [{"path": "../outside.java", "size": 1, "sha256": "a" * 64}]
            }),
        ]
        for label, changes in task_bad_cases:
            data = json.loads(json.dumps(task_data))
            data.update(changes)
            try:
                protocol.validate_task_document(
                    data,
                    expected_task_id=task_id,
                    max_files=10,
                    max_total_bytes=5_000,
                )
                rejected = False
            except protocol.CcValidationError:
                rejected = True
            check(f"拒绝{label}", rejected)

        # ---- 静态检查：不启动 CC / shell ----
        forbidden = ("subprocess", "Popen", "os.system", "shell=True", "claude.exe", "claude ")
        source_texts = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in [
                PROJECT_ROOT / "app" / "cc" / "protocol.py",
                PROJECT_ROOT / "app" / "cc" / "snapshot.py",
                PROJECT_ROOT / "app" / "cc" / "service.py",
                PROJECT_ROOT / "app" / "tools" / "cc_tools.py",
            ]
        )
        check("交接代码不包含进程/Shell 调用", not any(item in source_texts for item in forbidden))

    print("\n== 结果 ==")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  [FAIL] {name}  {detail}")
    print(f"  {passed}/{len(RESULTS)} 通过")
    if passed != len(RESULTS):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
