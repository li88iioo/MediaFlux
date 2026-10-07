"""SQLite 连接、初始化协调与统一数据访问门面。

职责边界：database_schema 保存建表声明；database_migrations 保存逐版本迁移；
repositories 按业务域持有唯一 CRUD 实现。连接/WAL/测试路径和启动恢复仍由
本模块负责，跨表事务不在门面处分拆。

表：
- organize_log   光鸭整理记录（可回退）
- download_log   下载记录（qB / 光鸭）
- rss_items      RSS 订阅项
- rss_entries    RSS 条目（标题/状态/发布时间）
- tmdb_lock      TMDB 已确认映射缓存（片名→tmdb_id）
- strm_index     STRM 文件索引
- settings_kv    通用 KV（兜底配置存储）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.config import PATHS
from app.logger import get_logger
from app.modules.media_identity import normalize_media_number as _normalize_organize_position
from app.private_files import protect_sqlite_files
from app.runtime_paths import get_runtime_paths

logger = get_logger(__name__)

_PRODUCTION_DB_PATH = PATHS.database_path.resolve()
DB_PATH = PATHS.database_path
_lock = threading.RLock()
_wal_setup_lock = threading.Lock()
_wal_mode_cache: dict[str, tuple[int, int, int]] = {}
_configured_test_mode = False
SCHEMA_VERSION = 33

LOCAL_MEDIA_INTERRUPTED_WRITE_ERROR_PREFIX = "上次进程在本地媒体写操作期间中断"
_LOCAL_MEDIA_INTERRUPTED_PREWRITE_ERROR = (
    "上次进程在本地媒体识别或计划期间中断，已释放为可重试"
)


def is_interrupted_local_media_write_error(error: object) -> bool:
    """识别必须先人工核验、不能直接普通重试的启动恢复错误。"""
    return str(error or "").startswith(LOCAL_MEDIA_INTERRUPTED_WRITE_ERROR_PREFIX)


_SQLITE_CONTENTION_PHASES = frozenset(
    {"connect_setup", "operation", "commit", "init_schema"}
)
_sqlite_contention_lock = threading.Lock()
_sqlite_contention_counts = {
    "total": 0,
    "busy": 0,
    "locked": 0,
    "connect_setup": 0,
    "operation": 0,
    "commit": 0,
    "init_schema": 0,
}


def _sqlite_contention_kind(exc: BaseException) -> str | None:
    if not isinstance(exc, sqlite3.OperationalError):
        return None
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        primary = code & 0xFF
        if primary == sqlite3.SQLITE_BUSY:
            return "busy"
        if primary == sqlite3.SQLITE_LOCKED:
            return "locked"
    message = str(exc).strip().lower()
    if "locked" in message:
        return "locked"
    if "busy" in message:
        return "busy"
    return None


def _observe_sqlite_contention(
    exc: BaseException,
    *,
    phase: str,
    elapsed_ms: int = 0,
) -> None:
    """只记录固定低基数字段；不得写 SQL、路径或原始异常文本。"""
    kind = _sqlite_contention_kind(exc)
    if kind is None:
        return
    normalized_phase = phase if phase in _SQLITE_CONTENTION_PHASES else "operation"
    with _sqlite_contention_lock:
        _sqlite_contention_counts["total"] += 1
        _sqlite_contention_counts[kind] += 1
        _sqlite_contention_counts[normalized_phase] += 1
        total = _sqlite_contention_counts["total"]
    logger.warning(
        "SQLite contention kind=%s phase=%s elapsed_ms=%s total=%s",
        kind,
        normalized_phase,
        max(0, int(elapsed_ms)),
        total,
    )


def get_sqlite_contention_metrics() -> dict[str, int]:
    with _sqlite_contention_lock:
        return dict(_sqlite_contention_counts)


def _reset_sqlite_contention_metrics_for_tests() -> None:
    with _sqlite_contention_lock:
        for key in _sqlite_contention_counts:
            _sqlite_contention_counts[key] = 0


def production_db_path() -> Path:
    """返回固定的生产数据库绝对路径，不受测试配置影响。"""
    return _PRODUCTION_DB_PATH


def _normalized_path(path: Path) -> Path:
    return Path(path).expanduser().resolve()


def resolve_db_path() -> Path:
    """解析当前数据库路径，并在测试模式下拒绝连接生产库。"""
    with _lock:
        configured_path = _normalized_path(DB_PATH)
        env_test_mode = os.getenv("MEDIAFLUX_TEST_MODE", "").strip() == "1"
        test_mode = _configured_test_mode or env_test_mode
        env_path = os.getenv("MEDIAFLUX_TEST_DB_PATH", "").strip()

        # 显式 configure_database()/patch(DB_PATH, ...) 优先；环境变量只在
        # 尚未离开生产默认路径且明确启用测试模式时接管。
        if env_test_mode and env_path and configured_path == _PRODUCTION_DB_PATH:
            configured_path = _normalized_path(Path(env_path))

        if test_mode and configured_path == _PRODUCTION_DB_PATH:
            raise RuntimeError("测试模式禁止连接生产数据库")
        return configured_path


def configure_database(path: Path, *, test_mode: bool = False) -> Path:
    """显式配置后续连接使用的数据库路径。"""
    configured_path = _normalized_path(path)
    if test_mode and configured_path == _PRODUCTION_DB_PATH:
        raise RuntimeError("测试模式禁止连接生产数据库")

    global DB_PATH, _configured_test_mode
    with _lock:
        DB_PATH = configured_path
        _configured_test_mode = bool(test_mode)
    # 数据库路径可在测试、恢复或运维切换中复用；清空一次性 WAL 协商缓存，
    # 避免同路径的新文件继承旧连接状态。
    with _wal_setup_lock:
        _wal_mode_cache.clear()
    return configured_path


from app.database_schema import _SCHEMA  # noqa: E402


def _protect_database_files(path: Path | None = None) -> None:
    """尽力收紧数据库文件权限，不让权限修复掩盖数据库业务异常。"""
    try:
        target = path if path is not None else resolve_db_path()
        if not protect_sqlite_files(target):
            logger.warning("数据库私有文件权限收紧失败")
    except Exception as exc:  # 权限防护为 best-effort，不能破坏数据库可用性
        logger.warning("数据库私有文件权限收紧异常 type=%s", type(exc).__name__)


def _database_file_identity(path: Path) -> tuple[int, int, int] | None:
    """返回可识别同路径 inode 复用的轻量文件代际标识。

    仅使用 ``st_dev``/``st_ino`` 会漏掉快速 unlink + recreate 后文件系统
    立即复用 inode 的情况。加入纳秒级 ctime 后，数据库被替换时会重新
    协商 WAL；普通 WAL 写入主要落在 sidecar，不增加连接热路径查询。
    """
    try:
        metadata = path.stat()
    except OSError:
        return None
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_ctime_ns),
    )


def _ensure_wal_mode(conn: sqlite3.Connection, path: Path) -> None:
    """每个数据库文件只协商一次 WAL，文件被替换时自动重新协商。"""
    cache_key = str(path)
    with _wal_setup_lock:
        identity = _database_file_identity(path)
        if identity is not None and _wal_mode_cache.get(cache_key) == identity:
            return
        conn.execute("PRAGMA journal_mode=WAL;")
        refreshed = _database_file_identity(path)
        if refreshed is not None:
            _wal_mode_cache[cache_key] = refreshed


def _connect() -> sqlite3.Connection:
    started = time.monotonic()
    conn: sqlite3.Connection | None = None
    try:
        db_path = resolve_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _ensure_wal_mode(conn, db_path)
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn
    except sqlite3.Error as exc:
        if conn is not None:
            try:
                conn.close()
            except Exception as close_exc:
                logger.warning(
                    "SQLite 连接初始化失败后的关闭异常 type=%s",
                    type(close_exc).__name__,
                )
        _observe_sqlite_contention(
            exc,
            phase="connect_setup",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        raise


def _test_mode_enabled() -> bool:
    return _configured_test_mode or os.getenv("MEDIAFLUX_TEST_MODE", "").strip() == "1"


from app import database_migrations


def _run_schema_savepoint(
    conn: sqlite3.Connection,
    *,
    operation: Callable[[sqlite3.Connection], None],
    next_version: int | None = None,
) -> None:
    """在独立 SAVEPOINT 中原子执行一次 schema 操作。"""
    savepoint = "mediaflux_schema_step"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        operation(conn)
        if next_version is not None:
            conn.execute(f"PRAGMA user_version={int(next_version)}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        rolled_back = False
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            rolled_back = True
        except sqlite3.Error as rollback_exc:
            logger.error(
                "数据库 schema 操作回滚失败 type=%s",
                type(rollback_exc).__name__,
            )
        if rolled_back:
            try:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except sqlite3.Error as release_exc:
                logger.error(
                    "数据库 schema SAVEPOINT 释放失败 type=%s",
                    type(release_exc).__name__,
                )
        raise


def _execute_schema_script(conn: sqlite3.Connection, script: str) -> None:
    """逐语句执行 schema，使外层 SAVEPOINT 能真正覆盖全部 DDL。"""
    buffer = ""
    for line in str(script or "").splitlines(keepends=True):
        buffer += line
        if not sqlite3.complete_statement(buffer):
            continue
        statement = buffer.strip()
        buffer = ""
        if statement:
            conn.execute(statement)
    if buffer.strip():
        raise sqlite3.OperationalError("数据库 schema 包含不完整 SQL")


def _database_has_user_schema(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type IN ('table','index','view','trigger') "
        "AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


def _create_pre_migration_backup(
    conn: sqlite3.Connection,
    *,
    current_version: int,
) -> None:
    if _test_mode_enabled():
        return
    from app.modules.backup import BackupError, create_backup

    try:
        paths = get_runtime_paths()
        database_path = resolve_db_path()
        standard_runtime_database = database_path == paths.database_path.resolve()
        output = None
        if not standard_runtime_database:
            output = (
                database_path.parent
                / "backups"
                / (
                    f"mediaflux-pre-migration-{current_version}-to-"
                    f"{SCHEMA_VERSION}-{time.time_ns()}.zip"
                )
            )
        create_backup(
            paths,
            output=output,
            reason=f"pre-migration-{current_version}-to-{SCHEMA_VERSION}",
            source_connection=conn,
            include_settings=standard_runtime_database,
        )
    except BackupError as exc:
        raise RuntimeError(f"数据库迁移前备份失败，已取消启动：{exc}") from exc


def _prepare_schema_migration(
    conn: sqlite3.Connection,
    *,
    database_existed: bool,
) -> int:
    """校验正式 schema 世代，并为未来受支持升级保留备份门禁。"""
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if not database_existed:
        return current_version
    if current_version == 0:
        # 已存在但尚未打版本的早期数据库允许平滑进入，由 _SCHEMA
        # 幂等补齐表结构，再基线化为当前正式版本。已有用户 schema 时仍须
        # 在任何补列、凭据清理和数据修复前建立可恢复快照。
        if _database_has_user_schema(conn):
            _create_pre_migration_backup(conn, current_version=current_version)
        return current_version
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库版本 {current_version} 高于当前程序支持的 {SCHEMA_VERSION}，"
            "已拒绝降级启动"
        )
    if current_version == SCHEMA_VERSION:
        return current_version

    migrations = []
    version = current_version
    while version < SCHEMA_VERSION:
        migration = database_migrations._SCHEMA_MIGRATIONS.get(version)
        if migration is None:
            raise RuntimeError(
                f"数据库缺少从版本 {version} 升级到 {version + 1} 的正式迁移；已取消启动"
            )
        migrations.append(migration)
        version += 1

    _create_pre_migration_backup(conn, current_version=current_version)

    def migrate_schema_chain(connection: sqlite3.Connection) -> None:
        for next_version, migration in enumerate(
            migrations,
            start=current_version + 1,
        ):
            migration(connection)
            connection.execute(f"PRAGMA user_version={next_version}")

    _run_schema_savepoint(conn, operation=migrate_schema_chain)
    return current_version


def _sync_missing_schema_columns(conn: sqlite3.Connection) -> None:
    """在正式基线阶段平滑补齐已有表中缺少的列，避免历史开发库升级报 no such column。"""
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(_SCHEMA)
        for (tbl,) in mem.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall():
            if tbl.startswith("sqlite_"):
                continue
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
            ).fetchone()
            if not exists:
                continue

            target_cols = {
                str(r[1]) for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()
            }
            for row in mem.execute(f"PRAGMA table_info({tbl})").fetchall():
                _cid, col_name, col_type, notnull, dflt_value, _pk = row
                col_name = str(col_name)
                if col_name not in target_cols:
                    default_clause = ""
                    if dflt_value is not None:
                        default_clause = f" DEFAULT {dflt_value}"
                    elif notnull:
                        if "INT" in str(col_type or "").upper():
                            default_clause = " DEFAULT 0"
                        else:
                            default_clause = " DEFAULT ''"
                    col_type_str = str(col_type or "")
                    conn.execute(
                        f"ALTER TABLE {tbl} ADD COLUMN {col_name} {col_type_str}{default_clause}"
                    )
    finally:
        mem.close()


def finalize_provider_action_history_for_plans(
    conn: sqlite3.Connection,
    *,
    plan_refs: Iterable[str],
    status: str,
    error_code: str,
    timestamp: str,
) -> int:
    """一次扫描把 Provider 计划批次的终态投影到现有确认审计。

    这里只更新现有 ``executing`` 行，绝不补插。这样 Provider 计划终态与
    审计在同一事务内收敛，同时不会在隐私清理后重新创建主体记录。
    """
    normalized_plans = {str(ref or "").strip().upper() for ref in plan_refs} - {""}
    normalized_status = str(status or "").strip().casefold()
    if not normalized_plans or normalized_status not in {
        "succeeded",
        "failed",
        "stale",
        "outcome_unknown",
    }:
        return 0
    labels = {
        "succeeded": "Provider 原生写计划执行：已完成",
        "failed": "Provider 原生写计划执行：失败",
        "stale": "Provider 原生写计划执行：计划失效",
        "outcome_unknown": "Provider 原生写计划执行：结果待核对",
    }
    fallback_errors = {
        "succeeded": "",
        "failed": "provider_write_failed",
        "stale": "confirmation_stale",
        "outcome_unknown": "outcome_unknown",
    }
    normalized_error = re.sub(
        r"[^a-z0-9_]+", "_", str(error_code or "").strip().casefold()
    ).strip("_")[:64]
    if not normalized_error:
        normalized_error = fallback_errors[normalized_status]

    history_ids: list[int] = []
    for row in conn.execute(
        "SELECT id,safe_details FROM agent_action_history "
        "WHERE tool_name='provider.change.execute' AND status='executing'"
    ).fetchall():
        try:
            details = json.loads(str(row["safe_details"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(details, dict)
            and str(details.get("plan_ref") or "").strip().upper() in normalized_plans
        ):
            history_ids.append(int(row["id"]))
    if not history_ids:
        return 0
    conn.executemany(
        "UPDATE agent_action_history SET status=?,ok=?,summary=?,error_code=?,"
        "finished_at=?,elapsed_ms=0 WHERE id=? AND status='executing'",
        [
            (
                normalized_status,
                1 if normalized_status == "succeeded" else 0,
                labels[normalized_status],
                normalized_error,
                str(timestamp or now()),
                history_id,
            )
            for history_id in history_ids
        ],
    )
    return len(history_ids)


def recover_interrupted_provider_plans(
    conn: sqlite3.Connection, *, timestamp: str
) -> int:
    """在调用方已独占 Provider writer 时同步收束失去执行者的计划与审计。"""
    running_plan_ids = {
        str(row["plan_id"] or "")
        for row in conn.execute(
            "SELECT plan_id FROM agent_provider_plans WHERE status='running'"
        ).fetchall()
    }
    recovered = conn.execute(
        "UPDATE agent_provider_plans SET status='outcome_unknown',"
        "summary='进程中断，执行结果需要人工核对',"
        "error_code='execution_interrupted',finished_at=?,updated_at=? "
        "WHERE status='running'",
        (timestamp, timestamp),
    ).rowcount
    finalize_provider_action_history_for_plans(
        conn,
        plan_refs=running_plan_ids,
        status="outcome_unknown",
        error_code="execution_interrupted",
        timestamp=timestamp,
    )
    # 隐私清理已抹除上下文的运行计划只为等待旧 writer 收束；确认已无存活
    # 执行者后直接删除，避免重新保留主体数据。
    conn.execute("DELETE FROM agent_provider_plans WHERE context_fingerprint=''")
    return max(0, int(recovered or 0))


def _try_acquire_local_media_recovery_lock():
    """仅在没有存活本地媒体 writer 时返回启动恢复 lease。"""
    from app.modules.process_lock import CrossProcessLock

    recovery_lock = CrossProcessLock(
        "local-media-pipeline-write", directory=resolve_db_path().parent
    )
    if not recovery_lock.acquire(blocking=False):
        return None
    return recovery_lock


def init_db() -> None:
    """初始化首个正式数据库基线，并恢复上次异常中断的运行状态。"""
    from app.repositories import (
        agent_download_verification, agent_jobs, agent_library_patrol,
        agent_provider_plans, download_requests, local_media, media_proxy,
        organize_history, rss, strm,
    )

    with _lock:
        database_existed = resolve_db_path().exists()
        conn = _connect()
        local_media_recovery_lock = None
        try:
            _prepare_schema_migration(conn, database_existed=database_existed)

            # 未打版本的早期数据库也可能已有 v1 约束表；补列与重建必须作为
            # 一个原子步骤完成，避免退出后留下半迁移 schema。
            def prepare_schema_baseline(connection: sqlite3.Connection) -> None:
                _sync_missing_schema_columns(connection)
                database_migrations._migrate_agent_session_context_v2(connection)
                # 未打版本的早期数据库不会进入正式迁移链；仍需同步清除
                # 已被统一 Telegram 通知中心取代的旧整理通知队列。
                database_migrations._migrate_retire_organize_notification_outbox_v15(
                    connection
                )
                database_migrations._migrate_retire_telegram_write_confirmations_v16(
                    connection
                )
                database_migrations._migrate_strm_refresh_outbox_v19(connection)
                database_migrations._migrate_unify_rss_download_requests_v20(connection)
                database_migrations._migrate_durable_handoffs_v25(connection)

            _run_schema_savepoint(conn, operation=prepare_schema_baseline)
            _run_schema_savepoint(
                conn,
                operation=lambda connection: _execute_schema_script(
                    connection, _SCHEMA
                ),
            )
            conn.execute(
                "UPDATE agent_rate_limit_buckets SET expires_at="
                "MAX(window_start + 120, ?) WHERE expires_at<=0",
                (int(time.time()) + 60,),
            )
            # Docker-only 版本不再使用应用内 SMB 凭据；保留旧列兼容 schema，
            # 但主动清除历史敏感值，避免无业务用途的 NAS 密码继续进入备份。
            conn.execute(
                "UPDATE local_media_sources SET smb_user='',smb_pass='' "
                "WHERE COALESCE(smb_user,'')<>'' OR COALESCE(smb_pass,'')<>''"
            )
            # 固定等待与目录定时轮询已从产品链路移除；旧列仅作为 schema 墓碑，
            # 启动时统一归零，防止旧配置被误认为仍会生效。
            conn.execute(
                "UPDATE local_media_sources SET stable_seconds=0,scan_enabled=0,"
                "scan_interval_minutes=10 WHERE stable_seconds<>0 OR scan_enabled<>0 "
                "OR scan_interval_minutes<>10"
            )
            # 播放诊断保留期在启动时也执行，避免长期无新播放时旧媒体标识滞留。
            media_proxy._prune_playback_history(conn, retained_rows=10000)
            agent_jobs._purge_expired_runtime_state(conn)
            timestamp = now()
            organize_history._recover_after_restart(conn, timestamp)
            strm._recover_after_restart(conn, timestamp)
            # 启动恢复只能做一次 non-blocking writer 探测。另一个进程仍在
            # 整理时绝不能把它的活任务误判为 orphan；拿到锁后一直持有到本次
            # 初始化事务提交/回滚，避免恢复尚未提交时新 writer 抢先进入。
            local_media_recovery_lock = _try_acquire_local_media_recovery_lock()
            local_media._recover_after_restart(conn, timestamp, writer_available=local_media_recovery_lock is not None)
            download_requests._recover_after_restart(conn, timestamp)
            agent_download_verification._recover_after_restart(conn, timestamp)
            agent_library_patrol._recover_after_restart(conn, timestamp)
            agent_jobs._recover_after_restart(conn, timestamp)
            telegram_notifications._recover_after_restart(conn, timestamp)
            rss._recover_after_restart(conn, timestamp)
            agent_provider_plans._recover_after_restart(conn, timestamp)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        except BaseException as exc:
            if isinstance(exc, sqlite3.Error):
                _observe_sqlite_contention(exc, phase="init_schema")
            try:
                conn.rollback()
            except sqlite3.Error as rollback_exc:
                logger.warning(
                    "数据库初始化失败后的回滚异常 type=%s",
                    type(rollback_exc).__name__,
                )
            raise
        finally:
            try:
                conn.close()
            finally:
                if local_media_recovery_lock is not None:
                    local_media_recovery_lock.release()
                _protect_database_files()


@contextmanager
def get_conn():
    """获取连接的上下文管理器。自动提交/回滚。"""
    conn = _connect()
    started = time.monotonic()
    phase = "operation"
    try:
        yield conn
        phase = "commit"
        conn.commit()
    except Exception as exc:
        _observe_sqlite_contention(
            exc,
            phase=phase,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        try:
            conn.rollback()
        except sqlite3.Error as rollback_exc:
            logger.warning("数据库回滚失败 type=%s", type(rollback_exc).__name__)
        raise
    finally:
        try:
            conn.close()
        except sqlite3.Error as close_exc:
            logger.warning("数据库连接关闭失败 type=%s", type(close_exc).__name__)
        finally:
            _protect_database_files()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ===== Agent 网页搜索每日额度 =====
# 统一数据访问门面：调用方使用 app.database.*，事务实现按业务域唯一归属。
from app.repositories.agent_web_search import (  # noqa: E402,F401
    _validate_agent_web_search_usage_date,
    clear_agent_web_search_cache,
    current_agent_web_search_usage_date,
    get_agent_web_search_cache,
    get_agent_web_search_daily_usage,
    refund_agent_web_search_credits,
    reserve_agent_web_search_credits,
    set_agent_web_search_cache,
)

# ===== 媒体探测缓存 =====
# 统一数据访问门面：批量读取保持单连接契约。
from app.repositories.media_probe import (  # noqa: E402,F401
    get_media_probe_cache,
    get_media_probe_cache_many,
    prune_media_probe_cache,
    upsert_media_probe_cache,
    upsert_media_probe_failure_cache,
)

# ===== Emby / Jellyfin 多实例媒体反代 =====
# 统一数据访问门面：schema/连接仍由本模块持有。
from app.repositories.media_proxy import (  # noqa: E402,F401
    add_media_proxy_binding,
    add_media_proxy_instance,
    clear_media_proxy_playback_records,
    create_media_proxy_binding,
    delete_media_proxy_binding,
    delete_media_proxy_instance,
    get_media_proxy_binding,
    get_media_proxy_instance,
    get_media_proxy_playback_failure_summary,
    list_media_proxy_bindings,
    list_media_proxy_instances,
    list_media_proxy_playback_records,
    list_media_proxy_playback_sessions,
    record_media_proxy_playback_attempt,
    update_media_proxy_instance,
)

# ===== 整理后媒体规格补全队列 =====
from app.repositories.organize_probe import (  # noqa: E402,F401
    cancel_organize_probe_job,
    claim_due_organize_probe_jobs,
    commit_organize_probe_rename,
    complete_organize_probe_job,
    count_organize_probe_jobs,
    enqueue_organize_probe_completion,
    fail_or_retry_organize_probe_job,
    recover_stale_organize_probe_jobs,
    release_organize_probe_job,
)


# ===== 便捷 CRUD（按需扩展）=====
def kv_get(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings_kv WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings_kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now()),
        )


def add_organize_delete_audit(
    *,
    trigger: str,
    file_id: str,
    reason: str,
    status: str,
    organize_log_id: int | None = None,
    provider: str = "guangya",
    file_name: str = "",
    parent_id: str = "",
    size: int = 0,
    gcid: str = "",
    replacement_file_id: str = "",
    replacement_name: str = "",
    replacement_size: int = 0,
    replacement_gcid: str = "",
    provider_result: str = "",
    error: str = "",
) -> int:
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO organize_delete_audit(organize_log_id,trigger,provider,file_id,"
            "file_name,parent_id,size,gcid,replacement_file_id,replacement_name,"
            "replacement_size,replacement_gcid,reason,status,provider_result,error,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                organize_log_id,
                trigger,
                provider,
                file_id,
                file_name,
                parent_id,
                int(size or 0),
                gcid,
                replacement_file_id,
                replacement_name,
                int(replacement_size or 0),
                replacement_gcid,
                reason,
                status,
                provider_result,
                error,
                timestamp,
                timestamp,
            ),
        )
        return int(cur.lastrowid)


def update_organize_delete_audit(audit_id: int, **fields) -> bool:
    allowed = {"status", "provider_result", "error", "reason", "organize_log_id"}
    sets, values = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key}=?")
            values.append(value)
    if not sets:
        return False
    sets.append("updated_at=?")
    values.extend([now(), int(audit_id)])
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE organize_delete_audit SET {', '.join(sets)} WHERE id=?", values
        )
        return cur.rowcount == 1


def get_organize_delete_audit(audit_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_delete_audit WHERE id=?", (int(audit_id),)
        ).fetchone()


def list_organize_delete_audits(
    organize_log_id: int | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM organize_delete_audit"
    params: list = []
    if organize_log_id is not None:
        sql += " WHERE organize_log_id=?"
        params.append(int(organize_log_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 100), 1000)))
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def add_organize_log(
    source: str,
    original_path: str,
    new_path: str,
    file_id: str,
    status: str,
    tmdb_id: str = "",
    *,
    provider: str = "",
    external_id: str = "",
    operation_type: str = "organize",
    source_dir_id: str = "",
    original_parent_id: str = "",
    original_name: str = "",
    current_parent_id: str = "",
    current_name: str = "",
    target_parent_id: str = "",
    media_type: str = "",
    title: str = "",
    year: str = "",
    season: int | None = None,
    episode: int | None = None,
    error: str = "",
    release_parse: dict | None = None,
    parent_log_id: int | None = None,
    operation_token: str = "",
    legacy_incomplete: bool | None = None,
    _conn: sqlite3.Connection | None = None,
) -> int:
    timestamp = now()
    season = _normalize_organize_position(season)
    episode = _normalize_organize_position(episode)
    # 调用方可以主动标记为不完整，但不能覆盖关键快照缺失这一事实。
    incomplete = bool(legacy_incomplete) or not bool(
        original_name and original_parent_id
    )
    release_parse_json = (
        json.dumps(release_parse, ensure_ascii=False, separators=(",", ":"))
        if isinstance(release_parse, dict) and release_parse
        else ""
    )

    def insert(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            "INSERT INTO organize_log(source,original_path,new_path,file_id,status,tmdb_id,provider,external_id,"
            "operation_type,source_dir_id,original_parent_id,original_name,current_parent_id,"
            "current_name,target_parent_id,media_type,title,year,season,episode,error,release_parse_json,parent_log_id,"
            "operation_token,version,legacy_incomplete,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source,
                original_path,
                new_path,
                file_id,
                status,
                tmdb_id,
                provider,
                external_id,
                operation_type,
                source_dir_id,
                original_parent_id,
                original_name,
                current_parent_id,
                current_name,
                target_parent_id,
                media_type,
                title,
                year,
                season,
                episode,
                error,
                release_parse_json,
                parent_log_id,
                str(operation_token or "").strip(),
                1,
                1 if incomplete else 0,
                timestamp,
                timestamp,
            ),
        )
        return int(cur.lastrowid)

    if _conn is not None:
        return insert(_conn)
    with get_conn() as conn:
        return insert(conn)


def add_organize_log_items(
    log_id: int,
    items: list[dict],
    *,
    _conn: sqlite3.Connection | None = None,
) -> int:
    timestamp = now()
    rows = []
    for item in items:
        file_id = str(item.get("file_id") or "").strip()
        if not file_id:
            continue
        rows.append(
            (
                log_id,
                file_id,
                str(item.get("role") or "metadata"),
                str(item.get("original_parent_id") or ""),
                str(item.get("original_name") or ""),
                str(item.get("current_parent_id") or ""),
                str(item.get("current_name") or ""),
                str(item.get("target_parent_id") or ""),
                str(item.get("target_name") or ""),
                int(item.get("size") or 0),
                str(item.get("etag") or ""),
                str(item.get("status") or "success"),
                str(item.get("error") or ""),
                timestamp,
                timestamp,
            )
        )
    if not rows:
        return 0

    def insert(conn: sqlite3.Connection) -> int:
        conn.executemany(
            "INSERT INTO organize_log_items(log_id,file_id,role,original_parent_id,original_name,"
            "current_parent_id,current_name,target_parent_id,target_name,size,etag,status,error,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(log_id,file_id) DO UPDATE SET role=excluded.role,"
            "original_parent_id=excluded.original_parent_id,original_name=excluded.original_name,"
            "current_parent_id=excluded.current_parent_id,current_name=excluded.current_name,"
            "target_parent_id=excluded.target_parent_id,target_name=excluded.target_name,"
            "size=excluded.size,etag=excluded.etag,status=excluded.status,error=excluded.error,"
            "updated_at=excluded.updated_at",
            rows,
        )
        return len(rows)

    if _conn is not None:
        return insert(_conn)
    with get_conn() as conn:
        return insert(conn)


def resolve_pending_organize_logs(
    source: str,
    file_id: str,
    *,
    before_log_id: int,
    _conn: sqlite3.Connection | None = None,
) -> int:
    """把同一文件已完成的人工确认前置审计标记为已结算。

    新版待确认记录使用 ``manual``；早期版本将其误记为 ``skipped``，
    因此仅兼容包含人工确认语义的旧跳过记录。记录保留用于审计，但统一
    时间线会隐藏 ``confirmed`` 前置记录，只展示最终成功入库结果。
    """
    normalized_source = str(source or "").strip()
    normalized_file_id = str(file_id or "").strip()
    upper_bound = max(0, int(before_log_id or 0))
    if not normalized_source or not normalized_file_id or upper_bound <= 0:
        return 0

    def resolve(conn: sqlite3.Connection) -> int:
        rows = conn.execute(
            "SELECT id FROM organize_log WHERE source=? AND file_id=? AND id<? AND ("
            "status='manual' OR (status='skipped' AND ("
            "instr(COALESCE(error,''),'人工确认')>0 OR "
            "instr(COALESCE(error,''),'待确认')>0)))",
            (normalized_source, normalized_file_id, upper_bound),
        ).fetchall()
        log_ids = [int(row["id"]) for row in rows]
        if not log_ids:
            return 0
        placeholders = ",".join("?" for _ in log_ids)
        stamp = now()
        conn.execute(
            f"UPDATE organize_log SET status='confirmed',version=version+1,updated_at=? "
            f"WHERE id IN ({placeholders})",
            (stamp, *log_ids),
        )
        conn.execute(
            f"UPDATE organize_log_items SET status='confirmed',error='',updated_at=? "
            f"WHERE log_id IN ({placeholders}) AND status IN ('manual','skipped')",
            (stamp, *log_ids),
        )
        return len(log_ids)

    if _conn is not None:
        return resolve(_conn)
    with get_conn() as conn:
        return resolve(conn)


def finalize_pending_organize_logs(
    source: str,
    file_ids: Iterable[str],
    *,
    status: str,
    error: str = "",
    confirmation_actor: str = "",
) -> int:
    """结束仍待人工处理的整理日志；用于取消或不可重试失败。"""
    normalized_status = str(status or "").strip()
    if normalized_status not in {"skipped", "failed"}:
        raise ValueError("不支持的人工确认终态")
    normalized_source = str(source or "").strip()
    normalized_ids = list(
        dict.fromkeys(
            str(item or "").strip() for item in file_ids if str(item or "").strip()
        )
    )
    if not normalized_source or not normalized_ids:
        return 0
    placeholders = ",".join("?" for _ in normalized_ids)
    stamp = now()
    message = str(error or "").strip()
    actor = str(confirmation_actor or "").strip().lower()
    if actor not in {"", "human", "agent"}:
        raise ValueError("确认执行者无效")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id FROM organize_log WHERE source=? AND file_id IN ({placeholders}) "
            "AND (status='manual' OR (status='skipped' AND ("
            "instr(COALESCE(error,''),'人工确认')>0 OR "
            "instr(COALESCE(error,''),'待确认')>0)))",
            (normalized_source, *normalized_ids),
        ).fetchall()
        log_ids = [int(row["id"]) for row in rows]
        if not log_ids:
            return 0
        log_placeholders = ",".join("?" for _ in log_ids)
        conn.execute(
            f"UPDATE organize_log SET status=?,error=?,confirmation_actor=?,"
            f"version=version+1,updated_at=? "
            f"WHERE id IN ({log_placeholders})",
            (normalized_status, message, actor, stamp, *log_ids),
        )
        conn.execute(
            f"UPDATE organize_log_items SET status=?,error=?,updated_at=? "
            f"WHERE log_id IN ({log_placeholders}) AND status IN ('manual','skipped')",
            (normalized_status, message, stamp, *log_ids),
        )
        return len(log_ids)


def mark_organize_logs_confirmation_actor(
    operation_token: str, actor: str
) -> int:
    """给一次确认执行新产生的光鸭日志标记来源，不改变业务状态。"""
    token = str(operation_token or "").strip()
    normalized_actor = str(actor or "").strip().lower()
    if not token or normalized_actor not in {"human", "agent"}:
        return 0
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_log SET confirmation_actor=?,updated_at=? "
            "WHERE operation_token=?",
            (normalized_actor, now(), token),
        )
        return int(cursor.rowcount)


def _organize_log_filters(
    status: str | None = None, keyword: str = ""
) -> tuple[str, list]:
    sql = " WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if keyword:
        sql += " AND (original_path LIKE ? OR new_path LIKE ? OR original_name LIKE ? OR current_name LIKE ?)"
        value = f"%{keyword}%"
        params += [value, value, value, value]
    return sql, params


def list_organize_logs(
    status: str | None = None, keyword: str = "", limit: int = 20, offset: int = 0
) -> list[sqlite3.Row]:
    filters, params = _organize_log_filters(status, keyword)
    sql = "SELECT * FROM organize_log" + filters + " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, int(limit)), max(0, int(offset))])
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def latest_organize_log_id() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id),0) AS id FROM organize_log"
        ).fetchone()
        return int(row["id"] or 0)


def list_organize_logs_after(after_id: int) -> list[sqlite3.Row]:
    """按高水位读取全部新增审计，供单次批处理精确收束。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_log WHERE id>? ORDER BY id ASC",
            (max(0, int(after_id or 0)),),
        ).fetchall()


