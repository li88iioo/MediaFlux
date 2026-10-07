"""SQLite 逐版本迁移实现及登记表。

只使用协调器传入的连接，不独立连接、提交或管理版本。savepoint、备份与
user_version 推进仍由 app.database 负责，保证整个升级可原子回滚。
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from html.parser import HTMLParser

from app.logger import get_logger

# 保留升级日志归属，便于沿用现有运维过滤规则。
logger = get_logger("app.database")


def now() -> str:
    """沿用门面的时钟，避免迁移与运行期使用两套时间来源。"""
    from app import database

    return database.now()


def _restore_interrupted_agent_session_context_v2(
    conn: sqlite3.Connection,
) -> None:
    """恢复旧版非原子 v2 迁移留下的临时表，再由正式迁移重新执行。"""
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current_version > 1:
        return
    legacy_exists = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='agent_session_context_v1'"
    ).fetchone()
    if legacy_exists is None:
        return
    legacy_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='agent_session_context_v1'"
    ).fetchone()
    legacy_sql = str(legacy_sql_row[0] or "").casefold()
    legacy_columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(agent_session_context_v1)"
        ).fetchall()
    }
    required_legacy_columns = {
        "id",
        "owner_digest",
        "context_type",
        "payload",
        "expires_at",
        "created_at",
    }
    allowed_legacy_column_sets = (
        required_legacy_columns,
        required_legacy_columns | {"context_generation"},
    )
    normalized_legacy_sql = re.sub(r"\s+", "", legacy_sql)
    legacy_indexes = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA index_list(agent_session_context_v1)"
        ).fetchall()
    }
    expected_legacy_indexes = {
        "idx_agent_session_context_lookup",
        "idx_agent_session_context_expiry",
    }
    legacy_trigger = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name='agent_session_context_v1' LIMIT 1"
    ).fetchone()
    if (
        "check(context_typein('patrol','download_submission'))"
        not in normalized_legacy_sql
        or legacy_columns not in allowed_legacy_column_sets
        or legacy_indexes != expected_legacy_indexes
        or legacy_trigger is not None
    ):
        raise RuntimeError(
            "检测到无法确认来源的 agent_session_context_v1，"
            "已拒绝自动恢复；请使用迁移前备份恢复数据库"
        )
    current_exists = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='agent_session_context'"
    ).fetchone()
    if current_exists is not None:
        current_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='agent_session_context'"
        ).fetchone()
        current_sql = str(current_sql_row[0] or "").casefold()
        current_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(agent_session_context)"
            ).fetchall()
        }
        expected_current_columns = required_legacy_columns | {"context_generation"}
        current_indexes = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA index_list(agent_session_context)"
            ).fetchall()
        }
        current_trigger = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='agent_session_context' LIMIT 1"
        ).fetchone()
        if (
            "check" in current_sql
            or current_columns != expected_current_columns
            or current_indexes
            or current_trigger is not None
        ):
            raise RuntimeError(
                "检测到无法确认的 Agent 会话上下文双表状态，"
                "已拒绝自动恢复；请使用迁移前备份恢复数据库"
            )
        generation_match = (
            "legacy.context_generation=current.context_generation"
            if "context_generation" in legacy_columns
            else "current.context_generation=0"
        )
        unexpected_row = conn.execute(
            "SELECT 1 FROM agent_session_context AS current "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM agent_session_context_v1 AS legacy "
            "WHERE legacy.id=current.id "
            "AND legacy.owner_digest=current.owner_digest "
            "AND legacy.context_type=current.context_type "
            "AND legacy.payload=current.payload "
            "AND legacy.expires_at=current.expires_at "
            "AND legacy.created_at=current.created_at "
            f"AND {generation_match}"
            ") LIMIT 1"
        ).fetchone()
        if unexpected_row is not None:
            raise RuntimeError(
                "检测到 Agent 会话上下文部分迁移表包含新增或冲突数据，"
                "已拒绝自动覆盖；请使用迁移前备份恢复数据库"
            )
        # 已确认现表仅是旧表的空集或一致子集，可以丢弃并从完整旧表重做。
        conn.execute("DROP TABLE agent_session_context")
    conn.execute("ALTER TABLE agent_session_context_v1 RENAME TO agent_session_context")
    logger.warning("检测到未完成的 Agent 会话上下文迁移，已安全恢复并重新执行")


def _migrate_agent_session_context_v2(conn: sqlite3.Connection) -> None:
    """移除固定 context_type CHECK，允许仓储白名单安全扩展上下文类型。"""
    _restore_interrupted_agent_session_context_v2(conn)
    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='agent_session_context'"
    ).fetchone()
    if row is None:
        return
    table_sql = str(row[0] or "").casefold()
    if "check" not in table_sql or "context_type" not in table_sql:
        return
    legacy_columns = {
        str(column[1])
        for column in conn.execute(
            "PRAGMA table_info(agent_session_context)"
        ).fetchall()
    }
    generation_expression = (
        "COALESCE(context_generation,0)"
        if "context_generation" in legacy_columns
        else "0"
    )
    conn.execute("ALTER TABLE agent_session_context RENAME TO agent_session_context_v1")
    conn.execute(
        "CREATE TABLE agent_session_context ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "owner_digest TEXT NOT NULL,"
        "context_type TEXT NOT NULL,"
        "payload TEXT NOT NULL,"
        "expires_at REAL NOT NULL,"
        "context_generation INTEGER NOT NULL DEFAULT 0,"
        "created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO agent_session_context("
        "id,owner_digest,context_type,payload,expires_at,context_generation,created_at"
        ") SELECT id,owner_digest,context_type,payload,expires_at,"
        f"{generation_expression},created_at "
        "FROM agent_session_context_v1"
    )
    conn.execute("DROP TABLE agent_session_context_v1")
    conn.execute(
        "CREATE INDEX idx_agent_session_context_lookup "
        "ON agent_session_context(owner_digest, context_type, expires_at, id DESC)"
    )
    conn.execute(
        "CREATE INDEX idx_agent_session_context_expiry "
        "ON agent_session_context(expires_at)"
    )


def _migrate_agent_session_context_v3(conn: sqlite3.Connection) -> None:
    """为跨 Worker 工作流增加 fencing generation 与持久化 epoch。"""
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='agent_session_context'"
    ).fetchone()
    if table_exists is not None:
        columns = {
            str(column[1])
            for column in conn.execute(
                "PRAGMA table_info(agent_session_context)"
            ).fetchall()
        }
        if "context_generation" not in columns:
            conn.execute(
                "ALTER TABLE agent_session_context ADD COLUMN "
                "context_generation INTEGER NOT NULL DEFAULT 0"
            )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_session_context_epochs ("
        "owner_digest TEXT NOT NULL,"
        "context_type TEXT NOT NULL,"
        "generation INTEGER NOT NULL CHECK(generation > 0),"
        "touched_at REAL NOT NULL,"
        "updated_at TEXT NOT NULL,"
        "PRIMARY KEY(owner_digest, context_type)"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_session_context_epochs_touched "
        "ON agent_session_context_epochs(touched_at)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_session_context_generation_sequence ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT"
        ")"
    )


def _migrate_organize_operation_jobs_v4(conn: sqlite3.Connection) -> None:
    """增加可恢复的光鸭单次操作队列与终态记录。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS organize_operation_jobs ("
        "job_id TEXT PRIMARY KEY,"
        "job_kind TEXT NOT NULL CHECK(job_kind IN "
        "('agent_directory_scrape','agent_guangya_cleanup','agent_guangya_rename','agent_guangya_fs_change','directory_scrape')),"
        "owner_digest TEXT NOT NULL,"
        "operation TEXT NOT NULL,reference TEXT NOT NULL DEFAULT '',"
        "payload_json TEXT NOT NULL DEFAULT '{}',payload_auth TEXT NOT NULL DEFAULT '',"
        "dedupe_digest TEXT NOT NULL,"
        "status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN "
        "('pending','running','completed','partial','failed','cancelled','manual_review')),"
        "lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),"
        "result_json TEXT NOT NULL DEFAULT '{}',error_code TEXT NOT NULL DEFAULT '',"
        "error TEXT NOT NULL DEFAULT '',"
        "cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),"
        "expires_at REAL NOT NULL DEFAULT 0,purged_at TEXT,"
        "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,started_at TEXT,finished_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_organize_operation_jobs_pending "
        "ON organize_operation_jobs(status, created_at, job_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_organize_operation_jobs_updated "
        "ON organize_operation_jobs(updated_at DESC, job_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_organize_operation_jobs_owner_updated "
        "ON organize_operation_jobs(owner_digest, updated_at DESC, job_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_organize_operation_jobs_active_dedupe "
        "ON organize_operation_jobs(owner_digest,dedupe_digest) "
        "WHERE status IN ('pending','running')"
    )


