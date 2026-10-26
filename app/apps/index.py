"""应用索引：把发现到的应用存下来，并支持增量刷新。

**存在现有的** data/jiaagent.db 里（新增两张表），不单独开一个 JSON 文件。
理由：data/ 已经在 blocked_paths 里，任何写文件的工具都碰不到它。
把索引放进一个 JSON 文件，就等于给模型开了一个"可以篡改索引"的口子。

建表语句写在下面（幂等，每次操作前确保存在），没有放进 schema.sql。
这样测试可以指向一个临时数据库、不必依赖主库和 init_db()。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from app.apps import sources
from app.apps.model import AppRecord, CacheEntry, LaunchKind, Source, normalize

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS app_index (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    source        TEXT NOT NULL,
    launch_kind   TEXT NOT NULL,
    target        TEXT NOT NULL,
    identity_key  TEXT NOT NULL DEFAULT '',
    aliases       TEXT NOT NULL DEFAULT '[]',
    args          TEXT NOT NULL DEFAULT '[]',
    workdir       TEXT NOT NULL DEFAULT '',
    resolved      TEXT NOT NULL DEFAULT '',
    process_hint  TEXT NOT NULL DEFAULT '',
    fs_mtime_ns   INTEGER NOT NULL DEFAULT 0,
    fs_size       INTEGER NOT NULL DEFAULT 0,
    extra         TEXT NOT NULL DEFAULT '{}',
    enabled       INTEGER NOT NULL DEFAULT 1,
    first_seen    TEXT NOT NULL,
    last_verified TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_index_source ON app_index(source);
CREATE INDEX IF NOT EXISTS idx_app_index_name ON app_index(name);

CREATE TABLE IF NOT EXISTS app_index_sources (
    source       TEXT PRIMARY KEY,
    last_refresh TEXT NOT NULL,
    entry_count  INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT NOT NULL DEFAULT ''
);

-- 文件系统来源的增量指纹：每个被扫过的文件/目录，记下它当时的 (mtime, size)。
-- 和记录分开存，是因为同一个 exe 可能被好几个文件发现、最后只留一条记录，
-- 每个文件自己的指纹不能跟着那条记录走（会串）。
CREATE TABLE IF NOT EXISTS app_index_files (
    identity_key TEXT PRIMARY KEY,
    mtime_ns     INTEGER NOT NULL DEFAULT 0,
    size         INTEGER NOT NULL DEFAULT 0
);
"""

# 说明：早先这里有个 _FS_SOURCES 集合，用来"只把文件系统来源的缓存传下去"。
# 那样做会让注册表来源每次都查不到缓存、次次重新枚举，增量等于白做，已去掉。
# 现在整份缓存统一传给所有来源，各来源自己决定要不要用。

