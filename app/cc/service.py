"""CC 人工交接服务：准备快照、管理交接目录、验证结果。

流程（与 docs/cc-handoff.md 一致）：

  模型请求 prepare_cc_handoff
    -> prepare() 只校验参数/路径/大小/敏感规则，列出文件（只 stat）
    -> 闸门弹确认卡片；用户拒绝则这里不会被调用
    -> run() 复制受限快照，写 task.json / manifest.json
    -> 用户手工交给 CC
    -> 用户把 result.json 放回同一个 <task_id> 目录
    -> validate_cc_result 的 prepare() 只 stat result.json，并读 task.json
       检查 deadline（不读 result.json 正文）
    -> 用户确认后，run() 读取并严格验证结果

这里不启动 CC、不调用 CLI、不执行 shell、不修改原始项目文件。
"""

from __future__ import annotations

import logging
import os
import shutil
import stat as statmod
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.cc import protocol
from app.cc import snapshot
from app.cc.protocol import ResultDocument, TaskDocument
from app.cc.snapshot import (
    CcCollection,
    CcLimitExceeded,
    CcPackageInvalid,
    CcPolicy,
    CcResultChanged,
    CcResultTooLarge,
)
from app.errors import JiaAgentError
from app.settings import DATA_DIR, CcConfig, get_tools_config

logger = logging.getLogger(__name__)


class CcTaskNotFound(JiaAgentError):
    code = "cc_task_not_found"
    http_status = 404


class CcResultNotFound(JiaAgentError):
    code = "cc_result_not_found"
    http_status = 404


class CcTaskExpired(JiaAgentError):
    code = "cc_task_expired"
    http_status = 410


# result.json 不是可验证的普通文件时，语义与协议校验失败一致；复用同一个
# 错误类型，避免出现第二套 cc_result_invalid 实现。
CcResultInvalid = protocol.CcValidationError


@dataclass(frozen=True)
class ResultTarget:
    task_id: str
    handoff_dir: Path
    result_path: Path
    fingerprint: dict[str, Any]


@dataclass(frozen=True)
class ResultValidation:
    task: TaskDocument
    result: ResultDocument
    workspace_verified: bool


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _fingerprint(
    path: Path, *, st: os.stat_result | None = None
) -> dict[str, Any]:
    # 用 lstat：链接本身的身份也会进入指纹，不能用跟随链接的 stat。
    current = os.lstat(path) if st is None else st
    return {
        "path": str(path),
        "size": int(current.st_size),
        "mtime_ns": int(current.st_mtime_ns),
        "dev": int(current.st_dev),
        "ino": int(current.st_ino),
    }


def _ensure_task_not_expired(
    task: TaskDocument, *, now: datetime | None = None
) -> None:
    """在服务入口检查 deadline。

    prepare_result 和 validate_result 都会调用本函数；validate_result 在
    第二次读取 task.json 后还会再检查一次，防止两次读取之间任务包被替换。
    所有比较都要求带时区。注入 now 只用于测试，生产调用使用系统本地时间。
    """
    current = _now() if now is None else now
    if not isinstance(current, datetime):
        raise CcTaskExpired("当前时间无效，已停止验证")
    if current.tzinfo is None or current.utcoffset() is None:
        raise CcTaskExpired("当前时间缺少时区，已停止验证")
    try:
        deadline = datetime.fromisoformat(task.deadline)
    except (TypeError, ValueError) as exc:
        raise CcPackageInvalid("任务 deadline 无法解析，已停止验证") from exc
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise CcPackageInvalid("任务 deadline 缺少时区，已停止验证")
    if current >= deadline:
        raise CcTaskExpired(
            f"任务 {task.task_id} 已于 {task.deadline} 过期；"
            "请重新准备交接包，不会读取或展示这次结果。"
        )