def list_organize_logs_by_operation_token(operation_token: str) -> list[sqlite3.Row]:
    """精确读取单次整理调用写入的全部审计记录。"""
    token = str(operation_token or "").strip()
    if not token:
        return []
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_log WHERE operation_token=? ORDER BY id ASC",
            (token,),
        ).fetchall()


def list_organize_root_identities(media_root_path: str) -> list[sqlite3.Row]:
    """精确读取目标根目录历史媒体身份，不依赖通用日志分页窗口。"""
    prefix = str(media_root_path or "").strip("/")
    if not prefix:
        return []
    branch = prefix + "/"
    with get_conn() as conn:
        return conn.execute(
            "SELECT DISTINCT media_type,provider,external_id,tmdb_id FROM ("
            "SELECT media_type,provider,external_id,tmdb_id FROM organize_log "
            "WHERE status='success' AND new_path=? UNION ALL "
            "SELECT media_type,provider,external_id,tmdb_id FROM organize_log "
            "WHERE status='success' AND new_path>=? AND new_path<?)",
            (prefix, branch, branch + "\U0010ffff"),
        ).fetchall()


def count_organize_logs(status: str | None = None, keyword: str = "") -> int:
    filters, params = _organize_log_filters(status, keyword)
    with get_conn() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM organize_log" + filters, params
            ).fetchone()[0]
        )