# 同一个应用可能从多个来源被发现，保留优先级最高的那条。
# 手工配置永远第一——那是你亲自指定过的。
_PRIORITY: dict[Source, int] = {
    Source.MANUAL: 0,
    Source.START_MENU_USER: 1,
    Source.START_MENU_ALL: 2,
    Source.APP_PATHS: 3,
    Source.UNINSTALL: 4,
    Source.INSTALL_DIR: 5,
    Source.UWP: 6,
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _resolve_db_path(db_path: Path | str | None) -> Path:
    if db_path is not None:
        return Path(db_path)
    from app.memory.db import DB_PATH

    return Path(DB_PATH)


@contextmanager
def _db(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    target = _resolve_db_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_DDL)
        yield conn
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def _discovery_cfg():
    try:
        from app.settings import get_tools_config

        return get_tools_config().discovery
    except Exception:  # noqa: BLE001 - 配置读不到就用默认值
        return None


# ----------------------------------------------------------------------
# 读
# ----------------------------------------------------------------------


def _to_record(row: sqlite3.Row) -> AppRecord | None:
    """把数据库一行变回 AppRecord。认不出来的行返回 None（跳过，不炸）。"""
    try:
        return AppRecord(
            name=row["name"],
            source=Source(row["source"]),
            launch_kind=LaunchKind(row["launch_kind"]),
            target=row["target"],
            identity_key=row["identity_key"] or "",
            aliases=json.loads(row["aliases"] or "[]"),
            args=json.loads(row["args"] or "[]"),
            workdir=row["workdir"] or "",
            resolved=row["resolved"] or "",
            process_hint=row["process_hint"] or "",
            fs_mtime_ns=row["fs_mtime_ns"] or 0,
            fs_size=row["fs_size"] or 0,
            extra=json.loads(row["extra"] or "{}"),
            enabled=bool(row["enabled"]),
        )
    except (ValueError, json.JSONDecodeError, KeyError):
        logger.warning("索引里有一行读不出来，已跳过：%s", row["id"] if "id" in row.keys() else "?")
        return None


def load_all(db_path=None, *, include_disabled: bool = False) -> list[AppRecord]:
    sql = "SELECT * FROM app_index"
    if not include_disabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    with _db(db_path) as conn:
        rows = conn.execute(sql).fetchall()
    out = []
    for row in rows:
        record = _to_record(row)
        if record is not None:
            out.append(record)
    return out


def by_identity(db_path=None) -> dict[str, CacheEntry]:
    """按身份键索引，给增量扫描当缓存用。

    要把**所有被合并过的身份键都铺开**：去重时，同一个 exe 可能由好几个来源
    发现（一条 .lnk + 一条注册表项），最后只留下一条记录。另外那个身份键
    如果就此消失，下次刷新就会把它当成"新条目"重新解析一遍，增量就白做了。
    merged_keys 就是为这件事留的。

    指纹（mtime/size）单独从 app_index_files 表读，按身份键对应到"那个文件自己"
    的指纹，而不是跟着存活记录走（见 CacheEntry 的说明）。
    """
    records: dict[str, AppRecord] = {}
    for record in load_all(db_path, include_disabled=True):
        if record.identity_key:
            records.setdefault(record.identity_key, record)
        for key in record.extra.get("merged_keys") or []:
            records.setdefault(key, record)

    out: dict[str, CacheEntry] = {}
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT identity_key, mtime_ns, size FROM app_index_files"
        ).fetchall()
    for row in rows:
        identity = row["identity_key"]
        out[identity] = CacheEntry(
            record=records.get(identity),
            mtime_ns=row["mtime_ns"],
            size=row["size"],
        )

    # 注册表来源没有文件指纹，但需要能按身份键取到记录
    for identity, record in records.items():
        out.setdefault(identity, CacheEntry(record=record, mtime_ns=0, size=0))
    return out


def get(app_id: str, db_path=None) -> AppRecord | None:
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM app_index WHERE id = ?", (app_id,)).fetchone()
    return _to_record(row) if row is not None else None


def count(db_path=None) -> int:
    with _db(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM app_index WHERE enabled = 1").fetchone()
    return int(row["n"])


def last_refresh(db_path=None) -> str | None:
    with _db(db_path) as conn:
        row = conn.execute("SELECT MAX(last_refresh) AS t FROM app_index_sources").fetchone()
    return row["t"] if row and row["t"] else None


def source_stats(db_path=None) -> list[dict[str, Any]]:
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM app_index_sources ORDER BY source"
        ).fetchall()
    return [dict(r) for r in rows]


def is_stale(db_path=None) -> bool:
    """索引过期了吗？过期就表示该刷新了。"""
    cfg = _discovery_cfg()
    hours = int(getattr(cfg, "stale_after_hours", 24) or 0)
    if hours <= 0:
        return False          # 0 表示从不自动刷新
    last = last_refresh(db_path)
    if not last:
        return True
    try:
        moment = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now() - moment > timedelta(hours=hours)


# ----------------------------------------------------------------------
# 合并
# ----------------------------------------------------------------------


def _merge_key(record: AppRecord) -> str:
    """判断"是不是同一个应用"的依据：解析出的真实程序，没有就用启动目标。"""
    basis = record.resolved or record.target
    import os

    return os.path.normcase(basis.strip().strip('"'))


def _merge_aliases(primary: AppRecord, other: AppRecord) -> AppRecord:
    for alias in [other.name, *other.aliases]:
        if alias and alias != primary.name and alias not in primary.aliases:
            primary.aliases.append(alias)
    # 保留更有价值的信息：解析出的路径、进程名
    if not primary.resolved and other.resolved:
        primary.resolved = other.resolved
    if not primary.process_hint and other.process_hint:
        primary.process_hint = other.process_hint
    return primary