def _migrate_organize_operation_jobs_v5(conn: sqlite3.Connection) -> None:
    """强化主体隔离、确认过期、载荷完整性与隐私取消语义。"""
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(organize_operation_jobs)")
    }
    additions = {
        "payload_auth": "TEXT NOT NULL DEFAULT ''",
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1))",
        "expires_at": "REAL NOT NULL DEFAULT 0",
        "purged_at": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE organize_operation_jobs ADD COLUMN {name} {definition}"
            )
    conn.execute("DROP INDEX IF EXISTS idx_organize_operation_jobs_active_dedupe")
    conn.execute(
        "CREATE UNIQUE INDEX idx_organize_operation_jobs_active_dedupe "
        "ON organize_operation_jobs(owner_digest,dedupe_digest) "
        "WHERE status IN ('pending','running')"
    )
    timestamp = now()
    conn.execute(
        "UPDATE organize_operation_jobs SET status='cancelled',payload_json='{}',"
        "payload_auth='',error_code='UpgradeRequiresReconfirmation',"
        "error='服务升级后需重新预检确认',finished_at=COALESCE(finished_at,?),"
        "updated_at=? WHERE status='pending' AND COALESCE(payload_auth,'')=''",
        (timestamp, timestamp),
    )


def _ensure_agent_action_history_schema(conn: sqlite3.Connection) -> None:
    """仅由正式历史迁移补齐确认审计表与索引；业务 CRUD 不承担 schema 修复。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_action_history ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "confirmation_id TEXT NOT NULL DEFAULT '',"
        "owner_digest TEXT NOT NULL DEFAULT '',tool_name TEXT NOT NULL,"
        "risk TEXT NOT NULL,status TEXT NOT NULL,ok INTEGER NOT NULL DEFAULT 0,"
        "mode TEXT NOT NULL DEFAULT 'confirmed_action',summary TEXT NOT NULL,"
        "safe_details TEXT NOT NULL DEFAULT '{}',error_code TEXT NOT NULL DEFAULT '',"
        "elapsed_ms INTEGER NOT NULL DEFAULT 0,started_at TEXT NOT NULL,"
        "finished_at TEXT NOT NULL"
        ")"
    )
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(agent_action_history)")
    }
    if "confirmation_id" not in columns:
        conn.execute(
            "ALTER TABLE agent_action_history ADD COLUMN "
            "confirmation_id TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_action_history_id "
        "ON agent_action_history(id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_action_history_owner_id "
        "ON agent_action_history(owner_digest, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_action_history_tool_id "
        "ON agent_action_history(tool_name, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_action_history_ok_id "
        "ON agent_action_history(ok, id DESC)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_action_history_confirmation "
        "ON agent_action_history(confirmation_id) WHERE confirmation_id<>''"
    )


def _migrate_agent_action_history_v6(conn: sqlite3.Connection) -> None:
    """为确认写操作增加崩溃窗口内的持久执行标识。"""
    _ensure_agent_action_history_schema(conn)


def _migrate_local_media_recognition_summary_v7(conn: sqlite3.Connection) -> None:
    """持久化本地整理的最终媒体识别摘要。"""
    columns = {
        str(row["name"]) for row in conn.execute("PRAGMA table_info(local_media_tasks)")
    }
    if columns and "recognition_summary" not in columns:
        conn.execute(
            "ALTER TABLE local_media_tasks ADD COLUMN "
            "recognition_summary TEXT NOT NULL DEFAULT ''"
        )


def _migrate_local_library_target_server_path_v8(conn: sqlite3.Connection) -> None:
    """保存分类目标在 Jellyfin / Emby 中实际可见的目录根。"""
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(local_library_targets)")
    }
    if columns and "server_path" not in columns:
        conn.execute(
            "ALTER TABLE local_library_targets ADD COLUMN "
            "server_path TEXT NOT NULL DEFAULT ''"
        )


def _migrate_media_proxy_trusted_forwarders_v9(conn: sqlite3.Connection) -> None:
    """为每个媒体反代实例保存独立的可信代理边界。"""
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(media_proxy_instances)")
    }
    if columns and "trust_forwarded_headers" not in columns:
        conn.execute(
            "ALTER TABLE media_proxy_instances ADD COLUMN "
            "trust_forwarded_headers INTEGER NOT NULL DEFAULT 0"
        )
    if columns and "trusted_proxy_cidrs_json" not in columns:
        conn.execute(
            "ALTER TABLE media_proxy_instances ADD COLUMN "
            "trusted_proxy_cidrs_json TEXT NOT NULL DEFAULT '[]'"
        )


def _migrate_agent_guangya_operation_jobs_v10(
    conn: sqlite3.Connection,
) -> None:
    """一次扩展持久整理队列，承载 Agent 光鸭改名与残留清理任务。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='organize_operation_jobs'"
    ).fetchone()
    if row is None:
        _migrate_organize_operation_jobs_v4(conn)
        return
    table_sql = str(row["sql"] or "")
    if "agent_guangya_rename" in table_sql and "agent_guangya_cleanup" in table_sql:
        return
    for index_name in (
        "idx_organize_operation_jobs_pending",
        "idx_organize_operation_jobs_updated",
        "idx_organize_operation_jobs_owner_updated",
        "idx_organize_operation_jobs_active_dedupe",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {index_name}")
    conn.execute(
        "ALTER TABLE organize_operation_jobs RENAME TO organize_operation_jobs_v9"
    )
    conn.execute(
        "CREATE TABLE organize_operation_jobs ("
        "job_id TEXT PRIMARY KEY,"
        "job_kind TEXT NOT NULL CHECK(job_kind IN "
        "('agent_directory_scrape','agent_guangya_cleanup',"
        "'agent_guangya_rename','directory_scrape')),"
        "owner_digest TEXT NOT NULL,"
        "operation TEXT NOT NULL,reference TEXT NOT NULL DEFAULT '',"
        "payload_json TEXT NOT NULL DEFAULT '{}',payload_auth TEXT NOT NULL DEFAULT '',"
        "dedupe_digest TEXT NOT NULL,"
        "status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN "
        "('pending','running','completed','partial','failed','cancelled','manual_review')),"
        "lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),"
        "result_json TEXT NOT NULL DEFAULT '{}',error_code TEXT NOT NULL DEFAULT '',"
        "error TEXT NOT NULL DEFAULT '',"
        "cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),"
        "expires_at REAL NOT NULL DEFAULT 0,purged_at TEXT,"
        "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,started_at TEXT,finished_at TEXT"
        ")"
    )
    columns = (
        "job_id,job_kind,owner_digest,operation,reference,payload_json,payload_auth,"
        "dedupe_digest,status,lease_generation,result_json,error_code,error,"
        "cancel_requested,expires_at,purged_at,created_at,updated_at,started_at,finished_at"
    )
    conn.execute(
        f"INSERT INTO organize_operation_jobs({columns}) "
        f"SELECT {columns} FROM organize_operation_jobs_v9"
    )
    conn.execute("DROP TABLE organize_operation_jobs_v9")
    conn.execute(
        "CREATE INDEX idx_organize_operation_jobs_pending "
        "ON organize_operation_jobs(status, created_at, job_id)"
    )
    conn.execute(
        "CREATE INDEX idx_organize_operation_jobs_updated "
        "ON organize_operation_jobs(updated_at DESC, job_id)"
    )
    conn.execute(
        "CREATE INDEX idx_organize_operation_jobs_owner_updated "
        "ON organize_operation_jobs(owner_digest, updated_at DESC, job_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_organize_operation_jobs_active_dedupe "
        "ON organize_operation_jobs(owner_digest,dedupe_digest) "
        "WHERE status IN ('pending','running')"
    )


def _migrate_agent_guangya_fs_change_jobs_v17(
    conn: sqlite3.Connection,
) -> None:
    """扩展持久操作队列，承载通用光鸭文件系统变更计划。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='organize_operation_jobs'"
    ).fetchone()
    if row is None:
        _migrate_organize_operation_jobs_v4(conn)
        return
    table_sql = str(row["sql"] or "")
    if "agent_guangya_fs_change" in table_sql:
        return
    for index_name in (
        "idx_organize_operation_jobs_pending",
        "idx_organize_operation_jobs_updated",
        "idx_organize_operation_jobs_owner_updated",
        "idx_organize_operation_jobs_active_dedupe",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {index_name}")
    conn.execute(
        "ALTER TABLE organize_operation_jobs RENAME TO organize_operation_jobs_v16"
    )
    conn.execute(
        "CREATE TABLE organize_operation_jobs ("
        "job_id TEXT PRIMARY KEY,"
        "job_kind TEXT NOT NULL CHECK(job_kind IN "
        "('agent_directory_scrape','agent_guangya_cleanup',"
        "'agent_guangya_rename','agent_guangya_fs_change','directory_scrape')),"
        "owner_digest TEXT NOT NULL,"
        "operation TEXT NOT NULL,reference TEXT NOT NULL DEFAULT '',"
        "payload_json TEXT NOT NULL DEFAULT '{}',payload_auth TEXT NOT NULL DEFAULT '',"
        "dedupe_digest TEXT NOT NULL,"
        "status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN "
        "('pending','running','completed','partial','failed','cancelled','manual_review')),"
        "lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),"
        "result_json TEXT NOT NULL DEFAULT '{}',error_code TEXT NOT NULL DEFAULT '',"
        "error TEXT NOT NULL DEFAULT '',"
        "cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),"
        "expires_at REAL NOT NULL DEFAULT 0,purged_at TEXT,"
        "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,started_at TEXT,finished_at TEXT"
        ")"
    )
    columns = (
        "job_id,job_kind,owner_digest,operation,reference,payload_json,payload_auth,"
        "dedupe_digest,status,lease_generation,result_json,error_code,error,"
        "cancel_requested,expires_at,purged_at,created_at,updated_at,started_at,finished_at"
    )
    conn.execute(
        f"INSERT INTO organize_operation_jobs({columns}) "
        f"SELECT {columns} FROM organize_operation_jobs_v16"
    )
    conn.execute("DROP TABLE organize_operation_jobs_v16")
    conn.execute(
        "CREATE INDEX idx_organize_operation_jobs_pending "
        "ON organize_operation_jobs(status, created_at, job_id)"
    )
    conn.execute(
        "CREATE INDEX idx_organize_operation_jobs_updated "
        "ON organize_operation_jobs(updated_at DESC, job_id)"
    )
    conn.execute(
        "CREATE INDEX idx_organize_operation_jobs_owner_updated "
        "ON organize_operation_jobs(owner_digest, updated_at DESC, job_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_organize_operation_jobs_active_dedupe "
        "ON organize_operation_jobs(owner_digest,dedupe_digest) "
        "WHERE status IN ('pending','running')"
    )


def _migrate_agent_provider_plans_v18(conn: sqlite3.Connection) -> None:
    """建立 owner/session 隔离且可恢复的 Provider 写计划。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_provider_plans ("
        "plan_id TEXT PRIMARY KEY,"
        "owner_digest TEXT NOT NULL,session_digest TEXT NOT NULL,"
        "provider TEXT NOT NULL,profile_ref TEXT NOT NULL,operation TEXT NOT NULL,"
        "risk TEXT NOT NULL CHECK(risk IN ('low_write','write','danger')),"
        "status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN "
        "('prepared','running','succeeded','failed','stale','outcome_unknown')),"
        "arguments_json TEXT NOT NULL DEFAULT '{}',"
        "target_snapshot_json TEXT NOT NULL DEFAULT '{}',"
        "context_fingerprint TEXT NOT NULL,result_json TEXT NOT NULL DEFAULT '{}',"
        "summary TEXT NOT NULL DEFAULT '',error_code TEXT NOT NULL DEFAULT '',"
        "attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),"
        "expires_at REAL NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
        "started_at TEXT,finished_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_provider_plans_owner_updated "
        "ON agent_provider_plans(owner_digest,session_digest,updated_at DESC,plan_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_provider_plans_expiry "
        "ON agent_provider_plans(status,expires_at,plan_id)"
    )


def _migrate_local_media_numbering_mode_v11(conn: sqlite3.Connection) -> None:
    """持久化本地剧集编号方式，保证预览与最终执行使用同一映射。"""
    columns = {
        str(row["name"]) for row in conn.execute("PRAGMA table_info(local_media_tasks)")
    }
    if columns and "numbering_mode" not in columns:
        conn.execute(
            "ALTER TABLE local_media_tasks ADD COLUMN "
            "numbering_mode TEXT NOT NULL DEFAULT 'auto'"
        )


def _migrate_telegram_notification_outbox_v12(conn: sqlite3.Connection) -> None:
    """建立统一 Telegram 通知 outbox 与可更新消息线程。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS telegram_notification_outbox ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "event_key TEXT NOT NULL UNIQUE,thread_key TEXT NOT NULL DEFAULT '',"
        "topic TEXT NOT NULL DEFAULT 'system',importance TEXT NOT NULL DEFAULT 'result',"
        "chat_id TEXT NOT NULL DEFAULT '',event_json TEXT NOT NULL,"
        "message_id INTEGER,revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),"
        "delivered_revision INTEGER NOT NULL DEFAULT 0 CHECK(delivered_revision >= 0),"
        "status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN "
        "('pending','sending','retry_wait','sent','failed','outcome_unknown','suppressed')),"
        "attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),"
        "lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),"
        "next_attempt_at TEXT NOT NULL,last_error TEXT NOT NULL DEFAULT '',"
        "sent_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_notification_outbox_due "
        "ON telegram_notification_outbox(status,next_attempt_at,id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_notification_outbox_thread "
        "ON telegram_notification_outbox(thread_key,chat_id)"
    )


def _migrate_media_subscription_notification_outbox_v13(
    conn: sqlite3.Connection,
) -> None:
    """允许追更“无法判定”结果进入可靠通知 outbox。"""
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='media_subscription_notification_outbox'"
    ).fetchone()
    if schema is None:
        return
    normalized = re.sub(r"\s+", "", str(schema[0] or "").casefold())
    if "'inconclusive'" in normalized:
        return

    legacy = "media_subscription_notification_outbox_v12"
    conn.execute("DROP INDEX IF EXISTS idx_media_subscription_notification_due")
    conn.execute(
        "DROP INDEX IF EXISTS idx_media_subscription_notification_subscription"
    )
    conn.execute(
        f"ALTER TABLE media_subscription_notification_outbox RENAME TO {legacy}"
    )
    conn.execute(
        "CREATE TABLE media_subscription_notification_outbox ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "event_key TEXT NOT NULL UNIQUE,"
        "subscription_id INTEGER NOT NULL,"
        "subscription_revision INTEGER NOT NULL,"
        "run_id INTEGER,"
        "event_type TEXT NOT NULL CHECK(event_type IN "
        "('missing','satisfied','inconclusive','error')),"
        "payload_json TEXT NOT NULL DEFAULT '{}',"
        "status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN "
        "('pending','sending','retry_wait','sent','failed','discarded')),"
        "attempts INTEGER NOT NULL DEFAULT 0,"
        "lease_generation INTEGER NOT NULL DEFAULT 0,"
        "lease_until TEXT NOT NULL DEFAULT '',"
        "next_attempt_at TEXT NOT NULL,"
        "last_error TEXT NOT NULL DEFAULT '',"
        "sent_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
        "FOREIGN KEY (subscription_id) REFERENCES media_subscriptions(id) "
        "ON DELETE CASCADE,"
        "FOREIGN KEY (run_id) REFERENCES media_subscription_runs(id) "
        "ON DELETE SET NULL"
        ")"
    )
    columns = (
        "id,event_key,subscription_id,subscription_revision,run_id,event_type,"
        "payload_json,status,attempts,lease_generation,lease_until,"
        "next_attempt_at,last_error,sent_at,created_at,updated_at"
    )
    conn.execute(
        f"INSERT INTO media_subscription_notification_outbox({columns}) "
        f"SELECT {columns} FROM {legacy}"
    )
    conn.execute(f"DROP TABLE {legacy}")
    conn.execute(
        "CREATE INDEX idx_media_subscription_notification_due "
        "ON media_subscription_notification_outbox(status,next_attempt_at,id)"
    )
    conn.execute(
        "CREATE INDEX idx_media_subscription_notification_subscription "
        "ON media_subscription_notification_outbox(subscription_id,id DESC)"
    )


def _migrate_organize_confirmation_rollup_v14(
    conn: sqlite3.Connection,
) -> None:
    """关联人工确认与原整理任务，并记录汇总回写终态。"""
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(organize_confirmations)")
    }
    if not columns:
        # 极早期/损坏开发库可能只有版本号而没有该表；后续正式 schema
        # 基线会创建完整结构，这里不能提前对不存在的列创建索引。
        return
    if "organize_task_id" not in columns:
        conn.execute(
            "ALTER TABLE organize_confirmations ADD COLUMN "
            "organize_task_id TEXT DEFAULT ''"
        )
    if "rollup_applied" not in columns:
        conn.execute(
            "ALTER TABLE organize_confirmations ADD COLUMN "
            "rollup_applied INTEGER NOT NULL DEFAULT 0 "
            "CHECK(rollup_applied IN (0,1))"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_organize_confirmations_organize_task "
        "ON organize_confirmations(organize_task_id,id)"
    )


class _LegacyTelegramHTMLTextExtractor(HTMLParser):
    """把旧队列中已渲染的 Telegram HTML 收敛为安全纯文本。"""

    _BLOCK_TAGS = frozenset({"blockquote", "div", "p", "pre"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag.casefold() == "br" or tag.casefold() in self._BLOCK_TAGS:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        self.parts.append(str(data or ""))

    def text(self) -> str:
        return "".join(self.parts).replace("\r\n", "\n").replace("\r", "\n")


def _legacy_organize_notification_event(body: object, image_url: object) -> str:
    """将旧的已渲染正文转换为统一通知事件，避免标签二次转义。"""
    raw = str(body or "")
    parser = _LegacyTelegramHTMLTextExtractor()
    try:
        parser.feed(raw)
        parser.close()
        plain = parser.text()
    except Exception:
        # HTMLParser 对常规 Telegram 标记不会失败；损坏输入仍按纯文本保留。
        plain = html.unescape(re.sub(r"<[^>]*>", "", raw))
    lines = [line.strip() for line in plain.splitlines() if line.strip()]
    had_markup = bool(re.search(r"<\s*/?\s*[a-zA-Z][^>]*>", raw))
    title = "整理结果"
    if had_markup and lines:
        title = lines.pop(0)
    payload = {
        "title": title,
        "fields": [],
        "lines": lines if had_markup else ([plain.strip()] if plain.strip() else []),
        "image_url": str(image_url or ""),
        "footer": "",
        "actions": [],
        "layout": "default",
        "field_emojis": True,
        "state": "",
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _migrate_retire_organize_notification_outbox_v15(
    conn: sqlite3.Connection,
) -> None:
    """把整理汇总旧队列迁入统一 Telegram outbox 后移除旧表。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='organize_notification_outbox'"
    ).fetchone()
    if exists is None:
        return

    _migrate_telegram_notification_outbox_v12(conn)
    rows = conn.execute(
        "SELECT idempotency_key,chat_id,body,image_url,status,attempts,"
        "lease_generation,next_attempt_at,last_error,sent_at,created_at,updated_at "
        "FROM organize_notification_outbox ORDER BY id"
    ).fetchall()
    planned: list[tuple[sqlite3.Row | tuple, str]] = []
    for row in rows:
        idempotency_key = str(row[0] or "").strip()
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        event_key = f"retired-organize:{digest}"
        if (
            conn.execute(
                "SELECT 1 FROM telegram_notification_outbox WHERE event_key=?",
                (event_key,),
            ).fetchone()
            is not None
        ):
            raise sqlite3.IntegrityError(
                "统一 Telegram outbox 已存在旧整理通知迁移键；已保留源表等待人工核验"
            )
        planned.append((row, event_key))

    inserted = 0
    for row, event_key in planned:
        status = str(row[4] or "pending")
        canonical_status = {
            "pending": "pending",
            "retry_wait": "retry_wait",
            "sending": "outcome_unknown",
            "sent": "sent",
            "failed": "failed",
        }.get(status, "failed")
        last_error = str(row[8] or "")
        if status == "sending" and not last_error:
            last_error = "DeliveryOutcomeUnknown"
        event_json = _legacy_organize_notification_event(row[2], row[3])
        result = conn.execute(
            "INSERT INTO telegram_notification_outbox("
            "event_key,thread_key,topic,importance,chat_id,event_json,message_id,"
            "revision,delivered_revision,status,attempts,lease_generation,"
            "next_attempt_at,last_error,sent_at,created_at,updated_at"
            ") VALUES(?,'','organize','result',?,?,NULL,1,?,?,?, ?,?,?,?,?,?)",
            (
                event_key,
                str(row[1] or ""),
                event_json,
                1 if canonical_status == "sent" else 0,
                canonical_status,
                max(0, int(row[5] or 0)),
                max(0, int(row[6] or 0)),
                str(row[7] or row[11] or row[10] or now()),
                last_error,
                row[9],
                str(row[10] or now()),
                str(row[11] or row[10] or now()),
            ),
        )
        if result.rowcount != 1:
            raise sqlite3.IntegrityError("旧整理通知迁移写入数量异常；已保留源表")
        inserted += 1
    if inserted != len(rows):
        raise sqlite3.IntegrityError("旧整理通知迁移数量不一致；已保留源表")
    conn.execute("DROP TABLE organize_notification_outbox")