_TIMELINE_STATUS_VALUES = {
    "success",
    "failed",
    "issues",
    "skipped",
    "reverted",
    "processing",
    "manual",
}
_TIMELINE_ORIGIN_VALUES = {"all", "guangya", "local"}


def _organize_timeline_query(
    *, owner: str = "admin", origin: str = "all", status: str = "", keyword: str = ""
) -> tuple[str, list[object]]:
    normalized_origin = str(origin or "all").strip().lower()
    normalized_status = str(status or "").strip().lower()
    if normalized_origin not in _TIMELINE_ORIGIN_VALUES:
        raise ValueError("不支持的整理来源")
    if normalized_status and normalized_status not in _TIMELINE_STATUS_VALUES:
        raise ValueError("不支持的整理状态")

    sql = """
        WITH timeline AS (
            SELECT
                'guangya' AS origin, l.id AS id, '光鸭云盘' AS source_label,
                l.status AS raw_status,
                CASE
                    WHEN l.status='success' THEN 'success'
                    WHEN l.status IN ('failed','interrupted','partial_failed','revert_failed','deleted') THEN 'failed'
                    WHEN l.status='manual' OR (
                        l.status='skipped' AND (
                            instr(COALESCE(l.error,''),'人工确认')>0 OR
                            instr(COALESCE(l.error,''),'待确认')>0
                        )
                    ) THEN 'manual'
                    WHEN l.status='skipped' THEN 'skipped'
                    WHEN l.status='reverted' THEN 'reverted'
                    ELSE 'processing'
                END AS status,
                l.original_path AS original_path, l.original_name AS original_name,
                l.new_path AS new_path, l.current_name AS current_name,
                l.tmdb_id AS tmdb_id, l.provider AS provider,
                l.external_id AS external_id,
                l.media_type AS media_type, l.title AS title,
                l.year AS year, l.season AS season, l.episode AS episode,
                '' AS trigger, l.error AS error, '' AS warning,
                l.created_at AS created_at, COALESCE(l.updated_at,l.created_at) AS updated_at,
                '' AS completed_at, l.version AS version,
                l.legacy_incomplete AS legacy_incomplete,
                l.confirmation_actor AS confirmation_actor
            FROM organize_log l
            WHERE l.status<>'confirmed' AND NOT (
                (
                    l.status='manual' OR (
                        l.status='skipped' AND (
                            instr(COALESCE(l.error,''),'人工确认')>0 OR
                            instr(COALESCE(l.error,''),'待确认')>0
                        )
                    )
                ) AND EXISTS (
                    SELECT 1 FROM organize_log completed
                    WHERE completed.source=l.source AND completed.file_id=l.file_id
                      AND completed.id>l.id AND completed.status='success'
                )
            )
            UNION ALL
            SELECT
                'local' AS origin, t.id AS id, COALESCE(s.name,'已删除来源') AS source_label,
                t.status AS raw_status,
                CASE
                    WHEN t.status='completed' THEN 'success'
                    WHEN t.status='failed' THEN 'failed'
                    WHEN t.status='requires_manual' THEN 'manual'
                    ELSE 'processing'
                END AS status,
                t.content_path AS original_path, '' AS original_name,
                COALESCE((SELECT i.target_path FROM local_media_task_items i
                          WHERE i.task_id=t.id AND i.owner=t.owner AND i.target_path<>''
                          ORDER BY CASE WHEN i.role='video' THEN 0 ELSE 1 END, i.id LIMIT 1),'') AS new_path,
                '' AS current_name,
                t.tmdb_id AS tmdb_id,
                CASE WHEN t.tmdb_id<>'' THEN 'tmdb' ELSE '' END AS provider,
                t.tmdb_id AS external_id,
                t.media_type AS media_type, t.title AS title,
                t.year AS year, NULL AS season, NULL AS episode,
                t.trigger AS trigger, t.error AS error, t.warning AS warning,
                t.created_at AS created_at, t.updated_at AS updated_at,
                COALESCE(t.completed_at,'') AS completed_at, t.version AS version,
                0 AS legacy_incomplete, t.confirmation_actor AS confirmation_actor
            FROM local_media_tasks t
            LEFT JOIN local_media_sources s ON s.id=t.source_id AND s.owner=t.owner
            WHERE t.owner=?
        )
        SELECT * FROM timeline WHERE 1=1
    """
    params: list[object] = [_local_media_owner(owner)]
    if normalized_origin != "all":
        sql += " AND origin=?"
        params.append(normalized_origin)
    if normalized_status == "issues":
        # 对齐看板历史异常口径；旧failed筛选仍保留“已删除”的兼容语义。
        sql += " AND raw_status IN ('failed','interrupted','partial_failed','revert_failed')"
    elif normalized_status:
        sql += " AND status=?"
        params.append(normalized_status)
    clean_keyword = str(keyword or "").strip()
    if clean_keyword:
        value = f"%{clean_keyword}%"
        sql += " AND (original_path LIKE ? OR original_name LIKE ? OR new_path LIKE ? OR current_name LIKE ? OR title LIKE ? OR error LIKE ? OR warning LIKE ? OR source_label LIKE ?)"
        params.extend([value] * 8)
    return sql, params


def list_organize_timeline(
    *,
    owner: str = "admin",
    origin: str = "all",
    status: str = "",
    keyword: str = "",
    limit: int = 20,
    offset: int = 0,
) -> list[sqlite3.Row]:
    sql, params = _organize_timeline_query(
        owner=owner, origin=origin, status=status, keyword=keyword
    )
    sql += " ORDER BY updated_at DESC, origin DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, int(limit)), max(0, int(offset))])
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def count_organize_timeline(
    *, owner: str = "admin", origin: str = "all", status: str = "", keyword: str = ""
) -> int:
    sql, params = _organize_timeline_query(
        owner=owner, origin=origin, status=status, keyword=keyword
    )
    with get_conn() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0])


def count_organize_timeline_by_status(*, owner: str = "admin") -> dict[str, int]:
    sql, params = _organize_timeline_query(owner=owner)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT status, COUNT(*) AS count FROM ({sql}) GROUP BY status", params
        ).fetchall()
    counts = {
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "manual": 0,
        "processing": 0,
        "reverted": 0,
    }
    counts.update({str(row["status"]): int(row["count"] or 0) for row in rows})
    return counts


def get_agent_organize_audit(
    *, owner: str = "admin", origin: str = "all", status: str = "all", limit: int = 10
) -> dict[str, object]:
    """为 Agent 返回整理时间线的固定脱敏视图，不暴露路径、标识或错误正文。"""
    normalized_status = str(status or "all").strip().lower()
    if normalized_status not in _TIMELINE_STATUS_VALUES | {"all"}:
        raise ValueError("不支持的整理状态")
    safe_limit = max(1, min(int(limit), 50))
    base_sql, params = _organize_timeline_query(
        owner=owner,
        origin=origin,
        status="" if normalized_status == "all" else normalized_status,
    )
    with get_conn() as conn:
        total = int(
            conn.execute(f"SELECT COUNT(*) FROM ({base_sql})", params).fetchone()[0]
        )
        origin_rows = conn.execute(
            f"SELECT origin, COUNT(*) AS count FROM ({base_sql}) GROUP BY origin",
            params,
        ).fetchall()
        status_rows = conn.execute(
            f"SELECT status, COUNT(*) AS count FROM ({base_sql}) GROUP BY status",
            params,
        ).fetchall()
        rows = conn.execute(
            "SELECT origin,status,title,media_type,year,season,episode,updated_at "
            f"FROM ({base_sql}) ORDER BY updated_at DESC, origin DESC, id DESC LIMIT ?",
            [*params, safe_limit + 1],
        ).fetchall()

    return {
        "total": total,
        "by_origin": {
            str(row["origin"]): int(row["count"] or 0) for row in origin_rows
        },
        "by_status": {
            str(row["status"]): int(row["count"] or 0) for row in status_rows
        },
        "records": [dict(row) for row in rows[:safe_limit]],
        "truncated": len(rows) > safe_limit,
    }


def get_organize_log(log_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_log WHERE id=?", (log_id,)
        ).fetchone()


def list_organize_log_items(log_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_log_items WHERE log_id=? ORDER BY CASE role "
            "WHEN 'video' THEN 0 ELSE 1 END,id",
            (log_id,),
        ).fetchall()




def claim_organize_log_operation(
    log_id: int,
    operation_token: str,
    next_status: str,
    allowed_statuses: tuple[str, ...],
    expected_version: int | None = None,
) -> bool:
    statuses = tuple(dict.fromkeys(str(item) for item in allowed_statuses if str(item)))
    if not statuses or not operation_token:
        return False
    placeholders = ",".join("?" for _ in statuses)
    sql = (
        f"UPDATE organize_log SET status=?,operation_token=?,version=version+1,updated_at=? "
        f"WHERE id=? AND status IN ({placeholders}) AND COALESCE(legacy_incomplete,0)=0"
    )
    params: list = [next_status, operation_token, now(), log_id, *statuses]
    if expected_version is not None:
        sql += " AND version=?"
        params.append(int(expected_version))
    with get_conn() as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount == 1


class _BatchClaimRejected(RuntimeError):
    pass


def claim_organize_log_operations_batch(
    entries: list[dict],
    next_status: str,
    allowed_statuses: tuple[str, ...],
) -> bool:
    """在单一事务内认领整批日志；任一版本/状态漂移则全部回滚。"""
    statuses = tuple(dict.fromkeys(str(item) for item in allowed_statuses if str(item)))
    if not entries or not statuses:
        return False
    placeholders = ",".join("?" for _ in statuses)
    sql = (
        f"UPDATE organize_log SET status=?,operation_token=?,version=version+1,updated_at=? "
        f"WHERE id=? AND status IN ({placeholders}) AND COALESCE(legacy_incomplete,0)=0 "
        "AND version=?"
    )
    try:
        with get_conn() as conn:
            for entry in entries:
                token = str(entry.get("operation_token") or "")
                if not token:
                    raise _BatchClaimRejected("missing operation token")
                cur = conn.execute(
                    sql,
                    (
                        next_status,
                        token,
                        now(),
                        int(entry["log_id"]),
                        *statuses,
                        int(entry["expected_version"]),
                    ),
                )
                if cur.rowcount != 1:
                    raise _BatchClaimRejected("batch claim rejected")
    except _BatchClaimRejected:
        return False
    return True


# 操作步骤与可逆业务快照由同一仓储持有；保留原有数据库门面。
from app.repositories.organize_history import (  # noqa: E402,F401
    add_organize_operation_step,
    capture_organize_business_snapshot,
    finish_organize_operation_step,
    list_organize_operation_steps,
    list_latest_reversible_organize_steps,
    list_pending_organize_probe_steps,
    restore_organize_business_snapshot,
)


def update_organize_log(log_id: int, **fields) -> bool:
    allowed = {
        "status",
        "new_path",
        "current_parent_id",
        "current_name",
        "target_parent_id",
        "tmdb_id",
        "provider",
        "external_id",
        "media_type",
        "title",
        "year",
        "season",
        "episode",
        "release_parse_json",
        "error",
        "operation_type",
        "operation_token",
        "legacy_incomplete",
    }
    sets, values = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key}=?")
            values.append(value)
    if not sets:
        return False
    sets.extend(["version=version+1", "updated_at=?"])
    values.extend([now(), log_id])
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE organize_log SET {', '.join(sets)} WHERE id=?", values
        )
        return cur.rowcount == 1


def update_organize_log_item(item_id: int, **fields) -> bool:
    allowed = {
        "current_parent_id",
        "current_name",
        "target_parent_id",
        "target_name",
        "status",
        "error",
        "size",
        "etag",
    }
    sets, values = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key}=?")
            values.append(value)
    if not sets:
        return False
    sets.append("updated_at=?")
    values.extend([now(), item_id])
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE organize_log_items SET {', '.join(sets)} WHERE id=?", values
        )
        return cur.rowcount == 1






def recover_interrupted_organize_operations() -> dict[str, int]:
    """把进程退出遗留的忙碌状态转成需重新核验的中断状态。"""
    timestamp = now()
    with get_conn() as conn:
        logs = conn.execute(
            "UPDATE organize_log SET status='interrupted',error=CASE "
            "WHEN COALESCE(error,'')='' THEN '上次进程在云端写操作期间中断，必须重新核验快照' "
            "ELSE error END,updated_at=? "
            "WHERE status IN ('reorganizing','returning','reverting','deleting')",
            (timestamp,),
        ).rowcount
        steps = conn.execute(
            "UPDATE organize_operation_steps SET status='interrupted',"
            "error=CASE WHEN COALESCE(error,'')='' THEN '进程中断，步骤结果需要人工核验' ELSE error END,"
            "finished_at=COALESCE(finished_at,?) WHERE status='running'",
            (timestamp,),
        ).rowcount
        audits = conn.execute(
            "UPDATE organize_delete_audit SET status='interrupted',"
            "error='上次进程在光鸭 provider 调用期间中断，结果未知，需人工核验',"
            "provider_result='光鸭回收站结果未知，需人工核验',updated_at=? "
            "WHERE status='pending'",
            (timestamp,),
        ).rowcount
    return {"logs": logs, "steps": steps, "delete_audits": audits}


def clear_organize_logs() -> dict[str, int]:
    """清理可安全删除的光鸭与本地整理记录，不触碰任何媒体文件。"""
    guangya_busy_statuses = ("reorganizing", "returning", "reverting", "deleting")
    local_busy_statuses = (
        "waiting_stable",
        "recognizing",
        "planned",
        "moving",
        "verifying",
        "refreshing",
        "rolling_back",
    )
    guangya_placeholders = ",".join("?" for _ in guangya_busy_statuses)
    local_placeholders = ",".join("?" for _ in local_busy_statuses)
    with get_conn() as conn:
        guangya_busy = int(
            conn.execute(
                f"SELECT COUNT(*) FROM organize_log WHERE status IN ({guangya_placeholders})",
                guangya_busy_statuses,
            ).fetchone()[0]
        )
        local_busy = int(
            conn.execute(
                f"SELECT COUNT(*) FROM local_media_tasks WHERE status IN ({local_placeholders})",
                local_busy_statuses,
            ).fetchone()[0]
        )

        rows = conn.execute(
            f"SELECT id FROM organize_log WHERE status NOT IN ({guangya_placeholders})",
            guangya_busy_statuses,
        ).fetchall()
        log_ids = [int(row["id"]) for row in rows]
        deleted_guangya = 0
        if log_ids:
            id_placeholders = ",".join("?" for _ in log_ids)
            conn.execute(
                f"UPDATE organize_log SET parent_log_id=NULL WHERE parent_log_id IN ({id_placeholders})",
                log_ids,
            )
            conn.execute(
                f"DELETE FROM organize_operation_steps WHERE log_id IN ({id_placeholders})",
                log_ids,
            )
            conn.execute(
                f"DELETE FROM organize_delete_audit WHERE organize_log_id IN ({id_placeholders})",
                log_ids,
            )
            conn.execute(
                f"DELETE FROM organize_log_items WHERE log_id IN ({id_placeholders})",
                log_ids,
            )
            deleted_guangya = int(
                conn.execute(
                    f"DELETE FROM organize_log WHERE id IN ({id_placeholders})",
                    log_ids,
                ).rowcount
                or 0
            )

        deleted_local = int(
            conn.execute(
                f"DELETE FROM local_media_tasks WHERE status NOT IN ({local_placeholders})",
                local_busy_statuses,
            ).rowcount
            or 0
        )

    return {
        "deleted": deleted_guangya + deleted_local,
        "skipped_busy": guangya_busy + local_busy,
        "deleted_guangya": deleted_guangya,
        "deleted_local": deleted_local,
        "skipped_busy_guangya": guangya_busy,
        "skipped_busy_local": local_busy,
    }


def count_logs_by_status(table: str = "organize_log") -> dict:
    """按状态计数。返回 {status: count}，未出现的状态补 0。"""
    allowed = {"organize_log", "download_log"}
    if table not in allowed:
        raise ValueError(f"unsupported table: {table}")
    defaults = (
        {
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "reverted": 0,
            "interrupted": 0,
            "partial_failed": 0,
            "revert_failed": 0,
            "reorganizing": 0,
            "returning": 0,
            "reverting": 0,
            "deleting": 0,
            "deleted": 0,
        }
        if table == "organize_log"
        else {"success": 0, "failed": 0, "submitted": 0}
    )
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status"
        ).fetchall()
    for r in rows:
        defaults[r["status"] or "submitted"] = r["n"]
    return defaults


# ===== TMDB 映射锁 =====
# 统一数据访问门面：管理 API 与识别业务共享同一 Repository 实现。
# ===== Agent 下载结果自动复核 =====
# 统一数据访问门面：终态与通知发件箱保持单事务写入。
from app.repositories.agent_download_verification import (  # noqa: E402,F401
    claim_due_agent_download_verification,
    claim_due_agent_download_verification_notification,
    complete_agent_download_verification_notification,
    discard_agent_download_verification_notification,
    discard_agent_download_verification_notifications,
    enqueue_agent_download_verification,
    finish_agent_download_verification,
    get_agent_download_verification,
    list_agent_download_verification_notifications,
    release_agent_download_verification_notification,
    renew_agent_download_verification_lease,
    retry_agent_download_verification_notification,
    update_agent_download_verification,
)