def validate_result_fingerprint(data: Any, expected_path: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise CcResultChanged("结果文件指纹无效，请重新发起验证")
    keys = {"path", "size", "mtime_ns", "dev", "ino"}
    if set(data) != keys or not isinstance(data.get("path"), str):
        raise CcResultChanged("结果文件指纹无效，请重新发起验证")
    if os.path.normcase(data["path"]) != os.path.normcase(expected_path):
        raise CcResultChanged("结果文件路径与任务目录不一致，请重新发起验证")
    values: dict[str, Any] = {"path": data["path"]}
    for key in ("size", "mtime_ns", "dev", "ino"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CcResultChanged("结果文件指纹无效，请重新发起验证")
        values[key] = value
    return values


def _fingerprint_matches(fingerprint: dict[str, Any], st: os.stat_result) -> bool:
    return (
        int(st.st_size) == fingerprint["size"]
        and int(st.st_mtime_ns) == fingerprint["mtime_ns"]
        and int(st.st_dev) == fingerprint["dev"]
        and int(st.st_ino) == fingerprint["ino"]
    )


class CcService:
    """交接目录和策略的唯一入口。工具本身不拼路径、不判断权限。"""

    def __init__(
        self,
        config: CcConfig | None = None,
        *,
        handoff_root: Path | str | None = None,
    ) -> None:
        self.config = config if config is not None else get_tools_config().cc
        self.policy = CcPolicy.from_config(self.config)
        root = Path(handoff_root) if handoff_root is not None else DATA_DIR / "cc-handoff"
        # realpath 把已存在的父目录链接也解析掉，避免交接目录被父级链接带出去。
        self.handoff_root = Path(os.path.realpath(root))

    # ------------------------------------------------------------------
    @property
    def max_result_bytes(self) -> int:
        return self.policy.max_result_bytes

    @property
    def max_paths(self) -> int:
        return self.policy.max_paths

    @property
    def max_goal_chars(self) -> int:
        return self.policy.max_goal_chars

    @staticmethod
    def new_task_id() -> str:
        return f"cc-{uuid.uuid4().hex}"

    def handoff_dir_for(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not protocol.TASK_ID_RE.match(task_id):
            raise CcTaskNotFound("任务编号格式不合法")
        root = self.handoff_root
        target = root / task_id
        try:
            root_n = os.path.normcase(os.path.abspath(os.path.realpath(root)))
            target_n = os.path.normcase(os.path.abspath(os.path.realpath(target)))
            if os.path.commonpath([root_n, target_n]) != root_n:
                raise CcTaskNotFound("任务目录不在交接根目录内")
        except ValueError as exc:
            raise CcTaskNotFound("任务目录不合法") from exc
        if snapshot.path_is_link_like(target):
            raise CcPackageInvalid("任务目录是链接或目录联接，已拒绝")
        return target

    # ------------------------------------------------------------------
    def plan(self, paths: list[str]) -> CcCollection:
        """确认前调用：只校验和枚举，不读正文、不写文件。"""
        self.policy.ensure_can_create()
        return snapshot.collect_candidates(self.policy, paths)

    # ------------------------------------------------------------------
    def create_handoff(
        self,
        *,
        task_id: str,
        goal: str,
        collection: CcCollection,
    ) -> tuple[TaskDocument, Path]:
        """用户确认后调用：复制快照并原子地发布交接目录。"""
        # 必须在创建交接根目录之前确认 CC 自己的白名单有效。
        self.policy.ensure_can_create()
        target = self.handoff_dir_for(task_id)
        if target.exists():
            raise CcPackageInvalid(
                f"任务目录已存在：{task_id}；不会覆盖已有交接包，请重新发起"
            )

        if not collection.candidates:
            raise snapshot.CcSnapshotFailed("没有要复制的文件，已停止")
        if not collection.allowed_paths:
            raise snapshot.CcSnapshotFailed("没有授权范围，已停止")
        for prefix in collection.allowed_paths:
            protocol.safe_relative_path(prefix, field="授权范围")
        seen: set[str] = set()
        for candidate in collection.candidates:
            protocol.safe_relative_path(candidate.relpath, field="快照文件路径")
            key = snapshot.relpath_key(candidate.relpath)
            if key in seen:
                raise snapshot.CcSnapshotFailed(
                    "文件清单里有重复路径（Windows 下仅大小写不同也算重复），已停止"
                )
            seen.add(key)
            if not any(
                protocol.is_under(candidate.relpath, prefix)
                for prefix in collection.allowed_paths
            ):
                raise snapshot.CcSnapshotFailed("文件不在授权范围内，已停止")

        total = sum(candidate.size for candidate in collection.candidates)
        if len(collection.candidates) > self.policy.max_files:
            raise CcLimitExceeded("文件数超过当前 cc.max_files 上限")
        if total > self.policy.max_total_bytes:
            raise CcLimitExceeded("文件总大小超过当前 cc.max_total_bytes 上限")
        if collection.total_bytes != total:
            raise snapshot.CcSnapshotFailed("文件总大小与清单不一致，已停止")

        created = _now()
        deadline = created + timedelta(hours=self.policy.deadline_hours)
        try:
            self.handoff_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise snapshot.CcSnapshotFailed(
                f"无法创建交接根目录（{exc}）"
            ) from exc
        if snapshot.path_is_link_like(self.handoff_root):
            raise CcPackageInvalid("交接根目录是链接或目录联接，已拒绝")

        temp = self.handoff_root / f".tmp-{task_id}-{uuid.uuid4().hex[:8]}"
        temp_created = False
        published = False
        try:
            temp.mkdir(parents=False, exist_ok=False)
            temp_created = True
            workspace = temp / "workspace"
            entries = snapshot.copy_workspace(self.policy, collection, workspace)

            task = TaskDocument(
                schema_version=protocol.TASK_SCHEMA_VERSION,
                task_id=task_id,
                goal=goal,
                allowed_paths=collection.allowed_paths,
                read_only=True,
                created_at=_iso(created),
                deadline=_iso(deadline),
                manifest=entries,
            )
            # 发布前用同一套协议再校验一次，防止服务端自己生成出畸形包。
            protocol.validate_task_document(
                task.as_dict(),
                expected_task_id=task_id,
                max_files=self.policy.max_files,
                max_total_bytes=self.policy.max_total_bytes,
                max_goal_chars=self.policy.max_goal_chars,
                max_paths=self.policy.max_paths,
            )
            snapshot.write_json(temp / "task.json", task.as_dict())
            snapshot.write_json(temp / "manifest.json", task.as_manifest_dict())
            os.rename(temp, target)
            published = True
            return task, target
        except BaseException:
            # 只清理本次调用确实创建、且尚未发布到正式任务目录的临时目录；
            # rename 成功后即使随后出现异常，也不能删掉已经发布的目录。
            if temp_created and not published:
                try:
                    shutil.rmtree(temp)
                except FileNotFoundError:
                    pass
                except BaseException as cleanup_exc:
                    # 清理失败不能覆盖原始异常；把清理错误保留在日志里。
                    logger.error(
                        "清理未发布的交接临时目录失败：%s（%s）",
                        temp,
                        cleanup_exc,
                    )
            raise

    # ------------------------------------------------------------------
    def prepare_result(
        self, task_id: str, *, now: datetime | None = None
    ) -> ResultTarget:
        """确认前调用：stat result.json，并读取 task.json 检查 deadline。

        这里不读取 result.json 正文。deadline 在此入口检查一次；
        validate_result 入口还会再检查一次，不能只靠这里。
        """
        self.policy.ensure_enabled()
        handoff_dir = self.handoff_dir_for(task_id)
        if not handoff_dir.is_dir():
            raise CcTaskNotFound(
                f"找不到任务 {task_id} 的交接目录；请确认任务已准备完成"
            )

        task_path = handoff_dir / "task.json"
        if snapshot.path_is_link_like(task_path) or not task_path.is_file():
            raise CcTaskNotFound(f"任务 {task_id} 缺少 task.json，无法验证结果")

        result_path = handoff_dir / "result.json"
        if snapshot.path_is_link_like(result_path, error=CcResultInvalid):
            raise CcResultInvalid("result.json 是链接或目录联接，已拒绝")
        try:
            st = os.lstat(result_path)
        except FileNotFoundError as exc:
            raise CcResultNotFound(
                f"任务 {task_id} 还没有 result.json；请先把 CC 的结果放回该目录"
            ) from exc
        except OSError as exc:
            raise CcResultNotFound(f"无法查看 result.json（{exc}）") from exc
        if not statmod.S_ISREG(st.st_mode):
            raise CcResultInvalid("result.json 不是普通文件")
        if st.st_size > self.policy.max_result_bytes:
            raise CcResultTooLarge(
                f"result.json 太大了：{st.st_size} 字节，上限是 "
                f"{self.policy.max_result_bytes} 字节"
            )

        # 先完成 result.json 的存在性/类型检查，再读 task.json。这样链接
        # 分支仍然优先返回 cc_result_invalid，不会因为 task.json 内容不完整
        # 而改变既有的错误语义。
        task = snapshot.read_task_document(task_path, task_id=task_id)
        _ensure_task_not_expired(task, now=now)

        return ResultTarget(
            task_id=task_id,
            handoff_dir=handoff_dir,
            result_path=result_path,
            fingerprint=_fingerprint(result_path, st=st),
        )

    # ------------------------------------------------------------------
    def validate_result(
        self,
        *,
        task_id: str,
        result_fingerprint: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ResultValidation:
        """用户确认后调用：验证任务包完整性，再严格校验 result.json。

        deadline 在这里再次检查（prepare_result 已检查过一次），并且在重新
        读取 task.json 后再检查一次。只检查 prepare_result 不足以覆盖两次
        读取之间的替换。
        """
        self.policy.ensure_enabled()
        current = _now() if now is None else now
        target = self.prepare_result(task_id, now=current)
        expected: dict[str, Any] | None = None
        if result_fingerprint is not None:
            expected = validate_result_fingerprint(
                result_fingerprint, str(target.result_path)
            )
            try:
                st = os.lstat(target.result_path)
            except OSError as exc:
                raise CcResultChanged("result.json 在确认后消失了") from exc
            if not statmod.S_ISREG(st.st_mode):
                raise CcResultChanged("result.json 在确认后不再是普通文件")
            if not _fingerprint_matches(expected, st):
                raise CcResultChanged(
                    "result.json 在你确认之后发生了变化；为了不展示混合内容，"
                    "这次验证已停止。请重新检查文件后再发起验证。"
                )
            if st.st_size > self.policy.max_result_bytes:
                raise CcResultTooLarge(
                    f"result.json 超过上限 {self.policy.max_result_bytes} 字节"
                )

        task = snapshot.read_task_package(target.handoff_dir, task_id)
        _ensure_task_not_expired(task, now=current)
        self._check_task_limits(task)

        workspace = target.handoff_dir / "workspace"
        snapshot.verify_workspace(
            task,
            workspace,
            self.policy.max_total_bytes,
            max_file_bytes=self.policy.max_file_bytes,
        )

        try:
            raw = snapshot.read_json_capped(
                target.result_path,
                max_bytes=self.policy.max_result_bytes,
                label="result.json",
                expected_fingerprint=expected,
            )
        except snapshot.CcPackageInvalid as exc:
            if expected is not None:
                # 确认指纹已经存在时，读取阶段的链接/类型/打开失败都按
                # “确认后结果变化”处理，不能继续解析。
                raise CcResultChanged(
                    "result.json 在确认后与读取到的文件不一致，已停止"
                ) from exc
            raise
        result = protocol.validate_result_document(raw, task)
        return ResultValidation(
            task=task,
            result=result,
            workspace_verified=True,
        )

    def _check_task_limits(self, task: TaskDocument) -> None:
        """按当前配置再检查一次，防止交接包是在更宽松配置下生成的。"""
        if len(task.manifest) > self.policy.max_files:
            raise CcLimitExceeded("任务包文件数超过当前 cc.max_files 上限")
        for entry in task.manifest:
            if entry.size > self.policy.max_file_bytes:
                raise CcLimitExceeded(
                    f"任务包文件 {entry.path} 超过当前 cc.max_file_bytes 上限"
                )
        if sum(entry.size for entry in task.manifest) > self.policy.max_total_bytes:
            raise CcLimitExceeded("任务包总大小超过当前 cc.max_total_bytes 上限")
        if len(task.allowed_paths) > self.policy.max_paths:
            raise CcLimitExceeded("任务包 allowed_paths 超过当前 cc.max_paths 上限")
        if len(task.goal) > self.policy.max_goal_chars:
            raise CcLimitExceeded("任务包 goal 超过当前 cc.max_goal_chars 上限")