def _migrate_retire_telegram_write_confirmations_v16(
    conn: sqlite3.Connection,
) -> None:
    """移除已并入统一 Agent confirmation store 的短期 Telegram 票据表。

    旧票据有效期仅五分钟，且 owner 只保存不可逆摘要，无法安全重建 canonical
    owner。升级时 fail closed 使旧按钮失效，用户重新发起即可，避免保留第二套
    确认状态机和长期 schema 墓碑。
    """
    conn.execute("DROP TABLE IF EXISTS telegram_write_confirmations")


def _migrate_strm_refresh_outbox_v19(conn: sqlite3.Connection) -> None:
    """把仅覆盖元数据的旧 outbox 合并为统一 STRM 刷新交接队列。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS strm_refresh_outbox ("
        "path TEXT NOT NULL,"
        "allow_emby INTEGER NOT NULL DEFAULT 1 CHECK(allow_emby IN (0,1)),"
        "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
        "PRIMARY KEY(path,allow_emby))"
    )
    legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='strm_metadata_refresh_outbox'"
    ).fetchone()
    if legacy is not None:
        conn.execute(
            "INSERT INTO strm_refresh_outbox("
            "path,allow_emby,created_at,updated_at) "
            "SELECT path,1,created_at,updated_at "
            "FROM strm_metadata_refresh_outbox WHERE COALESCE(path,'')<>'' "
            "ON CONFLICT(path,allow_emby) DO UPDATE SET "
            "updated_at=MAX(strm_refresh_outbox.updated_at,excluded.updated_at)"
        )
        conn.execute("DROP TABLE strm_metadata_refresh_outbox")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strm_refresh_outbox_updated "
        "ON strm_refresh_outbox(updated_at,path,allow_emby)"
    )


def _legacy_rss_download_identity(
    payload: object, claim_key: object
) -> tuple[str, str, tuple[str, ...], str]:
    """把旧 RSS claim 还原为统一下载请求身份；仅供 v20 数据迁移。"""
    import base64
    from urllib.parse import parse_qs, unquote, urlsplit

    try:
        decoded = json.loads(str(payload or "{}"))
    except (TypeError, ValueError):
        decoded = {}
    source = (
        str(decoded.get("torrent_url") or decoded.get("link") or "").strip()
        if isinstance(decoded, dict)
        else ""
    )
    lowered = source.lower()
    if lowered.startswith("magnet:?"):
        kind = "magnet"
    elif lowered.startswith("ed2k://"):
        kind = "ed2k"
    else:
        kind = "http"

    btih = ""
    try:
        parsed = urlsplit(source)
        if kind == "magnet":
            for value in parse_qs(parsed.query).get("xt", []):
                xt = str(value or "").strip()
                if not xt.lower().startswith("urn:btih:"):
                    continue
                raw = xt[len("urn:btih:") :]
                if re.fullmatch(r"(?i)[0-9a-f]{40}", raw):
                    btih = raw.lower()
                    break
                if re.fullmatch(r"(?i)[a-z2-7]{32}", raw):
                    try:
                        btih = base64.b32decode(raw.upper()).hex()
                    except (ValueError, TypeError):
                        pass
                    break
        elif kind == "http":
            for part in reversed(
                [item for item in unquote(parsed.path).split("/") if item]
            ):
                match = re.fullmatch(r"(?i)([0-9a-f]{40})(?:\.torrent)?", part)
                if match:
                    btih = match.group(1).lower()
                    break
    except (TypeError, ValueError):
        btih = ""

    normalized_claim = str(claim_key or "").strip().lower()
    if btih and normalized_claim and btih != normalized_claim:
        # claim 与 payload 不一致时，不能把一个不可信旧键当成 qB task hash。
        btih = ""

    identities: list[str] = []
    if btih:
        identities.append(f"btih:{btih}")
    if source and (kind != "magnet" or not btih):
        identities.append(f"{kind}:{source}")
    if not identities:
        identities.append(f"legacy-rss-claim:{normalized_claim or 'unknown'}")
    request_keys = tuple(
        dict.fromkeys(
            hashlib.sha256(value.encode("utf-8")).hexdigest() for value in identities
        )
    )
    return kind, source, request_keys, btih


def _migrate_unify_rss_download_requests_v20(conn: sqlite3.Connection) -> None:
    """把 RSS 专属后端 claim 收口到统一下载请求后退休旧表。"""

    def columns(table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    request_columns = columns("download_requests")
    key_columns = columns("download_request_keys")
    entry_columns = columns("rss_entries")
    can_transfer = (
        {
            "id",
            "request_key",
            "origin",
            "kind",
            "title",
            "source_value",
            "targets",
            "status",
            "qb_task_id",
            "qb_status",
            "gy_status",
            "organize_started",
            "organize_status",
            "organize_finished_at",
            "local_import_status",
            "local_import_error",
            "local_import_completed_at",
            "error",
            "created_at",
            "updated_at",
            "completed_at",
        }
        <= request_columns
        and {"request_key", "request_id", "created_at"} <= key_columns
        and {
            "id",
            "rss_item_id",
            "title",
            "payload",
            "status",
            "processed",
            "created_at",
            "submitted_at",
        }
        <= entry_columns
    )
    migration_stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def merge_target(current: object, backend: str) -> str:
        normalized = str(current or "").strip().lower()
        if normalized == backend or normalized == "both":
            return normalized
        if normalized in {"qb", "guangya"}:
            return "both"
        return backend

    def legacy_backend_status(
        current: object,
        incoming: str,
    ) -> tuple[str, bool]:
        """合并旧 claim，未知结果不得覆盖已确认或正在推进的状态。"""
        normalized = str(current or "").strip().lower()
        if normalized == incoming:
            return normalized, False
        ambiguous = {"", "pending", "submitting", "outcome_unknown", "manual_review"}
        if incoming == "completed" and normalized in ambiguous:
            return incoming, True
        if incoming == "manual_review" and normalized in {
            "",
            "pending",
            "submitting",
            "outcome_unknown",
        }:
            return incoming, True
        # 旧迁移记录不能覆盖当前下载链路已经推进到 submitted、
        # downloading、failed、cancelled 等更明确的事实。
        return normalized, False

    for table, backend in (
        ("rss_qb_download_claims", "qb"),
        ("rss_guangya_download_claims", "guangya"),
    ):
        claim_columns = columns(table)
        if not claim_columns:
            continue

        if (
            can_transfer
            and {"infohash", "first_entry_id", "status", "created_at", "updated_at"}
            <= claim_columns
        ):
            claims = conn.execute(
                f"SELECT infohash,first_entry_id,status,created_at,updated_at FROM {table}"
            ).fetchall()
            for claim in claims:
                entry = conn.execute(
                    "SELECT id,rss_item_id,title,payload,status,processed,created_at,"
                    "submitted_at FROM rss_entries WHERE id=?",
                    (int(claim[1]),),
                ).fetchone()
                if entry is None:
                    continue
                kind, source, request_keys, btih = _legacy_rss_download_identity(
                    entry[3], claim[0]
                )
                placeholders = ",".join("?" for _ in request_keys)
                existing = conn.execute(
                    "SELECT r.id FROM download_requests r WHERE r.id IN ("
                    "SELECT request_id FROM download_request_keys "
                    f"WHERE request_key IN ({placeholders}) UNION "
                    "SELECT id FROM download_requests "
                    f"WHERE request_key IN ({placeholders})"
                    ") ORDER BY r.id DESC LIMIT 1",
                    [*request_keys, *request_keys],
                ).fetchone()
                claim_status = str(claim[2] or "").strip().lower()
                certain_completed = claim_status == "submitted" and bool(entry[5])
                root_status = "completed" if certain_completed else "manual_review"
                backend_status = "completed" if certain_completed else "manual_review"
                error = (
                    ""
                    if certain_completed
                    else "旧 RSS 下载提交结果无法证明，已转为人工核对"
                )
                created_at = str(claim[3] or entry[6] or migration_stamp)
                updated_at = str(claim[4] or entry[7] or migration_stamp)

                if existing is None:
                    values = {
                        "request_key": request_keys[0],
                        "origin": f"rss:{int(entry[1])}",
                        "kind": kind,
                        "title": str(entry[2] or "RSS 下载任务"),
                        "source_value": source,
                        "targets": backend,
                        "status": root_status,
                        "qb_task_id": btih if backend == "qb" else "",
                        "qb_status": backend_status if backend == "qb" else "",
                        "gy_status": backend_status if backend == "guangya" else "",
                        "organize_started": (
                            1 if backend == "guangya" and certain_completed else 0
                        ),
                        "organize_status": (
                            "skipped"
                            if backend == "guangya" and certain_completed
                            else ""
                        ),
                        "organize_finished_at": (
                            updated_at
                            if backend == "guangya" and certain_completed
                            else None
                        ),
                        "local_import_status": (
                            "skipped" if backend == "qb" and certain_completed else ""
                        ),
                        "local_import_error": (
                            "旧 RSS 完成记录仅迁移幂等边界，不回放历史本地整理"
                            if backend == "qb" and certain_completed
                            else ""
                        ),
                        "local_import_completed_at": (
                            updated_at
                            if backend == "qb" and certain_completed
                            else None
                        ),
                        "error": error,
                        "created_at": created_at,
                        "updated_at": updated_at,
                        "completed_at": updated_at,
                    }
                    names = ",".join(values)
                    markers = ",".join("?" for _ in values)
                    created = conn.execute(
                        f"INSERT INTO download_requests({names}) VALUES({markers})",
                        list(values.values()),
                    )
                    request_id = int(created.lastrowid)
                else:
                    request_id = int(existing[0])
                    current = conn.execute(
                        "SELECT targets,status,qb_status,gy_status,qb_task_id,"
                        "local_import_status,organize_started,organize_status,error,"
                        "updated_at,completed_at FROM download_requests WHERE id=?",
                        (request_id,),
                    ).fetchone()
                    if current is None:
                        continue
                    merged_target = merge_target(current["targets"], backend)
                    backend_column = "qb_status" if backend == "qb" else "gy_status"
                    merged_backend_status, claim_applied = legacy_backend_status(
                        current[backend_column],
                        backend_status,
                    )
                    merged_statuses = {
                        "qb": (
                            merged_backend_status
                            if backend == "qb"
                            else str(current["qb_status"] or "")
                        ),
                        "guangya": (
                            merged_backend_status
                            if backend == "guangya"
                            else str(current["gy_status"] or "")
                        ),
                    }
                    target_backends = (
                        ("qb", "guangya")
                        if merged_target == "both"
                        else (merged_target,)
                    )
                    target_statuses = [
                        merged_statuses[item]
                        for item in target_backends
                        if item in merged_statuses
                    ]
                    current_root_status = str(current["status"] or "")
                    merged_root_status = current_root_status
                    if (
                        target_statuses
                        and all(value == "completed" for value in target_statuses)
                        and current_root_status
                        not in {"partial", "failed", "cancelled"}
                    ):
                        merged_root_status = "completed"
                    elif (
                        "manual_review" in target_statuses
                        and current_root_status
                        not in {"partial", "failed", "cancelled"}
                    ):
                        merged_root_status = "manual_review"

                    effective_updated_at = max(
                        str(current["updated_at"] or ""), updated_at
                    )
                    updates: dict[str, object] = {
                        "targets": merged_target,
                        backend_column: merged_backend_status,
                        "status": merged_root_status,
                        "updated_at": effective_updated_at,
                    }
                    if (
                        backend == "qb"
                        and btih
                        and not str(current["qb_task_id"] or "")
                    ):
                        updates["qb_task_id"] = btih
                    if backend == "qb" and certain_completed and claim_applied:
                        if not str(current["local_import_status"] or ""):
                            updates.update(
                                {
                                    "local_import_status": "skipped",
                                    "local_import_error": (
                                        "旧 RSS 完成记录仅迁移幂等边界，不回放历史本地整理"
                                    ),
                                    "local_import_completed_at": updated_at,
                                }
                            )
                    if backend == "guangya" and certain_completed and claim_applied:
                        if not str(current["organize_status"] or ""):
                            updates.update(
                                {
                                    "organize_started": max(
                                        1, int(current["organize_started"] or 0)
                                    ),
                                    "organize_status": "skipped",
                                    "organize_finished_at": updated_at,
                                }
                            )
                    if (
                        merged_root_status == "manual_review"
                        and claim_applied
                        and not str(current["error"] or "")
                    ):
                        updates["error"] = error
                    if merged_root_status in {"completed", "manual_review"} and not str(
                        current["completed_at"] or ""
                    ):
                        updates["completed_at"] = updated_at
                    sets = ",".join(f"{name}=?" for name in updates)
                    conn.execute(
                        f"UPDATE download_requests SET {sets} WHERE id=?",
                        [*updates.values(), request_id],
                    )

                conn.executemany(
                    "INSERT OR IGNORE INTO download_request_keys("
                    "request_key,request_id,created_at) VALUES(?,?,?)",
                    [(key, request_id, created_at) for key in request_keys],
                )

        # 旧进程中断时可能留下无法续接的 lease。远端是否已接收不可证明，
        # 因此把仍未处理的条目收敛到人工核对，再删除旧协调表。
        conn.execute(
            "UPDATE rss_entries SET status='failed',processed=0,processed_at=NULL,"
            "failure_code='submission_outcome_unknown',failure_retryable=0,"
            "failed_at=COALESCE(failed_at,NULLIF(submitted_at,''),"
            "datetime('now','localtime')) WHERE COALESCE(processed,0)=0 "
            f"AND id IN (SELECT first_entry_id FROM {table})"
        )
        conn.execute(f"DROP TABLE {table}")


def _migrate_agent_kernel_v21(conn: sqlite3.Connection) -> None:
    """建立新 Agent Kernel 的统一会话、引用与真实事件存储。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_kernel_sessions ("
        "owner_digest TEXT NOT NULL,session_digest TEXT NOT NULL,"
        "generation INTEGER NOT NULL CHECK(generation >= 0),"
        "state_json TEXT NOT NULL,state_hmac TEXT NOT NULL,updated_at REAL NOT NULL,"
        "PRIMARY KEY(owner_digest,session_digest))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_kernel_refs ("
        "ref_id TEXT PRIMARY KEY,owner_digest TEXT NOT NULL,session_digest TEXT NOT NULL,"
        "kind TEXT NOT NULL,value_json TEXT NOT NULL,value_hmac TEXT NOT NULL,"
        "expires_at REAL NOT NULL,created_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_kernel_refs_scope "
        "ON agent_kernel_refs(owner_digest,session_digest,expires_at)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_kernel_events ("
        "event_id TEXT PRIMARY KEY,owner_digest TEXT NOT NULL,session_digest TEXT NOT NULL,"
        "turn_id TEXT NOT NULL,request_id TEXT NOT NULL,"
        "sequence INTEGER NOT NULL CHECK(sequence > 0),event_type TEXT NOT NULL,"
        "event_json TEXT NOT NULL,event_hmac TEXT NOT NULL,occurred_at TEXT NOT NULL,"
        "created_at REAL NOT NULL,UNIQUE(owner_digest,session_digest,turn_id,sequence))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_kernel_events_session "
        "ON agent_kernel_events(owner_digest,session_digest,created_at,sequence)"
    )