# ===== 下载日志与统一下载请求 =====
# 统一数据访问门面：Repository 复用本模块持有的连接状态与迁移。
from app.repositories.download_requests import (  # noqa: E402,F401
    _DOWNLOAD_ATTENTION_WHERE,
    add_download_log,
    bind_download_request_guangya_staging,
    bind_media_download_admission_request,
    bind_pending_download_request_owner,
    claim_failed_share_transfer_request,
    clear_download_request_attentions,
    count_download_logs,
    count_download_requests_requiring_attention,
    create_download_request,
    create_share_transfer_request,
    delete_download_logs,
    finish_share_transfer_request,
    get_download_request,
    get_download_request_status_snapshot,
    list_download_logs,
    list_download_requests_requiring_attention,
    mark_download_request_resubmitted,
    purge_expired_download_request_torrent_data,
    update_download_log,
)
from app.repositories.recognition import (  # noqa: E402,F401
    delete_tmdb_lock,
    get_tmdb_lock,
    list_tmdb_locks,
    upsert_tmdb_lock,
)


def purge_expired_agent_task_history(
    *,
    current_time: str,
    next_cleanup_at: str,
    terminal_before: str,
    limit_per_table: int = 500,
) -> dict[str, object]:
    """跨进程限频清理 Agent 终态历史，绝不删除待执行或持有租约的任务。"""
    current = str(current_time or "").strip()
    next_run = str(next_cleanup_at or "").strip()
    cutoff = str(terminal_before or "").strip()
    if not current or not next_run or not cutoff:
        raise ValueError("Agent 历史清理时间参数不能为空")
    limit = max(1, min(int(limit_per_table), 5000))
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO agent_maintenance(task_key,next_run_at,updated_at) "
            "VALUES('history_cleanup',?,?)",
            (current, current),
        )
        lease = conn.execute(
            "SELECT next_run_at FROM agent_maintenance "
            "WHERE task_key='history_cleanup'",
        ).fetchone()
        due_at = str(lease["next_run_at"] or "") if lease else current
        if due_at > current:
            return {
                "performed": False,
                "next_cleanup_at": due_at,
                "download_verifications": 0,
                "download_verification_notification_outbox": 0,
                "patrol_notification_outbox": 0,
                "action_history": 0,
                "jobs": 0,
                "provider_plans": 0,
                "organize_operation_jobs": 0,
                "missing_media_workflows": 0,
                "web_search_usage": 0,
            }
        verification_notifications = conn.execute(
            "DELETE FROM agent_download_verification_notification_outbox WHERE id IN ("
            "SELECT id FROM agent_download_verification_notification_outbox WHERE "
            "(status='sent' AND COALESCE(NULLIF(sent_at,''),updated_at,created_at)<?) "
            "OR (status='discarded' AND updated_at<?) "
            "ORDER BY updated_at,id LIMIT ?)",
            (cutoff, cutoff, limit),
        )
        verification = conn.execute(
            "DELETE FROM agent_download_verifications WHERE request_id IN ("
            "SELECT request_id FROM agent_download_verifications "
            "WHERE status IN ('visible','attention') AND updated_at<? "
            "ORDER BY updated_at,request_id LIMIT ?)",
            (cutoff, limit),
        )
        notifications = conn.execute(
            "DELETE FROM agent_library_patrol_notification_outbox WHERE id IN ("
            "SELECT id FROM agent_library_patrol_notification_outbox WHERE "
            "(status='sent' AND COALESCE(NULLIF(sent_at,''),updated_at,created_at)<?) "
            "OR (status='discarded' AND updated_at<?) "
            "ORDER BY updated_at,id LIMIT ?)",
            (cutoff, cutoff, limit),
        )
        action_history = conn.execute(
            "DELETE FROM agent_action_history WHERE id IN ("
            "SELECT id FROM agent_action_history WHERE finished_at<? "
            "ORDER BY finished_at,id LIMIT ?)",
            (cutoff, limit),
        )
        jobs = conn.execute(
            "DELETE FROM agent_jobs WHERE job_id IN ("
            "SELECT job_id FROM agent_jobs WHERE status IN ('succeeded','failed','cancelled') "
            "AND updated_at<? ORDER BY updated_at,job_id LIMIT ?)",
            (cutoff, limit),
        )
        provider_plans = conn.execute(
            "DELETE FROM agent_provider_plans WHERE plan_id IN ("
            "SELECT plan_id FROM agent_provider_plans WHERE status IN "
            "('succeeded','failed','stale','outcome_unknown') AND updated_at<? "
            "ORDER BY updated_at,plan_id LIMIT ?)",
            (cutoff, limit),
        )
        organize_jobs = conn.execute(
            "DELETE FROM organize_operation_jobs WHERE job_id IN ("
            "SELECT job_id FROM organize_operation_jobs WHERE status IN "
            "('completed','partial','failed','cancelled','manual_review') "
            "AND updated_at<? ORDER BY updated_at,job_id LIMIT ?)",
            (cutoff, limit),
        )
        workflows = conn.execute(
            "DELETE FROM agent_missing_media_workflows WHERE workflow_id IN ("
            "SELECT workflow_id FROM agent_missing_media_workflows "
            "WHERE state IN ('visible','stale','cancelled') AND updated_at<? "
            "ORDER BY updated_at,workflow_id LIMIT ?)",
            (cutoff, limit),
        )
        web_usage = conn.execute(
            "DELETE FROM agent_web_search_daily_usage WHERE usage_date<?",
            (cutoff[:10],),
        )
        conn.execute(
            "UPDATE agent_maintenance SET next_run_at=?,updated_at=? "
            "WHERE task_key='history_cleanup'",
            (next_run, current),
        )
        return {
            "performed": True,
            "next_cleanup_at": next_run,
            "download_verifications": max(0, int(verification.rowcount)),
            "download_verification_notification_outbox": max(
                0, int(verification_notifications.rowcount)
            ),
            "patrol_notification_outbox": max(0, int(notifications.rowcount)),
            "action_history": max(0, int(action_history.rowcount)),
            "jobs": max(0, int(jobs.rowcount)),
            "provider_plans": max(0, int(provider_plans.rowcount)),
            "organize_operation_jobs": max(0, int(organize_jobs.rowcount)),
            "missing_media_workflows": max(0, int(workflows.rowcount)),
            "web_search_usage": max(0, int(web_usage.rowcount)),
        }


def purge_agent_subject_data(
    *, owner: str, principal: str | None = None
) -> dict[str, int]:
    """清除一个主体的全部 Agent 持久化数据；与单会话删除语义明确分离。"""
    normalized_owner = str(owner or "").strip()
    normalized_principal = str(principal if principal is not None else owner).strip()
    if not normalized_owner or len(normalized_owner) > 512 or not normalized_principal:
        raise ValueError("Agent 隐私清理主体无效")
    from app.modules.web_secret import get_web_secret

    secret = get_web_secret().encode("utf-8")

    def digest(domain: bytes, value: str) -> str:
        return hmac.new(
            secret, domain + value.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    digests = {
        "action_history": digest(
            b"mediaflux-agent-action-history:v1\0", normalized_owner
        ),
        "session_context": digest(
            b"mediaflux-agent-session-context:v1\0", normalized_owner
        ),
        "confirmations": digest(b"mediaflux-agent-confirmation:v1\0", normalized_owner),
        "jobs": digest(b"mediaflux-agent-durable-job:v1\0", normalized_owner),
        "provider_plans": digest(
            b"mediaflux-agent-provider-plan-owner:v1\0", normalized_owner
        ),
        "workflows": digest(b"mediaflux-agent-missing-workflow:v1\0", normalized_owner),
        "telegram_actions": digest(
            b"mediaflux-telegram-agent-action:v1\0", normalized_owner
        ),
        "conversations": digest(
            b"mediaflux-agent-conversation-principal:v1\0", normalized_principal
        ),
    }
    deleted: dict[str, int] = {}
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for key, table, column in (
            ("action_history", "agent_action_history", "owner_digest"),
            ("media_preferences", "agent_media_preferences", "owner_digest"),
            ("session_context", "agent_session_context", "owner_digest"),
            ("session_context_epochs", "agent_session_context_epochs", "owner_digest"),
            ("confirmations", "agent_confirmations", "owner_digest"),
            ("confirmation_epochs", "agent_confirmation_epochs", "owner_digest"),
            ("jobs", "agent_jobs", "owner_digest"),
            ("workflows", "agent_missing_media_workflows", "owner_digest"),
            ("telegram_actions", "telegram_agent_actions", "owner_digest"),
            ("conversations", "agent_conversations", "principal_digest"),
            ("conversation_epochs", "agent_conversation_epochs", "principal_digest"),
        ):
            digest_key = {
                "media_preferences": "action_history",
                "confirmation_epochs": "confirmations",
                "session_context_epochs": "session_context",
                "organize_operation_jobs": "jobs",
                "conversation_epochs": "conversations",
            }.get(key, key)
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE {column}=?", (digests[digest_key],)
            )
            deleted[key] = max(0, int(cursor.rowcount or 0))
        # 已进入远端写操作的任务不能直接删行：执行线程可能已持有参数快照。
        # 先最小化持久数据并设置取消位，worker 会在真正写入前再次检查；
        # 未运行任务和终态可立即删除。
        purge_time = now()
        running = conn.execute(
            "UPDATE organize_operation_jobs SET cancel_requested=1,purged_at=?,"
            "payload_json='{}',payload_auth='',reference='',result_json='{}',"
            "error_code='',error='',updated_at=? "
            "WHERE owner_digest=? AND status='running'",
            (purge_time, purge_time, digests["jobs"]),
        )
        removable = conn.execute(
            "DELETE FROM organize_operation_jobs WHERE owner_digest=? AND status<>'running'",
            (digests["jobs"],),
        )
        deleted["organize_operation_jobs"] = max(0, int(running.rowcount or 0)) + max(
            0, int(removable.rowcount or 0)
        )
        provider_running = conn.execute(
            "UPDATE agent_provider_plans SET "
            "owner_digest=lower(hex(randomblob(32))),"
            "session_digest=lower(hex(randomblob(32))),provider='',profile_ref='',"
            "operation='',risk='write',arguments_json='{}',target_snapshot_json='{}',"
            "result_json='{}',context_fingerprint='',summary='',"
            "error_code='privacy_purge_pending',updated_at=? "
            "WHERE owner_digest=? AND status='running'",
            (purge_time, digests["provider_plans"]),
        )
        provider_removable = conn.execute(
            "DELETE FROM agent_provider_plans WHERE owner_digest=? AND status<>'running'",
            (digests["provider_plans"],),
        )
        scrubbed_provider_plans = max(0, int(provider_running.rowcount or 0))
        removed_provider_plans = max(0, int(provider_removable.rowcount or 0))
        deleted["provider_plans_scrubbed_running"] = scrubbed_provider_plans
        deleted["provider_plans_deleted"] = removed_provider_plans
        deleted["provider_plans"] = scrubbed_provider_plans + removed_provider_plans
    return deleted


# ===== Agent 媒体库巡检 =====
# 统一数据访问门面：巡检结果、版本与通知发件箱保持单事务一致性。
# ===== Agent owner 隔离的可恢复长任务 =====
from app.repositories.agent_jobs import (  # noqa: E402,F401
    cancel_agent_job,
    claim_due_agent_job,
    complete_agent_job,
    continue_agent_job,
    create_agent_job,
    fail_or_retry_agent_job,
    finalize_cancelled_agent_job,
    find_active_agent_job,
    find_latest_active_agent_job,
    get_agent_job,
    is_agent_job_cancel_requested,
    list_agent_jobs,
    release_agent_job_lease,
    renew_agent_job_lease,
)
from app.repositories.agent_library_patrol import (  # noqa: E402,F401
    cancel_agent_library_patrol_lease,
    claim_due_agent_library_patrol,
    claim_due_agent_library_patrol_notification,
    complete_agent_library_patrol_notification,
    continue_agent_library_patrol,
    discard_agent_library_patrol_notification,
    discard_agent_library_patrol_notifications,
    ensure_agent_library_patrol,
    get_agent_library_patrol,
    list_agent_library_patrol_notifications,
    release_agent_library_patrol_notification,
    reschedule_agent_library_patrol,
    retry_agent_library_patrol_cycle,
    retry_agent_library_patrol_notification,
    update_agent_library_patrol,
)

# ===== 下载请求认领与本地入库状态 =====
from app.repositories.download_requests import (  # noqa: E402,F401
    cancel_pending_download_request,
    cancel_qb_download_tracking,
    claim_download_request,
    claim_download_request_notification,
    claim_download_request_organize,
    claim_download_request_staging_finalize,
    claim_download_request_targets,
    finalize_download_request_notification,
    finalize_download_request_submission,
    get_download_request_by_request_key,
    get_download_request_by_request_keys,
    link_download_request_to_local_media_task,
    list_active_download_requests,
    list_protected_guangya_staging_ids,
    mark_download_request_local_media_failed,
    mark_download_request_local_media_skipped,
    recover_stale_submitting_download_requests,
    update_download_request,
    update_download_request_and_sync_media_admission,
)

# ===== 媒体订阅、候选资源与下载准入 =====
# 独立于 RSS schema；统一订阅中心只在应用层聚合。
from app.repositories.media_subscriptions import (  # noqa: E402,F401
    add_media_subscription,
    add_media_subscription_run,
    begin_media_download_dispatch,
    cancel_media_subscription_run,
    claim_media_download_admission,
    claim_media_subscription_check_run,
    complete_media_download_admissions,
    count_media_subscriptions,
    delete_media_subscription,
    fail_media_subscription_check,
    fail_unbound_media_download_admission,
    finalize_media_subscription_check,
    finish_media_subscription_run,
    get_media_subscription,
    get_media_subscription_candidate,
    get_media_subscription_stats,
    list_active_media_download_admissions,
    list_due_media_subscriptions,
    list_media_subscription_candidates,
    list_media_subscription_candidates_by_ids,
    list_media_subscription_runs,
    list_media_subscription_workflows,
    list_media_subscriptions,
    media_subscription_check_is_active,
    reconcile_media_download_admissions,
    reconcile_startup_media_download_admissions,
    recover_stale_media_subscription_checks,
    replace_media_subscription_candidates,
    sync_media_download_admission_for_request,
    update_media_download_admission,
    update_media_subscription_candidate,
    update_media_subscription_config,
    upsert_media_subscription,
)

# ===== RSS 订阅、条目状态机与诊断 =====
# 统一数据访问门面：schema/migration 由 init_db 集中持有。
from app.repositories.rss import (  # noqa: E402,F401
    add_rss_entry_with_media,
    add_rss_subscription,
    claim_pending_rss_qb_entries,
    claim_retryable_failed_rss_entries,
    claim_rss_entry,
    count_rss_downloaded_entries_since,
    delete_rss_subscription,
    find_rss_subscriptions_by_normalized_name,
    get_pending_rss_qb_snapshot,
    get_retryable_failed_rss_snapshot,
    get_rss_diagnostic_summary,
    get_rss_entries_by_ids,
    get_rss_entry,
    get_rss_manual_review_summary,
    get_rss_stats,
    get_rss_subscription,
    get_rss_subscription_safe_summary,
    list_due_rss_subscriptions,
    list_enabled_rss_subscription_safe_targets,
    list_enabled_rss_subscriptions,
    list_rss_entries,
    list_rss_subscription_safe_summaries,
    list_rss_subscriptions,
    purge_processed_rss_entries,
    record_rss_entry_failure,
    recover_stale_submitting_rss_entries,
    rss_retry_entry_snapshot,
    skip_pending_rss_entries,
    update_rss_entries_processed,
    update_rss_entries_processed_snapshot,
    update_rss_subscription,
)

from app.repositories.strm_request_ownership import (  # noqa: E402,F401
    current_strm_request_owners,
    has_pending_strm_request_refresh,
    request_owners_for_work,
    update_strm_request_state,
)

# ===== STRM 索引 =====
# 统一数据访问门面：核心索引 CRUD 按业务域拆分并共享同一实现。
from app.repositories.strm import (  # noqa: E402,F401
    acknowledge_strm_refresh_paths,
    cancel_retired_strm_metadata_jobs,
    cancel_stale_strm_metadata_jobs,
    cancel_strm_metadata_job,
    claim_due_strm_metadata_jobs,
    claim_strm_change_targets,
    complete_strm_change_target,
    settle_strm_metadata_job,
    count_due_strm_change_targets,
    count_pending_strm_change_targets,
    count_strm_metadata_jobs,
    count_strm_refresh_paths,
    delete_strm_index_ids,
    delete_strm_path_cleanup,
    enqueue_strm_change_targets,
    enqueue_strm_metadata_jobs,
    enqueue_strm_path_cleanup,
    enqueue_strm_refresh_paths,
    fail_or_retry_strm_metadata_job,
    fail_strm_change_target,
    group_changes_by_target,
    list_strm_change_queue,
    list_strm_index,
    list_strm_index_by_prefix,
    list_strm_indexes_by_file_id,
    list_strm_installation_rows,
    list_strm_path_cleanup,
    list_strm_path_owners,
    list_strm_metadata_queue,
    list_strm_refresh_entries,
    merge_strm_changes,
    recover_stale_strm_change_targets,
    recover_stale_strm_metadata_jobs,
    release_strm_change_targets,
    renew_strm_change_target_leases,
    renew_strm_metadata_job_lease,
    requeue_strm_metadata_jobs,
    reschedule_strm_change_targets,
    seconds_until_next_strm_change_target,
    strm_metadata_job_is_current,
    upsert_strm_index,
    upsert_strm_index_batch,
)

_STRM_SOURCE_SNAPSHOT_KEY = "strm.configured_sources.snapshot.v1"


