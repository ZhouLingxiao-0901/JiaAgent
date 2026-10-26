"""CC 人工交接的任务/结果协议。

两个文件都是**不可信输入**：task.json 可能在交接过程中被改，result.json
更是 Claude Code 的产物。这里的原则是严格、版本化、失败关闭：

  * 只接受 JSON 对象；重复键、NaN/Infinity 一律拒绝；
  * 字段集合精确匹配，类型精确匹配（bool 不算 int）；
  * 路径只能是安全的相对路径，不能是绝对路径、盘符、UNC、.. 或链接；
  * result 的 task_id 必须与 task 一致，引用的文件必须在 manifest 里；
  * `succeeded` 但带非空 error 不算成功；
  * 任何一条不满足就抛 `CcValidationError`，绝不“尽量解析”。

这里只做数据校验，不读文件、不执行任何内容。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.errors import JiaAgentError

TASK_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1

RESULT_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled", "invalid"})

TASK_KEYS = frozenset({
    "schema_version",
    "task_id",
    "goal",
    "allowed_paths",
    "read_only",
    "created_at",
    "deadline",
    "manifest",
})
MANIFEST_KEYS = frozenset({"schema_version", "task_id", "files"})
MANIFEST_ENTRY_KEYS = frozenset({"path", "size", "sha256"})
RESULT_KEYS = frozenset({
    "schema_version",
    "task_id",
    "status",
    "summary",
    "files",
    "error",
    "finished_at",
    "truncated",
})
RESULT_FILE_KEYS = frozenset({"path", "note"})

TASK_ID_RE = re.compile(r"^cc-[0-9a-f]{32}\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

MAX_REL_PATH_CHARS = 1024
MAX_SUMMARY_CHARS = 8_000
MAX_ERROR_CHARS = 2_000
MAX_NOTE_CHARS = 500

_WINDOWS_RESERVED = frozenset({
    "con", "prn", "aux", "nul", "clock$",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})
_WINDOWS_BAD_CHARS = frozenset('<>:"|?*')


class CcValidationError(JiaAgentError):
    """协议不合法。code 会变成界面状态，调用方不得把它当成成功。"""

    code = "cc_result_invalid"
    http_status = 400


# ----------------------------------------------------------------------
# 基础校验
# ----------------------------------------------------------------------
def _fail(message: str) -> None:
    raise CcValidationError(message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_text(value: Any, *, field: str, max_chars: int) -> str:
    """校验进入协议/界面的自由文本。

    允许中文、换行、制表符和 emoji；拒绝未配对代理项、C0 控制字符
    （换行和制表符除外）、DEL 和 C1 控制字符。返回值就是原字符串。
    """
    if not isinstance(value, str):
        _fail(f"{field} 必须是字符串")
    if len(value) > max_chars:
        _fail(f"{field} 太长（上限 {max_chars} 个字符）")
    for ch in value:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            _fail(f"{field} 不能包含未配对的 Unicode 代理项")
        if ch in ("\n", "\t"):
            continue
        if code < 0x20 or 0x7F <= code <= 0x9F:
            _fail(f"{field} 不能包含控制字符")
    return value


def _parse_time(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} 必须是 ISO 8601 时间字符串")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _fail(f"{field} 不是有效的 ISO 8601 时间")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(f"{field} 必须包含时区")
    return parsed


def safe_relative_path(value: Any, *, field: str = "路径") -> str:
    """校验一个工作区相对路径（统一使用 `/` 分隔）。

    只接受普通相对路径。绝对路径、盘符、UNC、设备路径、`..`、反斜杠、
    Windows 保留名、控制字符一律拒绝。这个函数既用于协议字段，也用于
    服务端落盘前最后一道检查。
    """
    if not isinstance(value, str):
        _fail(f"{field} 必须是字符串")
    if not value or value != value.strip():
        _fail(f"{field} 不能为空，也不能有首尾空白")
    if len(value) > MAX_REL_PATH_CHARS:
        _fail(f"{field} 太长（上限 {MAX_REL_PATH_CHARS} 个字符）")
    if "\\" in value:
        _fail(f"{field} 必须使用 `/` 作为分隔符")
    if value.startswith("/") or value.startswith("//"):
        _fail(f"{field} 必须是相对路径，不能以 / 开头")
    if re.match(r"^[A-Za-z]:", value):
        _fail(f"{field} 不能包含盘符")
    for ch in value:
        code = ord(ch)
        if code < 32 or code == 127:
            _fail(f"{field} 不能包含控制字符")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _fail(f"{field} 不能包含空段、`.` 或 `..`")
    for part in parts:
        if any(ch in _WINDOWS_BAD_CHARS for ch in part):
            _fail(f"{field} 包含 Windows 不允许的字符")
        if part.endswith(".") or part.endswith(" "):
            _fail(f"{field} 的路径段不能以点或空格结尾")
        stem = part.split(".", 1)[0].casefold()
        if stem in _WINDOWS_RESERVED:
            _fail(f"{field} 包含 Windows 保留名称「{part}」")
    return value


def is_under(path: str, prefix: str) -> bool:
    """path 是否等于 prefix 或位于 prefix 下面（两者都必须是安全相对路径）。"""
    return path == prefix or path.startswith(prefix + "/")


def parse_json_strict(text: str) -> Any:
    """解析严格 JSON：拒绝重复键、NaN、Infinity，不尝试修复。

    CC 的结果必须是一份完整的 JSON 文档。Markdown 代码围栏、前后说明、
    尾随逗号都不是“差不多能用”，而是格式错误。
    """

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail(f"JSON 里出现重复字段：{key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        _fail(f"JSON 里不允许出现 {value}")

    try:
        return json.loads(
            text,
            object_pairs_hook=no_duplicates,
            parse_constant=reject_constant,
        )
    except CcValidationError:
        raise
    except RecursionError as exc:
        raise CcValidationError("JSON 嵌套层级过深，已拒绝") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CcValidationError(f"不是有效的 JSON：{exc}") from exc
    except ValueError as exc:
        # Python 3.11+ 会限制超长整数字符串的转换，json.loads 因此抛出的
        # ValueError 也属于 JSON 输入不合法；必须映射成协议错误，不能
        # 让它作为未分类异常越过服务/工具的错误契约。
        raise CcValidationError(f"不是有效的 JSON：{exc}") from exc


# ----------------------------------------------------------------------
# 任务
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ManifestEntry:
    path: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class TaskDocument:
    schema_version: int
    task_id: str
    goal: str
    allowed_paths: tuple[str, ...]
    read_only: bool
    created_at: str
    deadline: str
    manifest: tuple[ManifestEntry, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "goal": self.goal,
            "allowed_paths": list(self.allowed_paths),
            "read_only": self.read_only,
            "created_at": self.created_at,
            "deadline": self.deadline,
            "manifest": [entry.as_dict() for entry in self.manifest],
        }

    def as_manifest_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "files": [entry.as_dict() for entry in self.manifest],
        }


def validate_task_document(
    data: Any,
    *,
    expected_task_id: str,
    max_files: int,
    max_total_bytes: int,
    max_goal_chars: int = 4_000,
    max_paths: int = 20,
) -> TaskDocument:
    if not isinstance(data, dict):
        _fail("task.json 的顶层必须是一个对象")
    if set(data) != TASK_KEYS:
        missing = TASK_KEYS - set(data)
        extra = set(data) - TASK_KEYS
        parts = []
        if missing:
            parts.append(f"缺少 {', '.join(sorted(missing))}")
        if extra:
            parts.append(f"多出 {', '.join(sorted(extra))}")
        _fail("task.json 字段不合法：" + "；".join(parts))

    if not _is_int(data.get("schema_version")) or data["schema_version"] != TASK_SCHEMA_VERSION:
        _fail(f"task.json 的 schema_version 必须是 {TASK_SCHEMA_VERSION}")

    task_id = data.get("task_id")
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        _fail("task_id 格式不合法")
    if task_id != expected_task_id:
        _fail("task_id 与交接目录不一致")

    goal = data.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        _fail("goal 必须是非空字符串")
    goal = validate_text(goal, field="goal", max_chars=max_goal_chars)

    if data.get("read_only") is not True:
        _fail("read_only 必须为 true；本阶段只接受只读分析任务")

    created_at = data.get("created_at")
    deadline = data.get("deadline")
    created = _parse_time(created_at, field="created_at")
    ends = _parse_time(deadline, field="deadline")
    if ends <= created:
        _fail("deadline 必须晚于 created_at")

    raw_allowed = data.get("allowed_paths")
    if not isinstance(raw_allowed, list) or not raw_allowed:
        _fail("allowed_paths 必须是非空数组")
    if len(raw_allowed) > max_paths:
        _fail(f"allowed_paths 太多（上限 {max_paths} 个）")
    allowed: list[str] = []
    for item in raw_allowed:
        path = safe_relative_path(item, field="allowed_paths 中的路径")
        if path in allowed:
            _fail(f"allowed_paths 里有重复路径：{path}")
        allowed.append(path)

    raw_manifest = data.get("manifest")
    if not isinstance(raw_manifest, list) or not raw_manifest:
        _fail("manifest 必须是非空数组")
    if len(raw_manifest) > max_files:
        _fail(f"manifest 文件数超过上限 {max_files}")

    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    total = 0
    for raw in raw_manifest:
        if not isinstance(raw, dict) or set(raw) != MANIFEST_ENTRY_KEYS:
            _fail("manifest 中每一项必须且只能包含 path、size、sha256")
        path = safe_relative_path(raw.get("path"), field="manifest 路径")
        if path in seen:
            _fail(f"manifest 里有重复文件：{path}")
        seen.add(path)
        if not any(is_under(path, prefix) for prefix in allowed):
            _fail(f"manifest 文件不在 allowed_paths 范围内：{path}")
        size = raw.get("size")
        if not _is_int(size) or size < 0:
            _fail(f"manifest 文件大小不合法：{path}")
        digest = raw.get("sha256")
        if not isinstance(digest, str) or not SHA256_RE.match(digest):
            _fail(f"manifest 的 sha256 不合法：{path}")
        total += size
        if total > max_total_bytes:
            _fail(f"manifest 总大小超过上限 {max_total_bytes} 字节")
        entries.append(ManifestEntry(path=path, size=size, sha256=digest))

    return TaskDocument(
        schema_version=TASK_SCHEMA_VERSION,
        task_id=task_id,
        goal=goal,
        allowed_paths=tuple(allowed),
        read_only=True,
        created_at=created.isoformat(),
        deadline=ends.isoformat(),
        manifest=tuple(entries),
    )


def validate_manifest_document(data: Any, task: TaskDocument) -> None:
    if not isinstance(data, dict) or set(data) != MANIFEST_KEYS:
        _fail("manifest.json 字段不合法")
    if not _is_int(data.get("schema_version")) or data["schema_version"] != TASK_SCHEMA_VERSION:
        _fail("manifest.json 的 schema_version 不合法")
    if data.get("task_id") != task.task_id:
        _fail("manifest.json 的 task_id 与 task.json 不一致")
    files = data.get("files")
    if not isinstance(files, list) or len(files) != len(task.manifest):
        _fail("manifest.json 与 task.json 的文件清单数量不一致")
    for raw, expected in zip(files, task.manifest):
        if not isinstance(raw, dict) or set(raw) != MANIFEST_ENTRY_KEYS:
            _fail("manifest.json 的文件项格式不合法")
        if raw != expected.as_dict():
            _fail("manifest.json 与 task.json 的文件清单不一致")


# ----------------------------------------------------------------------
# 结果
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ResultFile:
    path: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "note": self.note}


@dataclass(frozen=True)
class ResultDocument:
    schema_version: int
    task_id: str
    status: str
    summary: str
    files: tuple[ResultFile, ...]
    error: str
    finished_at: str
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "files": [item.as_dict() for item in self.files],
            "error": self.error,
            "finished_at": self.finished_at,
            "truncated": self.truncated,
        }


def validate_result_document(data: Any, task: TaskDocument) -> ResultDocument:
    if not isinstance(data, dict):
        _fail("result.json 的顶层必须是一个对象")
    if set(data) != RESULT_KEYS:
        missing = RESULT_KEYS - set(data)
        extra = set(data) - RESULT_KEYS
        parts = []
        if missing:
            parts.append(f"缺少 {', '.join(sorted(missing))}")
        if extra:
            parts.append(f"多出 {', '.join(sorted(extra))}")
        _fail("result.json 字段不合法：" + "；".join(parts))

    if not _is_int(data.get("schema_version")) or data["schema_version"] != RESULT_SCHEMA_VERSION:
        _fail(f"result.json 的 schema_version 必须是 {RESULT_SCHEMA_VERSION}")

    if data.get("task_id") != task.task_id:
        _fail("result.json 的 task_id 与任务不一致")

    status = data.get("status")
    if not isinstance(status, str) or status not in RESULT_STATUSES:
        _fail(f"status 不合法，只能是：{', '.join(sorted(RESULT_STATUSES))}")

    summary = validate_text(
        data.get("summary"), field="summary", max_chars=MAX_SUMMARY_CHARS
    )

    error = validate_text(data.get("error"), field="error", max_chars=MAX_ERROR_CHARS)

    truncated = data.get("truncated")
    if not isinstance(truncated, bool):
        _fail("truncated 必须是布尔值")

    finished_at = _parse_time(data.get("finished_at"), field="finished_at")

    manifest_paths = {entry.path for entry in task.manifest}
    raw_files = data.get("files")
    if not isinstance(raw_files, list):
        _fail("files 必须是数组")
    if len(raw_files) > len(task.manifest):
        _fail("files 引用的文件数量超过 manifest")
    files: list[ResultFile] = []
    seen: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict) or not set(raw) <= RESULT_FILE_KEYS:
            _fail("files 中每一项只能包含 path 和可选的 note")
        if "path" not in raw:
            _fail("files 中每一项都必须有 path")
        path = safe_relative_path(raw.get("path"), field="结果文件路径")
        if path not in manifest_paths:
            _fail(f"结果引用了 manifest 之外的文件：{path}")
        if path in seen:
            _fail(f"结果里有重复文件：{path}")
        seen.add(path)
        note = raw.get("note", "")
        if not isinstance(note, str):
            _fail(f"note 必须是字符串：{path}")
        note = validate_text(note, field="note", max_chars=MAX_NOTE_CHARS)
        files.append(ResultFile(path=path, note=note))

    # 声称成功却带着错误，是自相矛盾；不能把它当成功再交给模型。
    if status == "succeeded":
        if not summary.strip():
            _fail("status 为 succeeded 时 summary 不能为空")
        if error.strip():
            _fail("status 为 succeeded 时 error 必须为空")

    return ResultDocument(
        schema_version=RESULT_SCHEMA_VERSION,
        task_id=task.task_id,
        status=status,
        summary=summary,
        files=tuple(files),
        error=error,
        finished_at=finished_at.isoformat(),
        truncated=truncated,
    )