def _migrate_agent_recognition_review_v22(conn: sqlite3.Connection) -> None:
    """为整理识别的 Agent 主动复核增加持久队列与最小审计字段。"""

    def add_missing_columns(table: str, columns: dict[str, str]) -> None:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is None:
            return
        existing = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )

    add_missing_columns(
        "organize_confirmations",
        {
            "review_status": "TEXT NOT NULL DEFAULT '' CHECK(review_status IN ('','waiting','pending','running','approved','abstained','failed','cancelled'))",
            "review_attempts": "INTEGER NOT NULL DEFAULT 0 CHECK(review_attempts >= 0)",
            "review_result_json": "TEXT DEFAULT ''",
            "review_started_at": "TEXT",
            "review_completed_at": "TEXT",
            "confirmation_actor": "TEXT NOT NULL DEFAULT '' CHECK(confirmation_actor IN ('','human','agent'))",
        },
    )
    add_missing_columns(
        "organize_log",
        {
            "confirmation_actor": "TEXT NOT NULL DEFAULT '' CHECK(confirmation_actor IN ('','human','agent'))",
        },
    )
    add_missing_columns(
        "local_media_tasks",
        {
            "confirmation_actor": "TEXT NOT NULL DEFAULT '' CHECK(confirmation_actor IN ('','human','agent'))",
        },
    )
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='organize_confirmations'"
    ).fetchone() is not None:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_organize_confirmations_review_queue "
            "ON organize_confirmations(review_status,status,id)"
        )