def _dedupe(records: list[AppRecord]) -> list[AppRecord]:
    """同一个应用可能从好几个来源被发现（例如一个快捷方式和一个注册表项
    指向同一个 exe）。按"启动目标"归并成一条，保留优先级最高的来源。

    被合并掉的那些身份键会记进 extra["merged_keys"]，
    下次刷新时它们才算"见过"，不会重复解析。
    """
    best: dict[str, AppRecord] = {}
    keys: dict[str, list[str]] = {}

    for record in records:
        key = _merge_key(record)
        identity = record.identity_key
        current = best.get(key)
        if current is None:
            best[key] = record
            keys[key] = [identity] if identity else []
            continue
        if identity:
            keys[key].append(identity)
        if _PRIORITY.get(record.source, 99) < _PRIORITY.get(current.source, 99):
            best[key] = _merge_aliases(record, current)
        else:
            best[key] = _merge_aliases(current, record)

    out: list[AppRecord] = []
    for key, record in best.items():
        # 这次看到的、归到这条记录名下的其他身份键
        fresh = {k for k in keys.get(key, []) if k and k != record.identity_key}
        # 之前几轮已经归到它名下的身份键——增量扫描时被合并掉的那条记录
        # 不会再次出现在 records 里，如果不把旧的 merged_keys 带过来，
        # 它就会在下一次刷新时被当成"没见过"而重新解析。
        prior = set(record.extra.get("merged_keys") or [])
        record.extra = dict(record.extra)
        record.extra["merged_keys"] = sorted(fresh | prior)
        out.append(record)
    return out


# ----------------------------------------------------------------------
# 写
# ----------------------------------------------------------------------


_UPSERT = """
INSERT INTO app_index (
    id, name, source, launch_kind, target, identity_key, aliases, args,
    workdir, resolved, process_hint, fs_mtime_ns, fs_size, extra,
    enabled, first_seen, last_verified
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(id) DO UPDATE SET
    name = excluded.name,
    source = excluded.source,
    launch_kind = excluded.launch_kind,
    target = excluded.target,
    identity_key = excluded.identity_key,
    aliases = excluded.aliases,
    args = excluded.args,
    workdir = excluded.workdir,
    resolved = excluded.resolved,
    process_hint = excluded.process_hint,
    fs_mtime_ns = excluded.fs_mtime_ns,
    fs_size = excluded.fs_size,
    extra = excluded.extra,
    enabled = excluded.enabled,
    last_verified = excluded.last_verified
"""


def _write(records: list[AppRecord], source_stats: dict[str, dict], db_path=None) -> int:
    now = _now()
    with _db(db_path) as conn:
        existing = {
            row["id"]: row["first_seen"]
            for row in conn.execute("SELECT id, first_seen FROM app_index")
        }
        keep: set[str] = set()
        for record in records:
            rid = record.id
            keep.add(rid)
            conn.execute(_UPSERT, (
                rid,
                record.name,
                record.source.value,
                record.launch_kind.value,
                record.target,
                record.identity_key,
                json.dumps(record.aliases, ensure_ascii=False),
                json.dumps(record.args, ensure_ascii=False),
                record.workdir,
                record.resolved,
                record.process_hint,
                record.fs_mtime_ns,
                record.fs_size,
                json.dumps(record.extra, ensure_ascii=False, default=str),
                1 if record.enabled else 0,
                existing.get(rid, now),   # 首次发现的时间要留住
                now,
            ))

        # 这次没发现到的，说明应用被卸载/快捷方式被删了，移出索引
        for rid in set(existing) - keep:
            conn.execute("DELETE FROM app_index WHERE id = ?", (rid,))

        for name, stat in source_stats.items():
            conn.execute(
                """
                INSERT INTO app_index_sources (source, last_refresh, entry_count, duration_ms, last_error)
                VALUES (?,?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET
                    last_refresh = excluded.last_refresh,
                    entry_count = excluded.entry_count,
                    duration_ms = excluded.duration_ms,
                    last_error = excluded.last_error
                """,
                (name, now, stat["count"], stat["ms"], stat.get("error", "")),
            )
    return len(records)


def _write_fingerprints(db_path, fingerprints: dict[str, tuple[int, int]]) -> None:
    """把这次扫到的文件指纹整份替换进 app_index_files。"""
    rows = [(k, v[0], v[1]) for k, v in fingerprints.items()]
    with _db(db_path) as conn:
        conn.execute("DELETE FROM app_index_files")
        conn.executemany(
            "INSERT INTO app_index_files (identity_key, mtime_ns, size) VALUES (?,?,?)",
            rows,
        )