def _normalize_strm_source_snapshot(
    sources: Iterable[object],
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for index, raw in enumerate(sources):
        if isinstance(raw, dict):
            source_id = str(raw.get("id") or "").strip()
            source_name = str(raw.get("name") or "").strip()
        else:
            source_id = str(raw or "").strip()
            source_name = ""
        if not source_id or source_id == "0":
            continue
        normalized.setdefault(source_id, source_name or f"源目录{index + 1}")
    return normalized


def _write_strm_source_snapshot(
    conn: sqlite3.Connection,
    sources: dict[str, str],
    strm_root: str,
) -> None:
    payload = json.dumps(
        {
            "version": 1,
            "strm_root": str(strm_root or ""),
            "sources": [
                {"id": source_id, "name": source_name}
                for source_id, source_name in sources.items()
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO settings_kv(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (_STRM_SOURCE_SNAPSHOT_KEY, payload, now()),
    )


def _read_strm_source_snapshot(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], str] | None:
    row = conn.execute(
        "SELECT value FROM settings_kv WHERE key=?", (_STRM_SOURCE_SNAPSHOT_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["value"] or ""))
        if not isinstance(payload, dict) or int(payload.get("version") or 0) != 1:
            raise ValueError("unsupported snapshot")
        raw_sources = payload.get("sources")
        if not isinstance(raw_sources, list):
            raise ValueError("invalid sources")
        return (
            _normalize_strm_source_snapshot(raw_sources),
            str(payload.get("strm_root") or ""),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("STRM 来源快照损坏，已按当前配置重建")
        return None


def _apply_strm_source_reconciliation(
    conn: sqlite3.Connection,
    active_ids: tuple[str, ...],
    retired_sources: dict[str, tuple[str, str]],
) -> None:
    if active_ids:
        placeholders = ",".join("?" for _ in active_ids)
        conn.execute(
            f"DELETE FROM strm_retired_sources WHERE source_id IN ({placeholders})",
            active_ids,
        )
    timestamp = now()
    for source_id, (source_name, strm_root) in retired_sources.items():
        conn.execute(
            "INSERT INTO strm_retired_sources(source_id,source_name,strm_root,queued_at,"
            "updated_at,attempts,last_error) VALUES(?,?,?,?,?,0,'') "
            "ON CONFLICT(source_id) DO UPDATE SET source_name=excluded.source_name,"
            "strm_root=excluded.strm_root,updated_at=excluded.updated_at,last_error=''",
            (source_id, source_name, strm_root, timestamp, timestamp),
        )


@contextmanager
def reconcile_strm_retired_sources_transaction(
    active_source_ids: Iterable[object],
    retired_sources: Iterable[tuple[object, object, object]],
    *,
    configured_sources: Iterable[object] | None = None,
    configured_strm_root: str = "",
):
    """在配置发布事务内同步 STRM 退役队列和已发布来源快照。

    调用方必须在 ``with`` 块内完成配置文件发布。若发布或 SQLite 提交失败，
    两项数据库状态会一起回滚；进程异常退出后，启动恢复可由旧快照与新配置
    重建缺失的退役记录。
    """
    active_ids = tuple(
        dict.fromkeys(
            str(item).strip() for item in active_source_ids if str(item).strip()
        )
    )
    normalized_retired: dict[str, tuple[str, str]] = {}
    for raw_id, raw_name, raw_root in retired_sources:
        source_id = str(raw_id).strip()
        if not source_id or source_id in active_ids:
            continue
        normalized_retired[source_id] = (
            str(raw_name or source_id),
            str(raw_root or ""),
        )

    with get_conn() as conn:
        _apply_strm_source_reconciliation(conn, active_ids, normalized_retired)
        if configured_sources is not None:
            _write_strm_source_snapshot(
                conn,
                _normalize_strm_source_snapshot(configured_sources),
                configured_strm_root,
            )
        yield list(normalized_retired)


def reconcile_configured_strm_sources(
    configured_sources: Iterable[object],
    strm_root: str,
) -> list[str]:
    """启动时用持久快照修复配置发布与 SQLite 提交之间的崩溃窗口。"""
    current = _normalize_strm_source_snapshot(configured_sources)
    active_ids = tuple(current)
    with get_conn() as conn:
        previous = _read_strm_source_snapshot(conn)
        retired: dict[str, tuple[str, str]] = {}
        if previous is not None:
            previous_sources, previous_root = previous
            retired = {
                source_id: (source_name, previous_root)
                for source_id, source_name in previous_sources.items()
                if source_id not in current
            }
        _apply_strm_source_reconciliation(conn, active_ids, retired)
        _write_strm_source_snapshot(conn, current, strm_root)
        return list(retired)


def enqueue_strm_retired_source(
    source_id: str, source_name: str, strm_root: str
) -> None:
    timestamp = now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO strm_retired_sources(source_id,source_name,strm_root,queued_at,"
            "updated_at,attempts,last_error) VALUES(?,?,?,?,?,0,'') "
            "ON CONFLICT(source_id) DO UPDATE SET source_name=excluded.source_name,"
            "strm_root=excluded.strm_root,updated_at=excluded.updated_at,last_error=''",
            (
                str(source_id),
                str(source_name or ""),
                str(strm_root or ""),
                timestamp,
                timestamp,
            ),
        )


def cancel_strm_retired_sources(source_ids: list[str]) -> int:
    ids = list(dict.fromkeys(str(item) for item in source_ids if str(item)))
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with get_conn() as conn:
        cur = conn.execute(
            f"DELETE FROM strm_retired_sources WHERE source_id IN ({placeholders})", ids
        )
        return cur.rowcount


def list_strm_retired_sources() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM strm_retired_sources ORDER BY queued_at, source_id"
        ).fetchall()


def update_strm_retired_source_error(source_id: str, error: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE strm_retired_sources SET attempts=attempts+1,last_error=?,updated_at=? "
            "WHERE source_id=?",
            (str(error or "")[:500], now(), str(source_id)),
        )


def delete_strm_retired_source(source_id: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM strm_retired_sources WHERE source_id=?", (str(source_id),)
        )
        return cur.rowcount


def _sanitize_strm_failure_error(value: object) -> str:
    from app.logger import redact_sensitive_text

    text = redact_sensitive_text(value)
    text = re.sub(r"https?://[^\s\"'<>]+", "[redacted-url]", text)
    return text[:500]


def record_strm_failure(
    *,
    source_id: str,
    source_name: str,
    file_id: str,
    parent_id: str,
    filename: str,
    action: str,
    rel_dir: str,
    target_rel_path: str,
    error: object,
) -> int:
    if action not in {"generate", "metadata"}:
        raise ValueError("STRM failure action must be generate or metadata")
    timestamp = now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO strm_failures("
            "source_id,source_name,file_id,parent_id,filename,action,rel_dir,"
            "target_rel_path,error,status,failure_count,retry_count,created_at,updated_at,resolved_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,'open',1,0,?,?,NULL) "
            "ON CONFLICT(source_id,file_id,action) DO UPDATE SET "
            "source_name=excluded.source_name,parent_id=excluded.parent_id,"
            "filename=excluded.filename,rel_dir=excluded.rel_dir,"
            "target_rel_path=excluded.target_rel_path,error=excluded.error,status='open',"
            "failure_count=strm_failures.failure_count+1,updated_at=excluded.updated_at,resolved_at=NULL",
            (
                str(source_id),
                str(source_name or ""),
                str(file_id),
                str(parent_id or ""),
                str(filename),
                action,
                str(rel_dir or ""),
                str(target_rel_path or ""),
                _sanitize_strm_failure_error(error),
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT id FROM strm_failures WHERE source_id=? AND file_id=? AND action=?",
            (str(source_id), str(file_id), action),
        ).fetchone()
        return int(row["id"])


def list_strm_failures(
    *,
    status: str = "open",
    source_id: str = "",
    action: str = "",
    ids: list[int] | None = None,
    before_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    values: list[object] = []
    if status and status != "all":
        clauses.append("status=?")
        values.append(status)
    if source_id:
        clauses.append("source_id=?")
        values.append(str(source_id))
    if action:
        clauses.append("action=?")
        values.append(str(action))
    if before_id is not None:
        clauses.append("id<?")
        values.append(int(before_id))
    if ids is not None:
        normalized = list(dict.fromkeys(int(item) for item in ids))
        if not normalized:
            return []
        clauses.append(f"id IN ({','.join('?' for _ in normalized)})")
        values.extend(normalized)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(max(1, min(int(limit or 200), 1000)))
    offset_sql = ""
    if offset > 0:
        offset_sql = " OFFSET ?"
        values.append(max(0, int(offset)))
    with get_conn() as conn:
        return conn.execute(
            f"SELECT * FROM strm_failures{where} ORDER BY id DESC LIMIT ?{offset_sql}",
            values,
        ).fetchall()


def get_strm_failure_retry_snapshot(
    *,
    action: str = "",
    limit: int = 101,
) -> list[sqlite3.Row]:
    """返回确认绑定所需的最小失败项快照，不读取路径、文件名或错误正文。"""
    if action not in {"", "generate", "metadata"}:
        raise ValueError("STRM failure retry action must be generate or metadata")
    clauses = ["status='open'", "action IN ('generate','metadata')"]
    values: list[object] = []
    if action:
        clauses.append("action=?")
        values.append(action)
    values.append(max(1, min(int(limit or 101), 1000)))
    with get_conn() as conn:
        return conn.execute(
            "SELECT id,action,failure_count,retry_count,updated_at FROM strm_failures WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id DESC LIMIT ?",
            values,
        ).fetchall()


def claim_strm_failures(ids: list[int], *, limit: int = 1000) -> list[sqlite3.Row]:
    normalized = list(dict.fromkeys(int(item) for item in ids))[: max(1, int(limit))]
    if not normalized:
        return []
    placeholders = ",".join("?" for _ in normalized)
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        claimable = conn.execute(
            f"SELECT id FROM strm_failures WHERE status='open' "
            f"AND id IN ({placeholders}) ORDER BY id DESC",
            normalized,
        ).fetchall()
        claimed_ids = [int(row["id"]) for row in claimable]
        if not claimed_ids:
            return []
        claimed_placeholders = ",".join("?" for _ in claimed_ids)
        cur = conn.execute(
            f"UPDATE strm_failures SET status='retrying',retry_count=retry_count+1,"
            f"updated_at=? WHERE status='open' AND id IN ({claimed_placeholders})",
            [timestamp, *claimed_ids],
        )
        if cur.rowcount != len(claimed_ids):
            raise RuntimeError("STRM 失败项 claim 状态竞争")
        return conn.execute(
            f"SELECT * FROM strm_failures WHERE status='retrying' "
            f"AND id IN ({claimed_placeholders}) ORDER BY id DESC",
            claimed_ids,
        ).fetchall()


def count_strm_failures(
    *, status: str = "open", source_id: str = "", action: str = ""
) -> int:
    clauses = []
    values: list[object] = []
    if status and status != "all":
        clauses.append("status=?")
        values.append(status)
    if source_id:
        clauses.append("source_id=?")
        values.append(str(source_id))
    if action:
        clauses.append("action=?")
        values.append(str(action))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM strm_failures" + where, values
            ).fetchone()["count"]
        )


def summarize_strm_failures() -> dict:
    with get_conn() as conn:
        open_count = int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM strm_failures WHERE status='open'"
            ).fetchone()["count"]
        )
        resolved_count = int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM strm_failures WHERE status='resolved'"
            ).fetchone()["count"]
        )
        rows = conn.execute(
            "SELECT source_id,MAX(source_name) AS source_name,COUNT(*) AS count "
            "FROM strm_failures WHERE status='open' GROUP BY source_id ORDER BY source_name,source_id"
        ).fetchall()
    sources = [
        {
            "id": str(row["source_id"]),
            "name": str(row["source_name"] or row["source_id"]),
            "open": int(row["count"]),
        }
        for row in rows
    ]
    return {
        "open": open_count,
        "resolved": resolved_count,
        "total": open_count + resolved_count,
        "by_source": {item["id"]: item["open"] for item in sources},
        "sources": sources,
    }


def get_strm_failure_triage_summary() -> dict[str, object]:
    """只读聚合 STRM 失败账本，不读取对象标识、路径、文件名或错误正文。"""
    statuses = ("open", "retrying", "resolved")
    actions = ("generate", "metadata")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status,action,COUNT(*) AS count,"
            "SUM(CASE WHEN status IN ('open','retrying') AND failure_count>=2 THEN 1 ELSE 0 END) AS active_repeated,"
            "SUM(CASE WHEN status IN ('open','retrying') AND retry_count>=1 THEN 1 ELSE 0 END) AS active_retried "
            "FROM strm_failures "
            "WHERE status IN ('open','retrying','resolved') "
            "AND action IN ('generate','metadata') "
            "GROUP BY status,action"
        ).fetchall()

    by_action = {
        action: {"total": 0, "open": 0, "retrying": 0, "resolved": 0}
        for action in actions
    }
    summary: dict[str, object] = {
        "total": 0,
        "open": 0,
        "retrying": 0,
        "resolved": 0,
        "active_repeated": 0,
        "active_retried": 0,
        "by_action": by_action,
    }
    for row in rows:
        status = str(row["status"] or "")
        action = str(row["action"] or "")
        if status not in statuses or action not in actions:
            continue
        count = max(0, int(row["count"] or 0))
        by_action[action][status] += count
        by_action[action]["total"] += count
        summary[status] = int(summary[status]) + count
        summary["total"] = int(summary["total"]) + count
        summary["active_repeated"] = int(summary["active_repeated"]) + max(
            0, int(row["active_repeated"] or 0)
        )
        summary["active_retried"] = int(summary["active_retried"]) + max(
            0, int(row["active_retried"] or 0)
        )
    return summary


def resolve_strm_failure(failure_id: int, *, expected_status: str = "retrying") -> bool:
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE strm_failures SET status='resolved',updated_at=?,resolved_at=? "
            "WHERE id=? AND status=?",
            (timestamp, timestamp, int(failure_id), str(expected_status)),
        )
        return cur.rowcount == 1


def _resolve_strm_failure_for_item_conn(
    conn: sqlite3.Connection, source_id: str, file_id: str, action: str, *, timestamp: str,
) -> int:
    """独立恢复与元数据完成事务共用同一失败项确认，不单独提交外层事务。"""
    cur = conn.execute(
        "UPDATE strm_failures SET status='resolved',updated_at=?,resolved_at=? "
        "WHERE source_id=? AND file_id=? AND action=? AND status='open'",
        (timestamp, timestamp, str(source_id), str(file_id), str(action)),
    )
    return cur.rowcount


def resolve_strm_failure_for_item(source_id: str, file_id: str, action: str) -> int:
    timestamp = now()
    with get_conn() as conn:
        return _resolve_strm_failure_for_item_conn(
            conn, source_id, file_id, action, timestamp=timestamp,
        )


def resolve_strm_failures_for_items(
    source_id: str,
    file_ids: Iterable[str],
    action: str,
    *,
    chunk_size: int = 400,
) -> int:
    """批量关闭同一来源/动作下已成功落盘的 STRM 失败项。"""
    ids = list(dict.fromkeys(str(item) for item in file_ids if str(item)))
    if not ids:
        return 0
    safe_chunk = max(1, min(int(chunk_size or 400), 400))
    timestamp = now()
    updated = 0
    with get_conn() as conn:
        for offset in range(0, len(ids), safe_chunk):
            chunk = ids[offset : offset + safe_chunk]
            placeholders = ",".join("?" for _ in chunk)
            cur = conn.execute(
                "UPDATE strm_failures SET status='resolved',updated_at=?,resolved_at=? "
                "WHERE source_id=? AND action=? AND status='open' "
                f"AND file_id IN ({placeholders})",
                [timestamp, timestamp, str(source_id), str(action), *chunk],
            )
            updated += int(cur.rowcount or 0)
    return updated


def mark_strm_failure_stale(
    failure_id: int, *, error: object, expected_status: str = "retrying"
) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE strm_failures SET status='open',error=?,"
            "failure_count=failure_count+1,updated_at=?,resolved_at=NULL "
            "WHERE id=? AND status=?",
            (
                _sanitize_strm_failure_error(error),
                now(),
                int(failure_id),
                str(expected_status),
            ),
        )
        return cur.rowcount == 1


