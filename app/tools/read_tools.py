"""只读文件理解工具：读一个文本文件、按关键字搜索文本文件。

这两个工具只做两件事：把明确允许的文本内容读出来，或搜索匹配位置。
它们**不执行**文件内容，不写、不删、不移动、不重命名，也不接受
PowerShell/CMD/Python 之类的"顺便执行一下"。

安全边界全部在 `read_policy.py`，工具本身不拼路径、不判断权限：

  * 只认 filesystem.allowed_roots，留空就拒绝，不回退到 permissions；
  * 目标路径由 prepare() 在服务端解析并固定，执行时重新校验 + 比对指纹；
  * 内容在读之前不碰，用户拒绝就不会读；
  * 正文只给模型和当前界面，数据库只落安全摘要；
  * 两个工具都 retry_safe=False，避免工作流自动重读。

授权卡片会明确写出目标路径/搜索目录，并提示内容可能发送给模型提供商。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.errors import JiaAgentError
from app.tools import read_policy as rp
from app.tools.base import Resolution, Tool, ToolError, ToolResult

logger = logging.getLogger(__name__)

_PRIVACY_WARNING = "只读；文件内容可能发送给当前模型提供商"


def _error_text(exc: JiaAgentError) -> str:
    detail = f"（{exc.detail}）" if exc.detail else ""
    return f"{exc.message}{detail}"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（本行还有 {len(text) - limit} 个字符）"


# ----------------------------------------------------------------------
# 读文件
# ----------------------------------------------------------------------
class ReadTextFileTool(Tool):
    name = "read_text_file"
    label = "读取文件"
    description = (
        "读取一个允许目录内的文本文件，返回真实路径、大小、编码、行数，"
        "默认带行号显示正文。只读：不执行文件内容，不修改、删除或移动文件。"
        "只接受绝对路径；二进制文件、敏感凭据文件、超出大小上限的文件会被拒绝。"
        "需要用户确认后才会真正读取；授权后如果文件发生变化，会停止而不是返回旧内容。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文本文件的完整路径，例如 D:\\JiaAgent\\README.md",
            },
            "include_line_numbers": {
                "type": "boolean",
                "description": "是否在正文前显示行号，默认 true",
            },
        },
        "required": ["path"],
    }
    requires_confirmation = True
    # prepare 必须在确认前固定真实路径和文件指纹；没有注入值就不执行。
    requires_prepared_target = True
    # 读取会把内容交给模型，失败后不要自动重读/重试。
    retry_safe = False

    def __init__(self, access_policy: rp.ReadAccessPolicy | None = None) -> None:
        self._policy = access_policy

    @property
    def policy(self) -> rp.ReadAccessPolicy:
        if self._policy is None:
            self._policy = rp.ReadAccessPolicy.from_config()
        return self._policy

    # ------------------------------------------------------------------
    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        clean = super().validate(args)
        path = clean.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ToolError("path 必须是非空字符串")
        if len(path) > self.policy.max_path_chars:
            raise ToolError(f"path 太长了，上限是 {self.policy.max_path_chars} 个字符")
        include = clean.get("include_line_numbers", True)
        if not isinstance(include, bool):
            raise ToolError("include_line_numbers 必须是布尔值 true 或 false")
        return clean

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        try:
            clean = self.validate(args)
            target = self.policy.resolve_file(clean["path"])
        except ToolError as exc:
            return Resolution(
                args=args, blocked=str(exc), blocked_code="invalid_argument"
            )
        except JiaAgentError as exc:
            return Resolution(
                args=args, blocked=_error_text(exc), blocked_code=exc.code
            )
        except Exception as exc:  # noqa: BLE001 - prepare 不能把整轮对话带崩
            logger.exception("读取文件的 prepare 出错")
            return Resolution(
                args=args,
                blocked=f"准备读取时出错，已停止：{type(exc).__name__}",
                blocked_code="failed",
            )

        fingerprint = rp.FileFingerprint.from_stat(target.path, target.stat)
        return Resolution(
            args=args,
            injected={
                "resolved_path": str(target.path),
                "file_fingerprint": fingerprint.as_dict(),
            },
        )

    def check_injected(self, injected: dict[str, Any]) -> str:
        resolved = injected.get("resolved_path")
        if not isinstance(resolved, str) or not resolved:
            return "没有固定要读取的文件，已停止"
        try:
            fp = rp.FileFingerprint.from_dict(injected.get("file_fingerprint"))
        except JiaAgentError as exc:
            return _error_text(exc)
        if os.path.normcase(fp.path) != os.path.normcase(resolved):
            return "固定的文件路径和指纹不一致，已停止"
        return ""

    def describe_action(self, args: dict[str, Any]) -> str:
        target = args.get("resolved_path") or args.get("path") or "（没有路径）"
        return f"读取文件：{target}（{_PRIVACY_WARNING}）"

    async def run(
        self,
        path: str = "",
        include_line_numbers: bool = True,
        resolved_path: str = "",
        file_fingerprint: dict[str, Any] | None = None,
    ) -> ToolResult:
        if not isinstance(resolved_path, str) or not resolved_path:
            raise ToolError("缺少服务端固定的读取目标，已拒绝执行")
        if not isinstance(include_line_numbers, bool):
            raise ToolError("include_line_numbers 必须是布尔值")
        if not isinstance(file_fingerprint, dict):
            raise ToolError("缺少服务端固定的文件指纹，已拒绝执行")

        policy = self.policy
        target = await asyncio.to_thread(policy.resolve_file, resolved_path)
        await asyncio.to_thread(policy.assert_file_unchanged, target, file_fingerprint)
        text, encoding = await asyncio.to_thread(
            policy.read_text_verified, target
        )
        return self._format(target, text, encoding, include_line_numbers)

    def _format(
        self,
        target: rp.ResolvedTarget,
        text: str,
        encoding: str,
        include_line_numbers: bool,
    ) -> ToolResult:
        lines = text.splitlines()
        header = [
            f"读取完成：{target.path}",
            "类型：文本文件",
            f"大小：{target.stat.st_size} 字节",
            f"编码：{encoding}",
            f"行数：{len(lines)}",
            "-" * 48,
        ]
        header_text = "\n".join(header)
        remaining = max(0, self.policy.max_read_output_chars - len(header_text) - 2)

        body: list[str] = []
        truncated = False
        for number, line in enumerate(lines, start=1):
            rendered = f"{number:>6} | {line}" if include_line_numbers else line
            if len(rendered) + 1 > remaining:
                truncated = True
                break
            body.append(rendered)
            remaining -= len(rendered) + 1

        if not lines:
            body.append("（这是个空文件，没有正文）")
        if truncated:
            body.append("")
            body.append(
                f"[输出已截断：文件共 {len(lines)} 行，只显示了前 {len(body)} 行。"
                "需要更多内容时，请用搜索工具定位后再读。]"
            )

        output = "\n".join(header + body)
        persist = (
            f"读取文件：{target.path}"
            f"（{target.stat.st_size} 字节，{len(lines)} 行，编码 {encoding}）"
        )
        return ToolResult(
            ok=True,
            output=output,
            persist_text=persist,
            status="read_truncated" if truncated else "read_ok",
        )


# ----------------------------------------------------------------------
# 搜索文件内容
# ----------------------------------------------------------------------
@dataclass
class _Hit:
    path: str
    line_no: int
    line: str
    context: list[tuple[int, str, bool]]  # (行号, 文本, 是否命中行)


@dataclass
class _SearchOutcome:
    hits: list[_Hit] = field(default_factory=list)
    files_examined: int = 0
    truncated: bool = False
    reasons: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def files_matched(self) -> int:
        return len({hit.path for hit in self.hits})


class SearchTextFilesTool(Tool):
    name = "search_text_files"
    label = "搜索文件内容"
    description = (
        "在允许目录内的文件夹里按关键字搜索纯文本内容，返回匹配文件的路径、"
        "行号和有限的前后文。只读：不执行、不修改文件。默认不区分大小写，"
        "不支持正则表达式。会自动跳过 .git、.venv、node_modules、缓存和依赖目录、"
        "符号链接/目录联接、二进制/敏感/过大文件；达到文件数、命中数或输出长度"
        "上限时会明确说明结果已截断。需要用户确认后才会真正搜索。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要搜索的文件夹的完整路径，例如 D:\\JiaAgent\\docs",
            },
            "query": {
                "type": "string",
                "description": "要查找的纯文本关键字，不支持正则表达式",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "是否区分大小写，默认 false",
            },
            "context_lines": {
                "type": "integer",
                "description": "每处命中前后显示几行上下文，默认 1，最多 3",
            },
        },
        "required": ["path", "query"],
    }
    requires_confirmation = True
    requires_prepared_target = True
    retry_safe = False

    def __init__(self, access_policy: rp.ReadAccessPolicy | None = None) -> None:
        self._policy = access_policy

    @property
    def policy(self) -> rp.ReadAccessPolicy:
        if self._policy is None:
            self._policy = rp.ReadAccessPolicy.from_config()
        return self._policy

    # ------------------------------------------------------------------
    def _validate_search_args(
        self, args: dict[str, Any]
    ) -> tuple[str, str, bool, int]:
        clean = super().validate(args)
        path = clean.get("path")
        query = clean.get("query")
        case_sensitive = clean.get("case_sensitive", False)
        context_lines = clean.get("context_lines", 1)

        if not isinstance(path, str) or not path.strip():
            raise ToolError("path 必须是非空字符串")
        if len(path) > self.policy.max_path_chars:
            raise ToolError(f"path 太长了，上限是 {self.policy.max_path_chars} 个字符")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query 必须是非空字符串")
        if len(query) > self.policy.max_query_chars:
            raise ToolError(
                f"query 太长了，上限是 {self.policy.max_query_chars} 个字符"
            )
        if any(ch in query for ch in "\x00\r\n"):
            raise ToolError("query 不能包含换行或空字符")
        if not isinstance(case_sensitive, bool):
            raise ToolError("case_sensitive 必须是布尔值 true 或 false")
        if isinstance(context_lines, bool) or not isinstance(context_lines, int):
            raise ToolError("context_lines 必须是整数")
        if context_lines < 0 or context_lines > self.policy.max_context_lines:
            raise ToolError(
                f"context_lines 必须在 0 到 {self.policy.max_context_lines} 之间"
            )
        return path, query, case_sensitive, context_lines

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        try:
            path, _query, _case, _ctx = self._validate_search_args(args)
            target = self.policy.resolve_dir(path)
        except ToolError as exc:
            return Resolution(
                args=args, blocked=str(exc), blocked_code="invalid_argument"
            )
        except JiaAgentError as exc:
            return Resolution(
                args=args, blocked=_error_text(exc), blocked_code=exc.code
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("搜索文件的 prepare 出错")
            return Resolution(
                args=args,
                blocked=f"准备搜索时出错，已停止：{type(exc).__name__}",
                blocked_code="failed",
            )

        fingerprint = rp.DirFingerprint.from_stat(target.path, target.stat)
        return Resolution(
            args=args,
            injected={
                "resolved_dir": str(target.path),
                "dir_fingerprint": fingerprint.as_dict(),
            },
        )

    def check_injected(self, injected: dict[str, Any]) -> str:
        resolved = injected.get("resolved_dir")
        if not isinstance(resolved, str) or not resolved:
            return "没有固定要搜索的文件夹，已停止"
        try:
            fp = rp.DirFingerprint.from_dict(injected.get("dir_fingerprint"))
        except JiaAgentError as exc:
            return _error_text(exc)
        if os.path.normcase(fp.path) != os.path.normcase(resolved):
            return "固定的文件夹路径和指纹不一致，已停止"
        return ""

    def describe_action(self, args: dict[str, Any]) -> str:
        target = args.get("resolved_dir") or args.get("path") or "（没有路径）"
        query = args.get("query") or ""
        return (
            f"搜索文件内容：在 {target} 中查找「{query}」"
            f"（{_PRIVACY_WARNING}）"
        )

    async def run(
        self,
        path: str = "",
        query: str = "",
        case_sensitive: bool = False,
        context_lines: int = 1,
        resolved_dir: str = "",
        dir_fingerprint: dict[str, Any] | None = None,
    ) -> ToolResult:
        if not isinstance(resolved_dir, str) or not resolved_dir:
            raise ToolError("缺少服务端固定的搜索目标，已拒绝执行")
        if not isinstance(dir_fingerprint, dict):
            raise ToolError("缺少服务端固定的文件夹指纹，已拒绝执行")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query 必须是非空字符串")
        if len(query) > self.policy.max_query_chars:
            raise ToolError("query 太长")
        if any(ch in query for ch in "\x00\r\n"):
            raise ToolError("query 不能包含换行或空字符")
        if not isinstance(case_sensitive, bool):
            raise ToolError("case_sensitive 必须是布尔值")
        if isinstance(context_lines, bool) or not isinstance(context_lines, int):
            raise ToolError("context_lines 必须是整数")
        if context_lines < 0 or context_lines > self.policy.max_context_lines:
            raise ToolError("context_lines 超出允许范围")

        policy = self.policy
        root = await asyncio.to_thread(policy.resolve_dir, resolved_dir)
        await asyncio.to_thread(policy.assert_dir_unchanged, root, dir_fingerprint)
        outcome = await asyncio.to_thread(
            self._search_sync, root, query, case_sensitive, context_lines
        )
        return self._format_search(root, query, case_sensitive, outcome)

    # ------------------------------------------------------------------
    def _search_sync(
        self,
        root: rp.ResolvedTarget,
        query: str,
        case_sensitive: bool,
        context_lines: int,
    ) -> _SearchOutcome:
        policy = self.policy
        outcome = _SearchOutcome(skipped={
            "sensitive": 0,
            "blocked": 0,
            "too_large": 0,
            "binary": 0,
            "encoding": 0,
            "changed": 0,
            "unreadable": 0,
            "links": 0,
            "excluded_dirs": 0,
        })
        needle = query if case_sensitive else query.casefold()
        stop = False
        stack = [root.path]

        while stack and not stop:
            directory = stack.pop()
            try:
                iterator = os.scandir(directory)
            except PermissionError as exc:
                if directory == root.path:
                    raise rp.ReadFailed(
                        f"没有权限打开搜索文件夹：{root.path}（{exc}）"
                    ) from exc
                outcome.skipped["unreadable"] += 1
                continue
            except OSError as exc:
                if directory == root.path:
                    raise rp.ReadFailed(
                        f"打不开搜索文件夹：{root.path}（{exc}）"
                    ) from exc
                outcome.skipped["unreadable"] += 1
                continue

            with iterator:
                for entry in iterator:
                    # 符号链接、目录联接、reparse point 一律不跟。跟着走可能
                    # 跳出 allowed_roots，也可能绕成环。
                    if rp.is_link_like(entry):
                        outcome.skipped["links"] += 1
                        continue

                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_file = entry.is_file(follow_symlinks=False)
                    except OSError:
                        outcome.skipped["unreadable"] += 1
                        continue

                    if is_dir:
                        child = Path(entry.path)
                        if rp.is_excluded_dir_name(entry.name):
                            outcome.skipped["excluded_dirs"] += 1
                            continue
                        prune = policy.prune_reason(child)
                        if prune:
                            key = prune if prune in outcome.skipped else "blocked"
                            outcome.skipped[key] += 1
                            continue
                        stack.append(child)
                        continue

                    if not is_file:
                        continue

                    if outcome.files_examined >= policy.max_search_files:
                        outcome.truncated = True
                        outcome.reasons.append(
                            f"达到文件数上限 {policy.max_search_files}"
                        )
                        stop = True
                        break

                    outcome.files_examined += 1
                    candidate = Path(entry.path)
                    try:
                        target = policy.resolve_file(
                            str(candidate), max_bytes=policy.max_search_file_bytes
                        )
                    except rp.SensitiveFile:
                        outcome.skipped["sensitive"] += 1
                        continue
                    except rp.FileTooLarge:
                        outcome.skipped["too_large"] += 1
                        continue
                    except rp.ReadPathNotFound:
                        outcome.skipped["unreadable"] += 1
                        continue
                    except rp.PathNotAllowed:
                        outcome.skipped["blocked"] += 1
                        continue
                    except (JiaAgentError, OSError):
                        outcome.skipped["unreadable"] += 1
                        continue

                    try:
                        text, _encoding = policy.read_text_verified(
                            target, max_bytes=policy.max_search_file_bytes
                        )
                    except rp.BinaryFile:
                        outcome.skipped["binary"] += 1
                        continue
                    except rp.UnsupportedEncoding:
                        outcome.skipped["encoding"] += 1
                        continue
                    except rp.SensitiveFile:
                        outcome.skipped["sensitive"] += 1
                        continue
                    except rp.FileTooLarge:
                        outcome.skipped["too_large"] += 1
                        continue
                    except rp.FileChanged:
                        outcome.skipped["changed"] += 1
                        continue
                    except (JiaAgentError, OSError):
                        outcome.skipped["unreadable"] += 1
                        continue

                    lines = text.splitlines()
                    for index, line in enumerate(lines):
                        haystack = line if case_sensitive else line.casefold()
                        if needle not in haystack:
                            continue
                        if len(outcome.hits) >= policy.max_search_results:
                            outcome.truncated = True
                            outcome.reasons.append(
                                f"达到命中数上限 {policy.max_search_results}"
                            )
                            stop = True
                            break
                        start = max(0, index - context_lines)
                        end = min(len(lines), index + context_lines + 1)
                        context = [
                            (i + 1, lines[i], i == index)
                            for i in range(start, end)
                        ]
                        outcome.hits.append(
                            _Hit(
                                path=str(target.path),
                                line_no=index + 1,
                                line=line,
                                context=context,
                            )
                        )
                    if stop:
                        break

        # 文件系统遍历顺序不稳定；按路径和行号排一下，结果稳定、好核对。
        outcome.hits.sort(key=lambda hit: (hit.path.casefold(), hit.line_no))
        return outcome

    def _format_search(
        self,
        root: rp.ResolvedTarget,
        query: str,
        case_sensitive: bool,
        outcome: _SearchOutcome,
    ) -> ToolResult:
        policy = self.policy
        found = len(outcome.hits)
        matched_files = outcome.files_matched
        truncated = outcome.truncated
        reasons = list(outcome.reasons)

        first = (
            f"搜索完成：在 {root.path} 中查找「{query}」"
            f"（{'区分' if case_sensitive else '不区分'}大小写）"
        )
        summary_line = (
            f"扫描 {outcome.files_examined} 个文件，找到 {found} 处命中"
            f"（涉及 {matched_files} 个文件）"
        )

        skipped_parts = []
        labels = {
            "sensitive": "敏感/受保护",
            "blocked": "被规则禁止",
            "too_large": "过大",
            "binary": "二进制",
            "encoding": "编码不支持",
            "changed": "读取时变化",
            "unreadable": "读取失败",
            "links": "链接/联接",
            "excluded_dirs": "依赖/缓存目录",
        }
        for key, label in labels.items():
            count = outcome.skipped.get(key, 0)
            if count:
                skipped_parts.append(f"{label} {count}")

        header = [first, summary_line]
        if skipped_parts:
            header.append("跳过：" + "、".join(skipped_parts))
        header.append("-" * 48)

        header_text = "\n".join(header)
        # 预留一点空间：截断时首行/摘要会变长，而且最后还要写截断原因。
        remaining = max(
            0, policy.max_search_output_chars - len(header_text) - 2 - 512
        )
        chunks: list[str] = []
        shown = 0
        for hit in outcome.hits:
            block_lines = [f"{hit.path}:{hit.line_no}"]
            for line_no, text, is_match in hit.context:
                marker = ">" if is_match else " "
                block_lines.append(
                    f"  {marker} {line_no:>6} | {_clip(text, policy.max_line_chars)}"
                )
            chunk = "\n".join(block_lines)
            if len(chunk) + 2 > remaining:
                truncated = True
                reasons.append("达到输出长度上限")
                break
            chunks.append(chunk)
            remaining -= len(chunk) + 2
            shown += 1

        if truncated:
            first = first.replace("搜索完成：", "搜索完成（结果已截断）：", 1)
            header[0] = first
            summary_line = (
                f"扫描 {outcome.files_examined} 个文件，找到 {found} 处命中"
                f"（涉及 {matched_files} 个文件）"
            )
            if shown != found:
                summary_line += f"，已显示前 {shown} 处"
            header[1] = summary_line

        if not chunks:
            chunks.append(
                "没有找到匹配的内容。"
                if found == 0
                else "命中内容因输出长度上限未能显示。"
            )

        if truncated:
            unique_reasons = list(dict.fromkeys(reasons))
            chunks.append("")
            chunks.append("[已截断：" + "；".join(unique_reasons) + "]")

        output = "\n".join(header + chunks)
        persist = (
            f"搜索「{query}」：扫描 {outcome.files_examined} 个文件，"
            f"找到 {found} 处命中（涉及 {matched_files} 个文件）"
            + ("；结果已截断" if truncated else "")
        )
        return ToolResult(
            ok=True,
            output=output,
            persist_text=persist,
            status="search_truncated" if truncated else "search_ok",
        )