# ----------------------------------------------------------------------
# 配置里的别名
# ----------------------------------------------------------------------


def _configured_aliases() -> dict[str, list[str]]:
    """读 config/tools.yaml 的 aliases: 段。读不到就当没有，不让它影响刷新。"""
    try:
        from app.settings import get_tools_config

        return get_tools_config().aliases
    except Exception:  # noqa: BLE001 - 配置有问题不该让整个索引刷不出来
        logger.debug("读不到别名配置", exc_info=True)
        return {}


def _apply_configured_aliases(
    records: list[AppRecord], table: dict[str, list[str]]
) -> None:
    """按应用名把配置里的别名贴到记录上。

    它只往 record.aliases 里追加，不改名字、不改启动目标——
    纯粹影响"能不能被搜到"，不影响"启动什么"。
    按名字匹配时用归一化后的形式，所以全角/大小写/带不带 .exe 都能对上。
    """
    if not table:
        return
    lookup: dict[str, list[str]] = {}
    for name, values in table.items():
        key = normalize(str(name))
        if key:
            lookup[key] = values

    for record in records:
        extra = lookup.get(record.name_norm)
        if not extra:
            continue
        for alias in extra:
            alias = str(alias).strip()
            if alias and alias != record.name and alias not in record.aliases:
                record.aliases.append(alias)


# ----------------------------------------------------------------------
# 刷新
# ----------------------------------------------------------------------


def refresh(db_path=None) -> dict[str, Any]:
    """重新扫描所有来源并更新索引。只读系统，只写我们自己的数据库。"""
    started = time.time()
    cache = by_identity(db_path)

    # 整份缓存都传下去。早先只传文件系统来源那一部分，
    # 结果注册表来源次次查不到缓存、次次重新枚举，增量等于白做。
    scans = sources.scan_all(cache)

    records: list[AppRecord] = []
    fingerprints: dict[str, tuple[int, int]] = {}
    per_source: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for scan in scans:
        records.extend(scan.result.records)
        fingerprints.update(scan.result.fingerprints)
        per_source[scan.source_name] = {
            "count": len(scan.result.records),
            "ms": int((time.time() - started) * 1000),
            "error": scan.error,
        }
        if scan.error:
            errors[scan.source_name] = scan.error

    merged = _dedupe(records)
    _apply_configured_aliases(merged, _configured_aliases())

    # 兜底上限，防止某个来源失控把索引撑爆
    cfg = _discovery_cfg()
    limit = int(getattr(cfg, "max_entries", 2000) or 2000)
    truncated = 0
    if len(merged) > limit:
        truncated = len(merged) - limit
        logger.warning("索引条目超过上限 %d，截掉 %d 条", limit, truncated)
        merged = merged[:limit]

    written = _write(merged, per_source, db_path)
    _write_fingerprints(db_path, fingerprints)
    reparsed = sum(s.result.reparsed for s in scans)
    elapsed = int((time.time() - started) * 1000)

    stats = {
        "total": written,
        "reparsed": reparsed,
        # 分开记：文件系统来源重解析了几条，是"增量生效了没有"的直接证据
        "reparsed_by_source": {s.source_name: s.result.reparsed for s in scans},
        "duration_ms": elapsed,
        "sources": {k: v["count"] for k, v in per_source.items()},
        "errors": errors,
        "truncated": truncated,
    }
    logger.info(
        "应用索引刷新完成：%d 条，重新解析 %d 条，耗时 %d 毫秒",
        written, reparsed, elapsed,
    )
    return stats


def clear(db_path=None) -> int:
    """清空索引。下次用的时候会重新建。"""
    with _db(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM app_index").fetchone()["n"]
        conn.execute("DELETE FROM app_index")
        conn.execute("DELETE FROM app_index_sources")
        conn.execute("DELETE FROM app_index_files")
    return int(n)


def ensure_ready(db_path=None) -> dict[str, Any]:
    """索引是空的或者过期了就刷新一次，否则什么都不做。

    这是给"第一次用到时顺手刷新"用的，不在服务启动时跑。
    """
    cfg = _discovery_cfg()
    if cfg is not None and not getattr(cfg, "enabled", True):
        return {"skipped": "discovery.enabled 为 false"}
    if count(db_path) == 0 or is_stale(db_path):
        return refresh(db_path)
    return {"skipped": "索引还新鲜"}