def release_strm_failure_retry(
    failure_id: int, *, error: object, expected_status: str = "retrying"
) -> bool:
    """扫描无法确认对象状态时释放 claim，不把未知状态计作业务失败。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE strm_failures SET status='open',error=?,updated_at=?,resolved_at=NULL "
            "WHERE id=? AND status=?",
            (
                _sanitize_strm_failure_error(error),
                now(),
                int(failure_id),
                str(expected_status),
            ),
        )
        return cur.rowcount == 1


def update_strm_failure_retry(
    failure_id: int,
    *,
    source_id: str,
    source_name: str,
    file,
    rel_dir: str,
    target_rel_path: str,
    error: object,
    expected_status: str = "retrying",
) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE strm_failures SET source_id=?,source_name=?,parent_id=?,filename=?,"
            "rel_dir=?,target_rel_path=?,error=?,status='open',"
            "failure_count=failure_count+1,updated_at=?,resolved_at=NULL "
            "WHERE id=? AND status=?",
            (
                str(source_id),
                str(source_name or ""),
                str(file.parent_id or ""),
                str(file.name),
                str(rel_dir or ""),
                str(target_rel_path or ""),
                _sanitize_strm_failure_error(error),
                now(),
                int(failure_id),
                str(expected_status),
            ),
        )
        return cur.rowcount == 1


def delete_strm_failures(
    *,
    ids: list[int] | None = None,
    all_items: bool = False,
    status: str = "",
    source_id: str = "",
    action: str = "",
) -> int:
    """按 ID 列表或筛选条件清理 STRM 失败台账记录。"""
    clauses: list[str] = []
    values: list[object] = []
    if not all_items and ids is not None:
        normalized = list(dict.fromkeys(int(item) for item in ids if int(item) > 0))
        if not normalized:
            return 0
        clauses.append(f"id IN ({','.join('?' for _ in normalized)})")
        values.extend(normalized)
    else:
        if status and status != "all":
            clauses.append("status=?")
            values.append(status)
        if source_id:
            clauses.append("source_id=?")
            values.append(str(source_id))
        if action:
            clauses.append("action=?")
            values.append(str(action))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM strm_failures" + where, values)
        return int(cur.rowcount or 0)


_UUID_TEST_SOURCE_RE = re.compile(
    r"(?i)(?:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}|[0-9a-f]{32})"
)


def _configured_strm_source_ids() -> set[str]:
    """读取当前真实 STRM 源 ID，兼容多源和旧单源配置。"""
    from app import config

    source_ids: set[str] = set()
    raw = config.get("GY_STRM_SOURCE_DIRS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str):
                    source_id = item.strip()
                elif isinstance(item, dict):
                    source_id = str(item.get("id", "")).strip()
                else:
                    source_id = ""
                if source_id:
                    source_ids.add(source_id)
    return source_ids


def _strm_source_id(source: str) -> str:
    source = str(source or "").strip()
    for prefix in ("guangya:", "guangya-meta:"):
        if source.startswith(prefix):
            return source[len(prefix) :]
    return source


def _classify_strm_index_row(
    row: sqlite3.Row, strm_root: str, configured_source_ids: set[str], temp_root: Path
) -> dict[str, bool]:
    path = Path(str(row["strm_path"] or "")).expanduser()
    if not path.is_absolute():
        path = Path(str(strm_root or "")).expanduser() / path
    resolved_path = path.resolve(strict=False)
    in_temp_dir = resolved_path.is_relative_to(temp_root)
    exists = resolved_path.is_file()
    source_id = _strm_source_id(row["source"])
    real_source = source_id in configured_source_ids
    uuid_test_source = bool(_UUID_TEST_SOURCE_RE.search(source_id))
    return {
        "existing": exists,
        "missing": not exists,
        "real_source": real_source,
        "confirmed_test_artifact": (
            in_temp_dir and uuid_test_source and not exists and not real_source
        ),
    }


def _strm_index_kind(source: str) -> str:
    source = str(source or "").strip()
    if (
        source.startswith("guangya-meta:")
        or source.startswith("local-meta:")
        or "-meta:" in source
    ):
        return "metadata"
    if (
        source.startswith("guangya:")
        or source.startswith("local:")
        or source.startswith("115:")
        or source.startswith("quark:")
    ):
        return "video"
    return "other"


def list_strm_index_diagnostics(strm_root: str) -> dict:
    """只读统计 STRM 索引状态，仅返回安全计数和确认测试行 ID。"""
    configured_source_ids = _configured_strm_source_ids()
    temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,source,strm_path FROM strm_index ORDER BY id"
        ).fetchall()

    result = {
        "total": len(rows),
        "existing": 0,
        "missing": 0,
        "real_source": 0,
        "confirmed_test_artifact": 0,
        "confirmed_test_artifact_ids": [],
        "configured_source_count": len(configured_source_ids),
        "video": {
            "total": 0,
            "existing": 0,
            "missing": 0,
        },
        "metadata": {
            "total": 0,
            "existing": 0,
            "missing": 0,
        },
        "other": {
            "total": 0,
            "existing": 0,
            "missing": 0,
        },
        "metadata_queue": count_strm_metadata_jobs(),
    }
    for row in rows:
        classification = _classify_strm_index_row(row, strm_root, configured_source_ids, temp_root)
        for key in ("existing", "missing", "real_source", "confirmed_test_artifact"):
            result[key] += int(classification[key])
        if classification["confirmed_test_artifact"]:
            result["confirmed_test_artifact_ids"].append(int(row["id"]))

        kind = _strm_index_kind(row["source"])
        sub = result[kind]
        sub["total"] += 1
        if classification["existing"]:
            sub["existing"] += 1
        else:
            sub["missing"] += 1
    return result


def delete_confirmed_test_strm_indexes(ids: list[int]) -> int:
    """事务内复核并删除确认的测试索引；不执行任何文件系统删除。"""
    if not isinstance(ids, list):
        raise ValueError("索引 ID 必须为列表")
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in ids):
        raise ValueError("索引 ID 必须为正整数")
    normalized_ids = list(dict.fromkeys(ids))
    if not normalized_ids:
        return 0

    from app import config

    strm_root = config.get("STRM_ROOT", "")
    configured_source_ids = _configured_strm_source_ids()
    temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    placeholders = ",".join("?" for _ in normalized_ids)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"SELECT id,source,strm_path FROM strm_index WHERE id IN ({placeholders})",
            normalized_ids,
        ).fetchall()
        if len(rows) != len(normalized_ids) or any(
            not _classify_strm_index_row(row, strm_root, configured_source_ids, temp_root)[
                "confirmed_test_artifact"
            ]
            for row in rows
        ):
            raise ValueError("请求包含非确认测试索引，已拒绝整个清理请求")
        cursor = conn.execute(
            f"DELETE FROM strm_index WHERE id IN ({placeholders})", normalized_ids
        )
        return cursor.rowcount


# ===== Telegram 整理候选确认 =====
def create_organize_confirmation(
    *,
    token: str,
    fingerprint: str,
    chat_id: str,
    source_name: str,
    directory_path: str,
    payload: dict,
    expires_at: str,
    organize_task_id: str = "",
    review_requested: bool = False,
    review_ready: bool = False,
) -> int:
    """持久化一组 Telegram 整理候选，并使同指纹旧按钮失效。"""
    timestamp = now()
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE organize_confirmations SET status='expired',"
            "result_json=?,error='新的候选卡已替代本次确认',"
            "review_status=CASE WHEN review_status IN ('waiting','pending','running') "
            "THEN 'cancelled' ELSE review_status END,"
            "review_completed_at=CASE WHEN review_status IN ('waiting','pending','running') "
            "THEN ? ELSE review_completed_at END,"
            "completed_at=COALESCE(completed_at,?),rollup_applied=0,updated_at=? "
            "WHERE fingerprint=? AND status='pending'",
            (
                json.dumps({"resolution": "superseded"}, ensure_ascii=False),
                timestamp,
                timestamp,
                timestamp,
                str(fingerprint),
            ),
        )
        cursor = conn.execute(
            "INSERT INTO organize_confirmations("
            "token,fingerprint,chat_id,source_name,directory_path,payload_json,status,"
            "organize_task_id,rollup_applied,review_status,expires_at,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,'pending',?,0,?,?,?,?)",
            (
                str(token),
                str(fingerprint),
                str(chat_id or ""),
                str(source_name or ""),
                str(directory_path or ""),
                encoded,
                str(organize_task_id or ""),
                ("pending" if review_ready else "waiting") if review_requested else "",
                str(expires_at),
                timestamp,
                timestamp,
            ),
        )
        return int(cursor.lastrowid)


def get_organize_confirmation(token: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE token=?",
            (str(token or ""),),
        ).fetchone()


def activate_organize_confirmation_review(token: str) -> bool:
    """人工候选卡已可靠入队后，才允许后台 Agent 领取复核。"""
    timestamp = now()
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmations SET review_status='pending',updated_at=? "
            "WHERE token=? AND status='pending' AND review_status='waiting' "
            "AND expires_at>?",
            (timestamp, str(token or ""), timestamp),
        )
        return cursor.rowcount == 1


def recover_interrupted_organize_confirmation_reviews() -> int:
    """进程重启后恢复未决复核；连续中断三次后永久回退人工。"""
    timestamp = now()
    with get_conn() as conn:
        failed = conn.execute(
            "UPDATE organize_confirmations SET review_status='failed',"
            "review_result_json=?,review_completed_at=?,updated_at=? "
            "WHERE review_status='running' AND status='pending' "
            "AND review_attempts>=3",
            (
                json.dumps(
                    {
                        "reason_code": "repeated_interruption",
                        "summary": "Agent 复核连续中断，已保留人工确认",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                timestamp,
                timestamp,
            ),
        )
        recovered = conn.execute(
            "UPDATE organize_confirmations SET review_status='pending',"
            "review_started_at=NULL,updated_at=? "
            "WHERE review_status='running' AND status='pending' "
            "AND review_attempts<3",
            (timestamp,),
        )
        return int(failed.rowcount) + int(recovered.rowcount)


def claim_next_organize_confirmation_review() -> sqlite3.Row | None:
    """原子领取最早的待确认识别复核；只读模型调用不占用整理写队列。"""
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM organize_confirmations "
            "WHERE review_status='pending' AND status='pending' "
            "AND review_attempts<3 AND expires_at>? "
            "ORDER BY id ASC LIMIT 1",
            (timestamp,),
        ).fetchone()
        if row is None:
            return None
        cursor = conn.execute(
            "UPDATE organize_confirmations SET review_status='running',"
            "review_attempts=review_attempts+1,review_started_at=?,"
            "review_completed_at=NULL,updated_at=? "
            "WHERE id=? AND review_status='pending' AND status='pending'",
            (timestamp, timestamp, int(row["id"])),
        )
        if cursor.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE id=?",
            (int(row["id"]),),
        ).fetchone()


def stage_organize_confirmation_review_result(
    token: str, result: dict | None = None
) -> bool:
    """在 Agent 竞争执行所有权前保存最小审计，不改变确认状态。"""
    encoded = json.dumps(
        result or {}, ensure_ascii=False, separators=(",", ":"), default=str
    )
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmations SET review_result_json=?,updated_at=? "
            "WHERE token=? AND review_status='running' AND status='pending'",
            (encoded, now(), str(token or "")),
        )
        return cursor.rowcount == 1


def complete_organize_confirmation_review(
    token: str,
    *,
    status: str,
    result: dict | None = None,
) -> bool:
    """保存最小复核回执；模型上下文与工具原始载荷不会持久化。"""
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {
        "approved", "abstained", "failed", "cancelled"
    }:
        raise ValueError("Agent 复核状态无效")
    timestamp = now()
    encoded = json.dumps(
        result or {}, ensure_ascii=False, separators=(",", ":"), default=str
    )
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmations SET review_status=?,review_result_json=?,"
            "review_completed_at=?,updated_at=? "
            "WHERE token=? AND review_status='running'",
            (
                normalized_status,
                encoded,
                timestamp,
                timestamp,
                str(token or ""),
            ),
        )
        return cursor.rowcount == 1


def list_organize_confirmations_for_task(
    organize_task_id: str, *, chat_id: str = ""
) -> list[sqlite3.Row]:
    """返回同一整理汇总关联的全部确认记录。"""
    task_id = str(organize_task_id or "").strip()
    if not task_id:
        return []
    params: list[object] = [task_id]
    chat_clause = ""
    if str(chat_id or "").strip():
        chat_clause = " AND chat_id=?"
        params.append(str(chat_id or "").strip())
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE organize_task_id=?"
            + chat_clause
            + " ORDER BY id ASC",
            params,
        ).fetchall()


def list_unapplied_organize_confirmation_tasks(limit: int = 50) -> list[sqlite3.Row]:
    """列出有终态但尚未回写原汇总的父任务，供启动恢复与定时收口。"""
    resolved_limit = max(1, min(int(limit or 50), 500))
    with get_conn() as conn:
        return conn.execute(
            "SELECT organize_task_id,chat_id,MIN(id) AS first_id "
            "FROM organize_confirmations WHERE organize_task_id<>'' "
            "AND rollup_applied=0 AND status IN ('completed','failed','expired','cancelled') "
            "AND CASE WHEN json_valid(payload_json) THEN json_type(payload_json,'$.organize_rollup')='object' ELSE 0 END "
            "GROUP BY organize_task_id,chat_id ORDER BY first_id ASC LIMIT ?",
            (resolved_limit,),
        ).fetchall()


def mark_organize_confirmation_rollup_applied(
    organize_task_id: str, *, chat_id: str = ""
) -> int:
    """标记父任务当前全部确认记录已经投影到整理汇总。"""
    task_id = str(organize_task_id or "").strip()
    if not task_id:
        return 0
    params: list[object] = [now(), task_id]
    chat_clause = ""
    if str(chat_id or "").strip():
        chat_clause = " AND chat_id=?"
        params.append(str(chat_id or "").strip())
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmations SET rollup_applied=1,updated_at=? "
            "WHERE organize_task_id=?" + chat_clause,
            params,
        )
        return int(cursor.rowcount)


def bind_organize_confirmation_message(
    token: str, *, chat_id: str, message_id: int | str
) -> None:
    """把确认卡位置写入已有 payload，供后台任务可靠更新同一条消息。"""
    try:
        resolved_message_id = int(message_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("确认消息参数无效") from exc
    if resolved_message_id <= 0:
        raise ValueError("确认消息参数无效")
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT chat_id,payload_json FROM organize_confirmations WHERE token=?",
            (str(token or ""),),
        ).fetchone()
        if row is None:
            raise ValueError("确认操作不存在或已失效")
        expected_chat = str(row["chat_id"] or "")
        if expected_chat and expected_chat != str(chat_id or ""):
            raise ValueError("确认操作不存在或已失效")
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("确认任务数据损坏，请重新执行整理") from exc
        if not isinstance(payload, dict):
            raise ValueError("确认任务数据损坏，请重新执行整理")
        payload["_telegram_message_id"] = resolved_message_id
        conn.execute(
            "UPDATE organize_confirmations SET payload_json=?,updated_at=? WHERE token=?",
            (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                now(),
                str(token or ""),
            ),
        )


def claim_organize_confirmation(
    token: str, *, chat_id: str, selected_index: int, actor: str = "human"
) -> sqlite3.Row:
    """原子认领待确认按钮；过期、越权和重放统一拒绝。"""
    normalized_actor = str(actor or "human").strip().lower()
    if normalized_actor not in {"human", "agent"}:
        raise ValueError("确认执行者无效")
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # 入队顺序必须反映按钮实际点击顺序。全局 now() 只精确到秒，
        # 连续点击会退化成记录创建顺序，因此仅为队列时间保留微秒。
        queued_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        row = conn.execute(
            "SELECT * FROM organize_confirmations WHERE token=?",
            (str(token or ""),),
        ).fetchone()
        if row is None:
            raise ValueError("确认操作不存在或已失效")
        expected_chat = str(row["chat_id"] or "")
        if expected_chat and expected_chat != str(chat_id or ""):
            raise ValueError("确认操作不存在或已失效")
        if str(row["status"] or "") != "pending":
            raise ValueError("该确认操作已处理")
        expired = str(row["expires_at"] or "") <= timestamp
        if expired:
            conn.execute(
                "UPDATE organize_confirmations SET status='expired',selected_index=NULL,"
                "queued_at=NULL,task_id='',result_json=?,error='确认操作已过期',"
                "review_status=CASE WHEN review_status IN ('waiting','pending','running') "
                "THEN 'cancelled' ELSE review_status END,"
                "review_completed_at=CASE WHEN review_status IN ('waiting','pending','running') "
                "THEN ? ELSE review_completed_at END,"
                "completed_at=?,rollup_applied=0,updated_at=? WHERE id=?",
                (
                    json.dumps({"resolution": "expired"}, ensure_ascii=False),
                    timestamp,
                    timestamp,
                    timestamp,
                    int(row["id"]),
                ),
            )
            claimed = None
        else:
            cursor = conn.execute(
                "UPDATE organize_confirmations SET status='queued',selected_index=?,"
                "queued_at=?,confirmation_actor=?,"
                "review_status=CASE "
                "WHEN ?='agent' THEN 'approved' "
                "WHEN ?='human' AND review_status IN ('waiting','pending','running') THEN 'cancelled' "
                "ELSE review_status END,"
                "review_completed_at=CASE "
                "WHEN ?='agent' THEN ? "
                "WHEN ?='human' AND review_status IN ('waiting','pending','running') THEN ? "
                "ELSE review_completed_at END,updated_at=? "
                "WHERE id=? AND status='pending' "
                "AND (?<>'agent' OR review_status='running')",
                (
                    int(selected_index), queued_at, normalized_actor,
                    normalized_actor, normalized_actor,
                    normalized_actor, timestamp,
                    normalized_actor, timestamp,
                    timestamp, int(row["id"]), normalized_actor,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("该确认操作已处理")
            claimed = conn.execute(
                "SELECT * FROM organize_confirmations WHERE id=?", (int(row["id"]),)
            ).fetchone()
    if expired:
        raise ValueError("确认操作已过期，请重新执行整理")
    return claimed


def cancel_organize_confirmation(
    token: str,
    *,
    chat_id: str,
    event_json: str = "",
    message_id: int | None = None,
    enqueue_delivery: bool = True,
    resolution: str = "cancelled",
) -> sqlite3.Row:
    """原子取消待确认任务，并可在同一事务写入终态回执。"""
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM organize_confirmations WHERE token=?",
            (str(token or ""),),
        ).fetchone()
        if row is None:
            raise ValueError("确认操作不存在或已失效")
        expected_chat = str(row["chat_id"] or "")
        if expected_chat and expected_chat != str(chat_id or ""):
            raise ValueError("确认操作不存在或已失效")
        if str(row["status"] or "") != "pending":
            raise ValueError("该确认操作已处理")
        if str(row["expires_at"] or "") <= timestamp:
            conn.execute(
                "UPDATE organize_confirmations SET status='expired',selected_index=NULL,"
                "queued_at=NULL,task_id='',result_json=?,error='确认操作已过期',"
                "review_status=CASE WHEN review_status IN ('waiting','pending','running') "
                "THEN 'cancelled' ELSE review_status END,"
                "review_completed_at=CASE WHEN review_status IN ('waiting','pending','running') "
                "THEN ? ELSE review_completed_at END,"
                "completed_at=?,rollup_applied=0,updated_at=? WHERE id=?",
                (
                    json.dumps({"resolution": "expired"}, ensure_ascii=False),
                    timestamp,
                    timestamp,
                    timestamp,
                    int(row["id"]),
                ),
            )
            cancelled = None
        else:
            cursor = conn.execute(
                "UPDATE organize_confirmations SET status='cancelled',selected_index=NULL,"
                "queued_at=NULL,task_id='',result_json=?,error='',"
                "review_status=CASE WHEN review_status IN ('waiting','pending','running') "
                "THEN 'cancelled' ELSE review_status END,"
                "review_completed_at=CASE WHEN review_status IN ('waiting','pending','running') "
                "THEN ? ELSE review_completed_at END,"
                "completed_at=?,updated_at=? "
                "WHERE id=? AND status='pending'",
                (
                    json.dumps(
                        {
                            "cancelled": True,
                            "resolution": str(resolution or "cancelled"),
                        },
                        ensure_ascii=False,
                    ),
                    timestamp,
                    timestamp,
                    timestamp,
                    int(row["id"]),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("该确认操作已处理")
            cancelled = conn.execute(
                "SELECT * FROM organize_confirmations WHERE id=?", (int(row["id"]),)
            ).fetchone()
            if enqueue_delivery and str(event_json or ""):
                telegram_notifications._enqueue_confirmation_delivery(
                    conn,
                    token=token,
                    event_json=event_json,
                    chat_id=expected_chat or str(chat_id or ""),
                    message_id=message_id,
                    timestamp=timestamp,
                )
    if cancelled is None:
        raise ValueError("确认操作已过期，请重新执行整理")
    return cancelled


def list_due_pending_organize_confirmations(
    *, token: str = "", limit: int = 100
) -> list[sqlite3.Row]:
    """列出到期但尚未选择的候选；终态与回执由单条原子接口提交。"""
    timestamp = now()
    resolved_limit = max(1, min(int(limit or 100), 500))
    with get_conn() as conn:
        params: list[object] = [timestamp]
        token_clause = ""
        if str(token or "").strip():
            token_clause = " AND token=?"
            params.append(str(token or "").strip())
        params.append(resolved_limit)
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE status='pending' "
            "AND expires_at<=?"
            + token_clause
            + " ORDER BY expires_at ASC,id ASC LIMIT ?",
            params,
        ).fetchall()


def expire_organize_confirmation_with_delivery(
    token: str,
    *,
    event_json: str,
    chat_id: str,
    message_id: int | None,
    enqueue_delivery: bool = True,
) -> sqlite3.Row | None:
    """原子失效一张到期候选卡，并写入统一通知中心的事务桥。"""
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE organize_confirmations SET status='expired',selected_index=NULL,"
            "queued_at=NULL,task_id='',result_json=?,error='确认操作已过期',"
            "review_status=CASE WHEN review_status IN ('waiting','pending','running') "
            "THEN 'cancelled' ELSE review_status END,"
            "review_completed_at=CASE WHEN review_status IN ('waiting','pending','running') "
            "THEN ? ELSE review_completed_at END,"
            "completed_at=?,rollup_applied=0,updated_at=? "
            "WHERE token=? AND status='pending' AND expires_at<=?",
            (
                json.dumps({"resolution": "expired"}, ensure_ascii=False),
                timestamp,
                timestamp,
                timestamp,
                str(token or ""),
                timestamp,
            ),
        )
        if cursor.rowcount != 1:
            return None
        if enqueue_delivery:
            telegram_notifications._enqueue_confirmation_delivery(
                conn,
                token=token,
                event_json=event_json,
                chat_id=chat_id,
                message_id=message_id,
                timestamp=timestamp,
            )
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE token=?",
            (str(token or ""),),
        ).fetchone()


def get_next_queued_organize_confirmation() -> sqlite3.Row | None:
    """返回最早的已确认 Telegram 整理任务；入队后不再按候选 TTL 失效。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE status='queued' "
            "ORDER BY COALESCE(queued_at,created_at) ASC,id ASC LIMIT 1"
        ).fetchone()