def _migrate_agent_kernel_session_epochs_v23(conn: sqlite3.Connection) -> None:
    """保留已删除 Agent 会话的 generation，防止旧确认计划 ABA 复活。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_kernel_session_epochs ("
        "owner_digest TEXT NOT NULL,session_digest TEXT NOT NULL,"
        "generation INTEGER NOT NULL CHECK(generation >= 0),updated_at REAL NOT NULL,"
        "PRIMARY KEY(owner_digest,session_digest))"
    )
    conn.execute(
        "INSERT INTO agent_kernel_session_epochs("
        "owner_digest,session_digest,generation,updated_at) "
        "SELECT owner_digest,session_digest,generation,updated_at "
        "FROM agent_kernel_sessions WHERE 1 "
        "ON CONFLICT(owner_digest,session_digest) DO UPDATE SET "
        "generation=MAX(agent_kernel_session_epochs.generation,excluded.generation),"
        "updated_at=excluded.updated_at"
    )


def _migrate_agent_capability_closure_v24(conn: sqlite3.Connection) -> None:
    """保留旧偏好，增加结构化档案、一次性补偿状态及主动规则。"""
    automation_schema = """
CREATE TABLE IF NOT EXISTS media_automation_rules (
    id TEXT PRIMARY KEY,
    owner_digest TEXT NOT NULL,
    kind TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT NOT NULL,
    lease_token TEXT NOT NULL DEFAULT '',
    lease_until TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_automation_rules_due
ON media_automation_rules(enabled,next_run_at,lease_until);
CREATE INDEX IF NOT EXISTS idx_media_automation_rules_owner
ON media_automation_rules(owner_digest,id);
"""

    for statement in automation_schema.split(";"):
        if statement.strip():
            conn.execute(statement)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(agent_media_preferences)")}
    if columns and "profile_json" not in columns:
        conn.execute(
            "ALTER TABLE agent_media_preferences "
            "ADD COLUMN profile_json TEXT NOT NULL DEFAULT '{}'"
        )
    if columns and "revision_token" not in columns:
        conn.execute(
            "ALTER TABLE agent_media_preferences ADD COLUMN revision_token TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_compensations ("
        "receipt_id TEXT PRIMARY KEY,owner_digest TEXT NOT NULL,"
        "state TEXT NOT NULL DEFAULT 'available' "
        "CHECK(state IN ('available','executing','completed','outcome_unknown')),"
        "updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_compensations_owner "
        "ON agent_compensations(owner_digest,updated_at)"
    )


def _migrate_durable_handoffs_v25(conn: sqlite3.Connection) -> None:
    """保留旧队列，增加不可复用的刷新事件令牌及规格补全的持久交接。"""
    for table, column, definition in (
        ("strm_refresh_outbox", "event_token", "TEXT NOT NULL DEFAULT ''"),
        ("organize_probe_queue", "pending_strm_changes_json", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if columns and column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        if table == "strm_refresh_outbox" and columns:
            # 使用随机事件令牌而非秒时间戳或重置为 1 的版本，避免同秒与删后重建 ABA。
            conn.execute(
                "UPDATE strm_refresh_outbox SET event_token=lower(hex(randomblob(16))) "
                "WHERE event_token=''"
            )


def _migrate_strm_path_cleanup_v26(conn: sqlite3.Connection) -> None:
    """仅增加迁移清理凭据，不改写现有 STRM 索引或媒体文件。"""
    conn.execute('CREATE TABLE IF NOT EXISTS strm_path_cleanup (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    source TEXT NOT NULL,\n    file_id TEXT NOT NULL,\n    strm_path TEXT NOT NULL,\n    content_fingerprint TEXT NOT NULL,\n    created_at TEXT NOT NULL,\n    UNIQUE(source, file_id, strm_path, content_fingerprint)\n)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_strm_path_cleanup_source\n    ON strm_path_cleanup(source, id)')


def _migrate_organize_business_snapshot_v27(conn: sqlite3.Connection) -> None:
    """保留旧步骤原样；没有前像的数据不能凭文件名猜出旧媒体身份。"""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(organize_operation_steps)")}
    if columns and "state_before_json" not in columns:
        conn.execute(
            "ALTER TABLE organize_operation_steps ADD COLUMN "
            "state_before_json TEXT NOT NULL DEFAULT ''"
        )


def _migrate_postprocessing_recovery_v28(conn: sqlite3.Connection) -> None:
    """只增加持久后处理凭据；不猜旧任务的通知身份、不直接清理旧目录。"""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(organize_probe_queue)")}
    if columns and "notification_context_json" not in columns:
        conn.execute(
            "ALTER TABLE organize_probe_queue ADD COLUMN "
            "notification_context_json TEXT NOT NULL DEFAULT '{}'"
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS download_staging_reconcile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            confirmation_id INTEGER NOT NULL UNIQUE,
            request_id INTEGER NOT NULL,
            identity_json TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','retry','completed','retained','blocked')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            next_attempt_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (confirmation_id) REFERENCES organize_confirmations(id) ON DELETE CASCADE,
            FOREIGN KEY (request_id) REFERENCES download_requests(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_download_staging_reconcile_due "
        "ON download_staging_reconcile(status, next_attempt_at, id)"
    )


def _migrate_download_resource_and_strm_ownership_v29(conn: sqlite3.Connection) -> None:
    """保留下载资源类型及新STRM任务归属；不猜测旧队列关联。"""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(download_requests)")}
    if columns and "strm_generation" not in columns:
        conn.execute(
            "ALTER TABLE download_requests ADD COLUMN "
            "strm_generation INTEGER NOT NULL DEFAULT 0"
        )
    if columns and "content_type" not in columns:
        conn.execute(
            "ALTER TABLE download_requests ADD COLUMN content_type TEXT NOT NULL DEFAULT ''"
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strm_request_work (
            request_id INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            organize_task_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL CHECK(kind IN ('change','refresh')),
            work_key TEXT NOT NULL,
            failed_lease_generation INTEGER NOT NULL DEFAULT -1,
            PRIMARY KEY(request_id,kind,work_key),
            FOREIGN KEY(request_id) REFERENCES download_requests(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strm_request_work_key "
        "ON strm_request_work(kind,work_key)"
    )


def _migrate_episode_research_cache_v30(conn: sqlite3.Connection) -> None:
    """仅新增独立证据缓存；逐语句执行以保留协调器的备份和回滚边界。"""
    from app.database_schema import _EPISODE_RESEARCH_CACHE_STATEMENTS

    for statement in _EPISODE_RESEARCH_CACHE_STATEMENTS:
        conn.execute(statement)


def _migrate_recognition_format_rules_v31(conn: sqlite3.Connection) -> None:
    """仅新增发布格式教学规则表；逐语句执行以保留协调器的备份和回滚边界。"""
    from app.database_schema import _RECOGNITION_FORMAT_RULE_STATEMENTS

    for statement in _RECOGNITION_FORMAT_RULE_STATEMENTS:
        conn.execute(statement)


def _migrate_download_display_title_v32(conn: sqlite3.Connection) -> None:
    """新增纯展示名称及按请求补名索引；旧业务标题/路由不回写。"""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(download_requests)")}
    if columns and "display_title" not in columns:
        conn.execute("ALTER TABLE download_requests ADD COLUMN display_title TEXT NOT NULL DEFAULT ''")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='download_log'").fetchone():
        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_log_request_source_id ON download_log(request_id, source, id DESC)")


def _migrate_agent_kernel_effect_next_poll_index_v33(
    conn: sqlite3.Connection,
) -> None:
    """为待轮询会话添加与 due 查询一致的 JSON 表达式索引。"""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_kernel_effect_next_poll_at "
        "ON agent_kernel_sessions (CASE WHEN json_valid(state_json) THEN "
        "json_extract(state_json,'$.metadata.effect_next_poll_at') END)"
    )


# 正式 schema 升级按“当前版本 -> 下一版本”登记迁移函数。
_SCHEMA_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_agent_session_context_v2,
    2: _migrate_agent_session_context_v3,
    3: _migrate_organize_operation_jobs_v4,
    4: _migrate_organize_operation_jobs_v5,
    5: _migrate_agent_action_history_v6,
    6: _migrate_local_media_recognition_summary_v7,
    7: _migrate_local_library_target_server_path_v8,
    8: _migrate_media_proxy_trusted_forwarders_v9,
    9: _migrate_agent_guangya_operation_jobs_v10,
    10: _migrate_local_media_numbering_mode_v11,
    11: _migrate_telegram_notification_outbox_v12,
    12: _migrate_media_subscription_notification_outbox_v13,
    13: _migrate_organize_confirmation_rollup_v14,
    14: _migrate_retire_organize_notification_outbox_v15,
    15: _migrate_retire_telegram_write_confirmations_v16,
    16: _migrate_agent_guangya_fs_change_jobs_v17,
    17: _migrate_agent_provider_plans_v18,
    18: _migrate_strm_refresh_outbox_v19,
    19: _migrate_unify_rss_download_requests_v20,
    20: _migrate_agent_kernel_v21,
    21: _migrate_agent_recognition_review_v22,
    22: _migrate_agent_kernel_session_epochs_v23,
    23: _migrate_agent_capability_closure_v24,
    24: _migrate_durable_handoffs_v25,
    25: _migrate_strm_path_cleanup_v26,
    26: _migrate_organize_business_snapshot_v27,
    27: _migrate_postprocessing_recovery_v28,
    28: _migrate_download_resource_and_strm_ownership_v29,
    29: _migrate_episode_research_cache_v30,
    30: _migrate_recognition_format_rules_v31,
    31: _migrate_download_display_title_v32,
    32: _migrate_agent_kernel_effect_next_poll_index_v33,
}