def get_organize_confirmation_queue_position(row_id: int) -> int:
    """按用户点击入队时间返回前方任务数量，并计入当前运行项。"""
    with get_conn() as conn:
        target = conn.execute(
            "SELECT id,COALESCE(queued_at,created_at) AS queue_time "
            "FROM organize_confirmations WHERE id=?",
            (int(row_id),),
        ).fetchone()
        if target is None:
            return 0
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM organize_confirmations "
            "WHERE id<>? AND (status='running' OR (status='queued' AND ("
            "COALESCE(queued_at,created_at)<? OR "
            "(COALESCE(queued_at,created_at)=? AND id<?))))",
            (
                int(row_id),
                str(target["queue_time"] or ""),
                str(target["queue_time"] or ""),
                int(row_id),
            ),
        ).fetchone()
    return int(row["total"] or 0) if row is not None else 0


def claim_queued_organize_confirmation(token: str) -> sqlite3.Row | None:
    """原子领取一个已排队确认；已选择任务不再因等待跨过 TTL 而失效。"""
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE organize_confirmations SET status='running',error='',updated_at=? "
            "WHERE token=? AND status='queued'",
            (timestamp, str(token or "")),
        )
        if cursor.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE token=?",
            (str(token or ""),),
        ).fetchone()


def requeue_organize_confirmation(token: str, error: str = "") -> None:
    """统一整理锁繁忙时保留用户选择并放回 FIFO 队列。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE organize_confirmations SET status='queued',error=?,updated_at=? "
            "WHERE token=? AND status='running'",
            (str(error or ""), now(), str(token or "")),
        )


def update_organize_confirmation(token: str, **fields) -> None:
    allowed = {
        "status", "task_id", "result_json", "error", "completed_at",
        "review_status", "review_result_json", "review_started_at",
        "review_completed_at", "confirmation_actor",
    }
    sets: list[str] = []
    values: list[object] = []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key}=?")
            values.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    values.extend([now(), str(token or "")])
    with get_conn() as conn:
        conn.execute(
            f"UPDATE organize_confirmations SET {','.join(sets)} WHERE token=?",
            values,
        )


def release_organize_confirmation(
    token: str, error: str = "", *, include_running: bool = False
) -> None:
    """释放未完成认领，允许同一安全快照稍后再次点击。"""
    statuses = ("queued", "running") if include_running else ("queued",)
    placeholders = ",".join("?" for _ in statuses)
    with get_conn() as conn:
        conn.execute(
            "UPDATE organize_confirmations SET status='pending',selected_index=NULL,"
            f"queued_at=NULL,task_id='',error=?,completed_at=NULL,updated_at=? "
            f"WHERE token=? AND status IN ({placeholders})",
            (str(error or ""), now(), str(token or ""), *statuses),
        )


def complete_organize_confirmation_with_delivery(
    token: str,
    *,
    result_json: str,
    event_json: str,
    chat_id: str,
    message_id: int | None,
    enqueue_delivery: bool = True,
) -> None:
    """原子保存成功终态、下载收尾意图与可选回执。"""
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE organize_confirmations SET status='completed',result_json=?,error='',"
            "completed_at=?,rollup_applied=0,updated_at=? "
            "WHERE token=? AND status='running'",
            (str(result_json or "{}"), timestamp, timestamp, str(token or "")),
        )
        if cursor.rowcount != 1:
            raise ValueError("确认操作不存在或已失效")
        # 收尾意图与确认终态同事务落盘；即使回执静默或随后进程中断，
        # 下载隔离目录也能由后台安全恢复，而不是永久留下旧待处理状态。
        from app.repositories.download_staging_reconcile import enqueue_confirmation_cleanup

        enqueue_confirmation_cleanup(conn, token=token, timestamp=timestamp)
        if enqueue_delivery:
            telegram_notifications._enqueue_confirmation_delivery(
                conn,
                token=token,
                event_json=event_json,
                chat_id=chat_id,
                message_id=message_id,
                timestamp=timestamp,
            )


def fail_organize_confirmation_with_delivery(
    token: str,
    *,
    error: str,
    event_json: str,
    chat_id: str,
    message_id: int | None,
    retryable: bool,
    enqueue_delivery: bool = True,
    retry_expires_at: str = "",
) -> None:
    """原子保存失败/可重试状态与待投递回执。"""
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if retryable:
            cursor = conn.execute(
                "UPDATE organize_confirmations SET status='pending',selected_index=NULL,"
                "queued_at=NULL,task_id='',error=?,completed_at=NULL,rollup_applied=0,"
                "expires_at=CASE WHEN ?<>'' THEN ? ELSE expires_at END,updated_at=? "
                "WHERE token=? AND status IN ('queued','running')",
                (
                    str(error or ""),
                    str(retry_expires_at or ""),
                    str(retry_expires_at or ""),
                    timestamp,
                    str(token or ""),
                ),
            )
        else:
            cursor = conn.execute(
                "UPDATE organize_confirmations SET status='failed',error=?,completed_at=?,"
                "rollup_applied=0,updated_at=? "
                "WHERE token=? AND status IN ('queued','running')",
                (str(error or ""), timestamp, timestamp, str(token or "")),
            )
        if cursor.rowcount != 1:
            raise ValueError("确认操作不存在或已失效")
        if enqueue_delivery:
            telegram_notifications._enqueue_confirmation_delivery(
                conn,
                token=token,
                event_json=event_json,
                chat_id=chat_id,
                message_id=message_id,
                timestamp=timestamp,
            )


def claim_due_organize_confirmation_delivery(
    *,
    current_time: str,
    stale_before: str,
    token: str = "",
) -> sqlite3.Row | None:
    """原子领取一条到期回执；租约代数防止迟到 worker 覆盖新结果。"""
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        params: list[object] = [str(current_time), str(stale_before)]
        token_clause = ""
        if str(token or ""):
            token_clause = " AND confirmation_token=?"
            params.append(str(token))
        row = conn.execute(
            "SELECT * FROM organize_confirmation_delivery_outbox WHERE (("
            "status IN ('pending','retry_wait') AND next_attempt_at<=?) OR "
            "(status='sending' AND updated_at<=?))"
            + token_clause
            + " ORDER BY next_attempt_at ASC,id ASC LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return None
        cursor = conn.execute(
            "UPDATE organize_confirmation_delivery_outbox SET status='sending',"
            "lease_generation=lease_generation+1,updated_at=? WHERE id=? AND "
            "lease_generation=? AND status=?",
            (
                str(current_time),
                int(row["id"]),
                int(row["lease_generation"]),
                str(row["status"]),
            ),
        )
        if cursor.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM organize_confirmation_delivery_outbox WHERE id=?",
            (int(row["id"]),),
        ).fetchone()


def complete_organize_confirmation_delivery(
    delivery_id: int, *, expected_lease_generation: int, sent_at: str
) -> bool:
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmation_delivery_outbox SET status='sent',sent_at=?,"
            "last_error='',updated_at=? WHERE id=? AND status='sending' AND lease_generation=?",
            (
                str(sent_at),
                str(sent_at),
                int(delivery_id),
                int(expected_lease_generation),
            ),
        )
        return cursor.rowcount == 1


def retry_organize_confirmation_delivery(
    delivery_id: int,
    *,
    expected_lease_generation: int,
    next_attempt_at: str,
    error: str,
) -> bool:
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmation_delivery_outbox SET status='retry_wait',"
            "attempts=attempts+1,next_attempt_at=?,last_error=?,updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                str(next_attempt_at),
                str(error or "DeliveryFailed")[:500],
                now(),
                int(delivery_id),
                int(expected_lease_generation),
            ),
        )
        return cursor.rowcount == 1


def get_organize_confirmation_delivery(token: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_confirmation_delivery_outbox WHERE confirmation_token=?",
            (str(token or ""),),
        ).fetchone()


# ===== GCID 导入任务 =====
_GCID_IMPORT_TASK_STATUSES = {
    "previewed",
    "running",
    "success",
    "partial_success",
    "failed",
}
_GCID_IMPORT_ITEM_STATUSES = {"previewed", "running", "success", "failed"}


def create_gcid_import_task(
    *,
    operation_token: str,
    manifest_digest: str,
    target_dir_id: str,
    file_count: int,
    total_size: int,
) -> int:
    token = str(operation_token or "").strip()
    digest = str(manifest_digest or "").strip().lower()
    target = str(target_dir_id or "").strip()
    if not token or len(token) > 256:
        raise ValueError("operation_token 无效")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("manifest_digest 无效")
    if not target or len(target) > 256:
        raise ValueError("target_dir_id 无效")
    count = int(file_count)
    size = int(total_size)
    if count < 0 or size < 0:
        raise ValueError("文件统计不能为负数")
    timestamp = now()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO gcid_import_tasks("
            "operation_token,manifest_digest,target_dir_id,status,file_count,total_size,"
            "success_count,failed_count,error,created_at,updated_at"
            ") VALUES(?,?,?,'previewed',?,?,0,0,'',?,?)",
            (token, digest, target, count, size, timestamp, timestamp),
        )
        row = conn.execute(
            "SELECT * FROM gcid_import_tasks WHERE operation_token=?", (token,)
        ).fetchone()
        if row is None:
            raise RuntimeError("GCID 导入任务创建失败")
        if (
            row["manifest_digest"] != digest
            or row["target_dir_id"] != target
            or int(row["file_count"] or 0) != count
            or int(row["total_size"] or 0) != size
        ):
            raise ValueError("operation_token 已绑定其他 GCID 导入任务")
        return int(row["id"])


def replace_gcid_import_items(task_id: int, items: list[dict]) -> None:
    if not isinstance(items, list) or len(items) > 10_000:
        raise ValueError("GCID 导入明细必须是最多 10000 项的数组")
    normalized: list[tuple] = []
    seen: set[str] = set()
    timestamp = now()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("GCID 导入明细格式无效")
        path = str(item.get("path") or "").strip()
        gcid = str(item.get("gcid") or "").strip()
        status = str(item.get("status") or "previewed").strip()
        size = int(item.get("size") or 0)
        if not path or path.casefold() in seen or not gcid or size < 0:
            raise ValueError("GCID 导入明细字段无效或路径重复")
        if status not in _GCID_IMPORT_ITEM_STATUSES:
            raise ValueError("GCID 导入明细状态无效")
        seen.add(path.casefold())
        normalized.append(
            (
                int(task_id),
                path,
                size,
                gcid,
                status,
                str(item.get("remote_file_id") or "").strip(),
                str(item.get("error") or "")[:1000],
                timestamp,
                timestamp,
            )
        )
    with get_conn() as conn:
        if (
            conn.execute(
                "SELECT 1 FROM gcid_import_tasks WHERE id=?", (int(task_id),)
            ).fetchone()
            is None
        ):
            raise ValueError("GCID 导入任务不存在")
        conn.execute("DELETE FROM gcid_import_items WHERE task_id=?", (int(task_id),))
        conn.executemany(
            "INSERT INTO gcid_import_items("
            "task_id,path,size,gcid,status,remote_file_id,error,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            normalized,
        )


def update_gcid_import_task(
    task_id: int,
    *,
    status: str | None = None,
    success_count: int | None = None,
    failed_count: int | None = None,
    error: str | None = None,
) -> None:
    updates: list[str] = []
    params: list = []
    if status is not None:
        normalized_status = str(status).strip()
        if normalized_status not in _GCID_IMPORT_TASK_STATUSES:
            raise ValueError("GCID 导入任务状态无效")
        updates.append("status=?")
        params.append(normalized_status)
    for key, value in (
        ("success_count", success_count),
        ("failed_count", failed_count),
    ):
        if value is not None:
            normalized_count = int(value)
            if normalized_count < 0:
                raise ValueError("GCID 导入任务计数不能为负数")
            updates.append(f"{key}=?")
            params.append(normalized_count)
    if error is not None:
        updates.append("error=?")
        params.append(str(error)[:1000])
    if not updates:
        return
    updates.append("updated_at=?")
    params.append(now())
    params.append(int(task_id))
    with get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE gcid_import_tasks SET {', '.join(updates)} WHERE id=?", params
        )
        if cursor.rowcount != 1:
            raise ValueError("GCID 导入任务不存在")


def get_gcid_import_task(task_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM gcid_import_tasks WHERE id=?", (int(task_id),)
        ).fetchone()


def list_gcid_import_tasks(limit: int = 30) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM gcid_import_tasks ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit or 30), 200)),),
        ).fetchall()


def list_gcid_import_items(
    task_id: int, status: str = "", *, limit: int | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM gcid_import_items WHERE task_id=?"
    params: list = [int(task_id)]
    if status:
        if status not in _GCID_IMPORT_ITEM_STATUSES:
            raise ValueError("GCID 导入明细状态无效")
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(0, int(limit)))
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


# ===== 后台任务运行记录 =====
def add_task_run(
    task_name: str, trigger_type: str, status: str = "running", result: str = ""
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO task_runs(task_name,trigger_type,status,started_at,result) "
            "VALUES(?,?,?,?,?)",
            (task_name, trigger_type, status, now(), result),
        )
        return cur.lastrowid


def finish_task_run(
    run_id: int, status: str, result: str = "", error: str = ""
) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE task_runs SET status=?,finished_at=?,result=?,error=? WHERE id=?",
            (status, now(), result, error, run_id),
        )


def list_task_runs(task_name: str = "", limit: int = 20) -> list[sqlite3.Row]:
    sql = "SELECT * FROM task_runs WHERE 1=1"
    params: list = []
    if task_name:
        sql += " AND task_name=?"
        params.append(task_name)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


# ===== Agent 受确认动作审计 =====
_AGENT_ACTION_HISTORY_PER_OWNER_LIMIT = 2000
_AGENT_ACTION_HISTORY_GLOBAL_LIMIT = 20_000


def add_agent_action_history(
    *,
    owner_digest: str,
    tool_name: str,
    risk: str,
    status: str,
    ok: bool,
    summary: str,
    safe_details: dict | None = None,
    error_code: str = "",
    elapsed_ms: int = 0,
    started_at: str = "",
    finished_at: str = "",
    confirmation_id: str = "",
    finalize_only: bool = False,
    connection: sqlite3.Connection | None = None,
) -> int:
    """写入一条脱敏 Agent 动作审计；调用方只能传入安全投影。"""
    normalized_owner = str(owner_digest or "").strip().lower()
    normalized_tool = str(tool_name or "").strip()
    normalized_risk = str(risk or "").strip()
    normalized_status = str(status or "").strip()
    normalized_summary = str(summary or "").strip()
    normalized_error = str(error_code or "").strip()
    normalized_confirmation = str(confirmation_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_owner):
        raise ValueError("Agent 审计身份摘要无效")
    if not normalized_tool or len(normalized_tool) > 128:
        raise ValueError("Agent 审计工具名无效")
    if normalized_risk not in {"low_write", "write", "danger"}:
        raise ValueError("Agent 审计风险等级无效")
    if not normalized_status or len(normalized_status) > 64:
        raise ValueError("Agent 审计状态无效")
    if not normalized_summary or len(normalized_summary) > 240:
        raise ValueError("Agent 审计摘要无效")
    if normalized_error and (
        len(normalized_error) > 64 or not re.fullmatch(r"[a-z0-9_]+", normalized_error)
    ):
        raise ValueError("Agent 审计错误码无效")
    if normalized_confirmation and (
        len(normalized_confirmation) > 128
        or not re.fullmatch(r"[A-Za-z0-9_-]+", normalized_confirmation)
    ):
        raise ValueError("Agent 审计确认标识无效")
    if finalize_only and not normalized_confirmation:
        raise ValueError("Agent 审计终态更新缺少确认标识")
    details = safe_details or {}
    if not isinstance(details, dict):
        raise ValueError("Agent 审计详情必须是对象")
    for key, value in details.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            raise ValueError("Agent 审计详情字段无效")
        if value is not None and not isinstance(value, (bool, int, float, str)):
            raise ValueError("Agent 审计详情只允许标量")
        if isinstance(value, str) and len(value) > 128:
            raise ValueError("Agent 审计详情文本过长")
    encoded = json.dumps(
        details, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(encoded.encode("utf-8")) > 4096:
        raise ValueError("Agent 审计详情过大")
    elapsed = max(0, min(int(elapsed_ms or 0), 86_400_000))
    finished = str(finished_at or now()).strip()
    started = str(started_at or finished).strip()

    def write(conn: sqlite3.Connection) -> int:
        history_id = 0
        if normalized_confirmation:
            existing = conn.execute(
                "SELECT id FROM agent_action_history WHERE confirmation_id=?",
                (normalized_confirmation,),
            ).fetchone()
            if existing is not None:
                history_id = int(existing["id"])
                conn.execute(
                    "UPDATE agent_action_history SET owner_digest=?,tool_name=?,risk=?,"
                    "status=?,ok=?,mode='confirmed_action',summary=?,safe_details=?,"
                    "error_code=?,elapsed_ms=?,started_at=?,finished_at=? WHERE id=?",
                    (
                        normalized_owner,
                        normalized_tool,
                        normalized_risk,
                        normalized_status,
                        1 if ok else 0,
                        normalized_summary,
                        encoded,
                        normalized_error,
                        elapsed,
                        started,
                        finished,
                        history_id,
                    ),
                )
            elif finalize_only:
                # 隐私清理可能已删除 executing 行；迟到终态只能更新已有记录，
                # 不能在清理成功后重新插入主体数据。
                return 0
        if history_id <= 0:
            cursor = conn.execute(
                "INSERT INTO agent_action_history("
                "confirmation_id,owner_digest,tool_name,risk,status,ok,mode,summary,"
                "safe_details,error_code,elapsed_ms,started_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    normalized_confirmation,
                    normalized_owner,
                    normalized_tool,
                    normalized_risk,
                    normalized_status,
                    1 if ok else 0,
                    "confirmed_action",
                    normalized_summary,
                    encoded,
                    normalized_error,
                    elapsed,
                    started,
                    finished,
                ),
            )
            history_id = int(cursor.lastrowid)
        conn.execute(
            "DELETE FROM agent_action_history WHERE owner_digest=? AND id NOT IN ("
            "SELECT id FROM agent_action_history WHERE owner_digest=? "
            "ORDER BY id DESC LIMIT ?"
            ")",
            (
                normalized_owner,
                normalized_owner,
                _AGENT_ACTION_HISTORY_PER_OWNER_LIMIT,
            ),
        )
        total = int(
            conn.execute("SELECT COUNT(*) FROM agent_action_history").fetchone()[0] or 0
        )
        if total > _AGENT_ACTION_HISTORY_GLOBAL_LIMIT:
            # 一次收敛到全局容量：先按 owner 内新旧排序，再按轮次公平保留。
            # 这样会优先保留每个 owner 的最新记录，并确保当前新审计不因
            # 旧数据库已超限而被静默丢弃。
            conn.execute(
                "DELETE FROM agent_action_history WHERE id NOT IN ("
                "SELECT id FROM ("
                "SELECT id,ROW_NUMBER() OVER ("
                "PARTITION BY owner_digest ORDER BY id DESC"
                ") AS owner_rank FROM agent_action_history"
                ") WHERE owner_rank<=? ORDER BY owner_rank ASC,id DESC LIMIT ?"
                ")",
                (
                    _AGENT_ACTION_HISTORY_PER_OWNER_LIMIT,
                    _AGENT_ACTION_HISTORY_GLOBAL_LIMIT,
                ),
            )
        return history_id

    if connection is not None:
        return write(connection)
    with get_conn() as conn:
        return write(conn)


def list_agent_action_history(
    *, owner_digest: str, limit: int = 20, outcome: str = "all"
) -> list[sqlite3.Row]:
    """倒序读取 Agent 动作审计；只支持固定结果分类与有界条数。"""
    normalized_owner = str(owner_digest or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_owner):
        raise ValueError("Agent 审计身份摘要无效")
    bounded_limit = int(limit)
    if bounded_limit < 1 or bounded_limit > 50:
        raise ValueError("Agent 审计查询条数必须为 1 到 50")
    normalized_outcome = str(outcome or "all").strip().lower()
    if normalized_outcome not in {"all", "success", "failed"}:
        raise ValueError("Agent 审计结果筛选无效")
    sql = (
        "SELECT tool_name,risk,status,ok,mode,summary,safe_details,error_code,"
        "elapsed_ms,started_at,finished_at FROM agent_action_history "
        "WHERE owner_digest=?"
    )
    params: list = [normalized_owner]
    if normalized_outcome == "success":
        sql += " AND ok=1"
    elif normalized_outcome == "failed":
        sql += " AND ok=0 AND status NOT IN ('executing','outcome_unknown')"
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(bounded_limit)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def get_task_run(run_id: int) -> sqlite3.Row | None:
    """按运行记录 ID 精确读取后台任务，供跨进程恢复关联。"""
    try:
        normalized_id = int(run_id or 0)
    except (TypeError, ValueError):
        return None
    if normalized_id <= 0:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM task_runs WHERE id=?",
            (normalized_id,),
        ).fetchone()


def get_last_task_run(task_name: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM task_runs WHERE task_name=? ORDER BY id DESC LIMIT 1",
            (task_name,),
        ).fetchone()


def get_dashboard_automation_summary() -> dict:
    """返回看板所需的本地自动化状态，不触发任何第三方请求。"""
    with get_conn() as conn:
        downloads_active = int(
            conn.execute(
                "SELECT COUNT(*) FROM download_requests WHERE "
                "(status IN ('submitting','submitted','downloading') OR "
                "(status='completed' AND gy_status='completed' AND organize_started=0)) AND "
                "COALESCE(qb_status,'') NOT IN ('failed','manual_review') AND "
                "COALESCE(gy_status,'') NOT IN ('failed','manual_review') AND "
                "COALESCE(local_import_status,'')!='failed' AND COALESCE(organize_started,0)>=0"
            ).fetchone()[0]
        )
        downloads_review = int(
            conn.execute(
                f"SELECT COUNT(*) FROM download_requests WHERE {_DOWNLOAD_ATTENTION_WHERE}"
            ).fetchone()[0]
        )
        rss_subscriptions = int(
            conn.execute("SELECT COUNT(*) FROM rss_items WHERE enabled=1").fetchone()[0]
        )
        # 看板「订阅」是全站订阅总量：RSS 订阅源与媒体追更订阅都要计入，
        # 否则只做媒体追更的用户会在顶栏看到 0。
        media_subscriptions = int(
            conn.execute(
                "SELECT COUNT(*) FROM media_subscriptions WHERE enabled=1"
            ).fetchone()[0]
        )
        rss_row = conn.execute(
            "SELECT "
            "SUM(CASE WHEN status IN ('pending','submitting') THEN 1 ELSE 0 END) AS pending,"
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
            "FROM rss_entries"
        ).fetchone()
        organize_issues = int(
            conn.execute(
                "SELECT COUNT(*) FROM organize_log WHERE "
                "status IN ('failed','interrupted','partial_failed','revert_failed')"
            ).fetchone()[0]
        )
        strm_failures = int(
            conn.execute(
                "SELECT COUNT(*) FROM strm_failures WHERE status='open'"
            ).fetchone()[0]
        )
        last_strm = conn.execute(
            "SELECT status,COALESCE(finished_at,started_at,'') AS happened_at "
            "FROM task_runs WHERE task_name='strm_sync' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "downloads_active": downloads_active,
        "downloads_review": downloads_review,
        "rss_subscriptions": rss_subscriptions,
        "media_subscriptions": media_subscriptions,
        "subscriptions_total": rss_subscriptions + media_subscriptions,
        "rss_pending": int((rss_row["pending"] if rss_row else 0) or 0),
        "rss_failed": int((rss_row["failed"] if rss_row else 0) or 0),
        "organize_issues": organize_issues,
        "strm_failures": strm_failures,
        "strm_last_status": str(last_strm["status"] or "") if last_strm else "",
        "strm_last_at": str(last_strm["happened_at"] or "") if last_strm else "",
    }


def get_agent_persistent_health_summary() -> dict[str, dict[str, object]]:
    """返回 Agent 持久自动化的匿名健康汇总，不读取标题、路径或错误正文。"""
    with get_conn() as conn:
        verification_row = conn.execute(
            "SELECT COUNT(*) AS total,"
            "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,"
            "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,"
            "SUM(CASE WHEN status='retry_wait' THEN 1 ELSE 0 END) AS retry_wait,"
            "SUM(CASE WHEN status='visible' THEN 1 ELSE 0 END) AS visible,"
            "SUM(CASE WHEN status='attention' THEN 1 ELSE 0 END) AS attention "
            "FROM agent_download_verifications"
        ).fetchone()
        patrol_row = conn.execute(
            "SELECT status,outcome,checked_series_count,updates_available_count,"
            "missing_episode_count,"
            "inconclusive_count,unmapped_series_count,findings_truncated "
            "FROM agent_library_patrol WHERE patrol_key='default'"
        ).fetchone()

    verification_keys = (
        "total",
        "pending",
        "running",
        "retry_wait",
        "visible",
        "attention",
    )
    verification = {
        key: max(0, int((verification_row[key] if verification_row else 0) or 0))
        for key in verification_keys
    }
    if patrol_row is None:
        patrol: dict[str, object] = {
            "status": "not_created",
            "outcome": "",
            "checked_series_count": 0,
            "updates_available_count": 0,
            "missing_episode_count": 0,
            "inconclusive_count": 0,
            "unmapped_series_count": 0,
            "findings_truncated": False,
        }
    else:
        patrol = {
            "status": str(patrol_row["status"] or ""),
            "outcome": str(patrol_row["outcome"] or ""),
            "checked_series_count": max(
                0, int(patrol_row["checked_series_count"] or 0)
            ),
            "updates_available_count": max(
                0, int(patrol_row["updates_available_count"] or 0)
            ),
            "missing_episode_count": max(
                0, int(patrol_row["missing_episode_count"] or 0)
            ),
            "inconclusive_count": max(0, int(patrol_row["inconclusive_count"] or 0)),
            "unmapped_series_count": max(
                0, int(patrol_row["unmapped_series_count"] or 0)
            ),
            "findings_truncated": bool(patrol_row["findings_truncated"]),
        }
    return {"download_verification": verification, "library_patrol": patrol}


# ===== 媒体探索缓存、跨来源映射与收藏 =====
# 统一数据访问门面：外部调用与测试共享按业务域拆分的单一实现。
from app.repositories.discovery import (  # noqa: E402,F401
    add_media_watchlist,
    confirm_media_external_id_if_unchanged,
    delete_media_watchlist,
    get_discovery_cache,
    get_discovery_cache_many,
    get_media_external_id,
    get_media_watchlist,
    get_media_watchlist_by_id,
    list_media_external_ids,
    list_media_watchlist,
    list_media_watchlist_keys,
    purge_discovery_cache,
    update_discovery_cache_error,
    upsert_discovery_cache,
    upsert_media_external_id,
)


# ===== 本地媒体自动整理 =====
from app.repositories.local_media import (  # noqa: E402,F401
    _LOCAL_MEDIA_TERMINAL_TASK_STATUSES,
    _local_media_owner,
    _canonical_local_media_content_path,
    _active_local_media_task_for_path,
    _local_media_source_has_active_task,
    _local_media_target_signature,
    _latest_terminal_local_media_task_for_path,
    _normalize_local_media_task_path,
    _bind_qb_hash_to_active_local_media_task,
    create_local_media_source,
    save_local_media_source_bundle,
    get_local_media_source,
    list_local_media_sources,
    upsert_local_library_target,
    list_local_library_bindings,
    list_local_library_targets,
    replace_local_library_targets,
    create_local_media_task,
    create_and_link_qb_local_media_task,
    list_download_requests_for_local_media_task,
    get_local_media_task,
    list_local_media_tasks,
    list_local_media_qb_write_conflicts,
    list_waiting_local_media_tasks,
    delete_local_media_tasks,
    get_local_media_diagnostic_summary,
    _local_media_age_bucket_sql,
    get_local_media_review_queue_summary,
    get_local_media_history_summary,
    prepare_manual_local_media_task,
    claim_local_media_task,
    claim_local_media_confirmation_task,
    update_local_media_task,
    reconcile_local_media_downloads,
    add_local_media_task_item,
    list_local_media_task_items,
    add_local_media_operation_step,
    update_local_media_operation_step,
    list_local_media_operation_steps,
    update_local_media_source,
    delete_local_media_source,
    reset_local_media_task,
    reset_local_media_task_if_current,
)


def _agent_workspace_title_pattern(query: str) -> str:
    """为 Agent 标题搜索转义 LIKE 通配符，避免把用户输入解释为查询语法。"""
    value = (
        str(query or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{value}%"


def _agent_workspace_search_rows(sql: str, query: str, limit: int) -> dict[str, object]:
    safe_limit = max(1, min(int(limit), 20))
    with get_conn() as conn:
        rows = conn.execute(
            sql, (_agent_workspace_title_pattern(query), safe_limit + 1)
        ).fetchall()
    return {
        "items": [dict(row) for row in rows[:safe_limit]],
        "truncated": len(rows) > safe_limit,
    }


def search_agent_workspace_rss(query: str, *, limit: int = 8) -> dict[str, object]:
    """仅按 RSS 标题搜索并读取公开状态字段。"""
    return _agent_workspace_search_rows(
        "SELECT title,status,processed,pub_date,created_at FROM rss_entries "
        "WHERE title LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
        query,
        limit,
    )


def search_agent_workspace_downloads(
    query: str, *, limit: int = 8
) -> dict[str, object]:
    """仅按下载标题搜索；不读取路径、任务标识、源值或错误正文。"""
    safe_limit = max(1, min(int(limit), 20))
    pattern = _agent_workspace_title_pattern(query)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT title,"
            "CASE WHEN source='qb' THEN 'qb' WHEN source='guangya' THEN 'guangya' ELSE 'download' END AS source,"
            "status,progress,created_at FROM download_log "
            "WHERE COALESCE(title,'') LIKE ? ESCAPE '\\' "
            "UNION ALL "
            "SELECT title,"
            "CASE WHEN targets='qb' THEN 'qb' WHEN targets='guangya' THEN 'guangya' "
            "WHEN targets='both' THEN 'both' ELSE 'download' END AS source,"
            "status,0 AS progress,created_at FROM download_requests "
            "WHERE COALESCE(title,'') LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC LIMIT ?",
            (pattern, pattern, safe_limit + 1),
        ).fetchall()
    return {
        "items": [dict(row) for row in rows[:safe_limit]],
        "truncated": len(rows) > safe_limit,
    }


def search_agent_workspace_organize(query: str, *, limit: int = 8) -> dict[str, object]:
    """仅按已识别标题搜索整理历史；不读取文件名、路径、ID 或错误正文。"""
    safe_limit = max(1, min(int(limit), 20))
    pattern = _agent_workspace_title_pattern(query)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT title,media_type,year,season,episode,status,created_at,"
            "CASE WHEN source='guangya' THEN 'guangya' ELSE 'organize' END AS source "
            "FROM organize_log WHERE COALESCE(title,'') LIKE ? ESCAPE '\\' "
            "OR (COALESCE(title,'')='' AND COALESCE(original_name,'') LIKE ? ESCAPE '\\') "
            "ORDER BY id DESC LIMIT ?",
            (pattern, pattern, safe_limit + 1),
        ).fetchall()
    return {
        "items": [dict(row) for row in rows[:safe_limit]],
        "truncated": len(rows) > safe_limit,
    }


def search_agent_workspace_local_media(
    query: str, *, owner: str = "admin", limit: int = 8
) -> dict[str, object]:
    """仅按本地媒体任务标题搜索安全状态字段。"""
    safe_limit = max(1, min(int(limit), 20))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT title,media_type,year,status,trigger,created_at,updated_at "
            "FROM local_media_tasks WHERE owner=? "
            "AND COALESCE(title,'') LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
            (
                _local_media_owner(owner),
                _agent_workspace_title_pattern(query),
                safe_limit + 1,
            ),
        ).fetchall()
    return {
        "items": [dict(row) for row in rows[:safe_limit]],
        "truncated": len(rows) > safe_limit,
    }


def claim_agent_action_lease(lease_key: str, *, ttl_seconds: int = 90) -> str | None:
    """跨 owner/Worker 原子领取短期外部动作去重租约。"""
    import uuid

    normalized = str(lease_key or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError("Agent 动作租约键无效")
    ttl = max(5, min(int(ttl_seconds), 300))
    current = time.time()
    token = uuid.uuid4().hex
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM agent_action_leases WHERE expires_at<=?", (current,))
        cur = conn.execute(
            "INSERT OR IGNORE INTO agent_action_leases("
            "lease_key,lease_token,expires_at,created_at) VALUES(?,?,?,?)",
            (normalized, token, current + ttl, now()),
        )
        return token if cur.rowcount == 1 else None


from app.repositories import telegram_notifications  # noqa: E402
