"""SQLite 数据层封装。

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

import json
import hashlib
import hmac
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
from app.private_files import protect_sqlite_files
from app.runtime_paths import get_runtime_paths

logger = get_logger(__name__)

_PRODUCTION_DB_PATH = PATHS.database_path.resolve()
DB_PATH = PATHS.database_path
_lock = threading.RLock()
_configured_test_mode = False
SCHEMA_VERSION = 13

_SQLITE_CONTENTION_PHASES = frozenset({"connect_setup", "operation", "commit", "init_schema"})
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
    return configured_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS organize_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,            -- guangya / 115 / 123
    original_path TEXT NOT NULL,
    new_path TEXT NOT NULL,
    file_id TEXT,
    status TEXT NOT NULL,            -- planned / success / failed / skipped / reverting / reverted
    tmdb_id TEXT,
    provider TEXT DEFAULT '',
    external_id TEXT DEFAULT '',
    operation_type TEXT DEFAULT 'organize',
    source_dir_id TEXT DEFAULT '',
    original_parent_id TEXT DEFAULT '',
    original_name TEXT DEFAULT '',
    current_parent_id TEXT DEFAULT '',
    current_name TEXT DEFAULT '',
    target_parent_id TEXT DEFAULT '',
    media_type TEXT DEFAULT '',
    title TEXT DEFAULT '',
    year TEXT DEFAULT '',
    season INTEGER,
    episode INTEGER,
    error TEXT DEFAULT '',
    release_parse_json TEXT DEFAULT '',
    parent_log_id INTEGER,
    operation_token TEXT DEFAULT '',
    version INTEGER DEFAULT 1,
    legacy_incomplete INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    FOREIGN KEY (parent_log_id) REFERENCES organize_log(id)
);
CREATE INDEX IF NOT EXISTS idx_organize_log_status_id ON organize_log(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_organize_log_file_id ON organize_log(file_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_organize_log_status_path ON organize_log(status, new_path);
CREATE INDEX IF NOT EXISTS idx_organize_log_operation_token
    ON organize_log(operation_token, id) WHERE operation_token!='';

CREATE TABLE IF NOT EXISTS organize_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,
    fingerprint TEXT NOT NULL,
    chat_id TEXT DEFAULT '',
    source_name TEXT DEFAULT '',
    directory_path TEXT DEFAULT '',
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','queued','running','completed','failed','expired','cancelled')),
    selected_index INTEGER,
    queued_at TEXT,
    task_id TEXT DEFAULT '',
    result_json TEXT DEFAULT '',
    error TEXT DEFAULT '',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_organize_confirmations_status_expiry
    ON organize_confirmations(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_organize_confirmations_fingerprint
    ON organize_confirmations(fingerprint, id DESC);

CREATE TABLE IF NOT EXISTS organize_confirmation_delivery_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_token TEXT NOT NULL UNIQUE,
    event_json TEXT NOT NULL,
    chat_id TEXT NOT NULL DEFAULT '',
    message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','sending','retry_wait','sent')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
    next_attempt_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (confirmation_token) REFERENCES organize_confirmations(token) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_organize_confirmation_delivery_due
    ON organize_confirmation_delivery_outbox(status, next_attempt_at, id);

-- 整理任务汇总/媒体卡的持久化投递队列。带按钮的待确认卡继续走
-- organize_confirmation_delivery_outbox，避免两套队列重复投递同一张卡。
CREATE TABLE IF NOT EXISTS organize_notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    image_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','sending','retry_wait','sent','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
    next_attempt_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_organize_notification_outbox_due
    ON organize_notification_outbox(status, next_attempt_at, id);

CREATE TABLE IF NOT EXISTS telegram_notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    thread_key TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT 'system',
    importance TEXT NOT NULL DEFAULT 'result',
    chat_id TEXT NOT NULL DEFAULT '',
    event_json TEXT NOT NULL,
    message_id INTEGER,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    delivered_revision INTEGER NOT NULL DEFAULT 0 CHECK(delivered_revision >= 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','sending','retry_wait','sent','failed','outcome_unknown','suppressed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
    next_attempt_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telegram_notification_outbox_due
    ON telegram_notification_outbox(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_telegram_notification_outbox_thread
    ON telegram_notification_outbox(thread_key, chat_id);

CREATE TABLE IF NOT EXISTS organize_log_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'metadata',
    original_parent_id TEXT DEFAULT '',
    original_name TEXT DEFAULT '',
    current_parent_id TEXT DEFAULT '',
    current_name TEXT DEFAULT '',
    target_parent_id TEXT DEFAULT '',
    target_name TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    etag TEXT DEFAULT '',
    status TEXT DEFAULT 'success',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    FOREIGN KEY (log_id) REFERENCES organize_log(id) ON DELETE CASCADE,
    UNIQUE(log_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_organize_log_items_log_id ON organize_log_items(log_id, id);

CREATE TABLE IF NOT EXISTS organize_operation_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER NOT NULL,
    operation_token TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    action TEXT NOT NULL,
    file_id TEXT DEFAULT '',
    from_parent_id TEXT DEFAULT '',
    from_name TEXT DEFAULT '',
    to_parent_id TEXT DEFAULT '',
    to_name TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (log_id) REFERENCES organize_log(id) ON DELETE CASCADE,
    UNIQUE(log_id, operation_token, step_index)
);
CREATE INDEX IF NOT EXISTS idx_organize_operation_steps_log_id ON organize_operation_steps(log_id, id);

CREATE TABLE IF NOT EXISTS organize_probe_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organize_log_id INTEGER NOT NULL UNIQUE,
    provider TEXT NOT NULL DEFAULT 'guangya',
    source_id TEXT NOT NULL DEFAULT '',
    rel_dir TEXT NOT NULL DEFAULT '',
    rules_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued','running','retry_wait','completed','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 2 CHECK(max_attempts >= 1),
    next_attempt_at TEXT NOT NULL,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_until REAL NOT NULL DEFAULT 0,
    last_error_type TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (organize_log_id) REFERENCES organize_log(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_organize_probe_queue_due
    ON organize_probe_queue(status,next_attempt_at,id);

CREATE TABLE IF NOT EXISTS organize_delete_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organize_log_id INTEGER,
    trigger TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'guangya',
    file_id TEXT NOT NULL,
    file_name TEXT DEFAULT '',
    parent_id TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    gcid TEXT DEFAULT '',
    replacement_file_id TEXT DEFAULT '',
    replacement_name TEXT DEFAULT '',
    replacement_size INTEGER DEFAULT 0,
    replacement_gcid TEXT DEFAULT '',
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    provider_result TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_organize_delete_audit_log_id ON organize_delete_audit(organize_log_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_organize_delete_audit_file_id ON organize_delete_audit(file_id, id DESC);

CREATE TABLE IF NOT EXISTS download_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,            -- qb / guangya
    title TEXT,
    path TEXT,
    status TEXT DEFAULT 'submitted', -- submitted / success / failed
    rss_item_id INTEGER,
    request_id INTEGER,
    backend_task_id TEXT,
    progress REAL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_download_log_status_id ON download_log(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_download_log_source_id ON download_log(source, id DESC);

CREATE TABLE IF NOT EXISTS download_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key TEXT NOT NULL UNIQUE,
    origin TEXT NOT NULL DEFAULT 'telegram',
    chat_id TEXT,
    user_id TEXT NOT NULL DEFAULT '',
    message_id TEXT,
    kind TEXT NOT NULL,              -- magnet / ed2k / http / torrent
    title TEXT,
    source_value TEXT,
    torrent_data BLOB,
    targets TEXT DEFAULT '',         -- qb / guangya / both
    status TEXT NOT NULL DEFAULT 'pending',
    qb_task_id TEXT,
    gy_task_id TEXT,
    gy_task_ids TEXT NOT NULL DEFAULT '[]',
    gy_batch_count INTEGER NOT NULL DEFAULT 0,
    gy_isolated INTEGER NOT NULL DEFAULT 0,
    gy_staging_parent_dir TEXT DEFAULT '',
    gy_staging_name TEXT DEFAULT '',
    gy_staging_cleanup_status TEXT DEFAULT '',
    gy_staging_cleanup_error TEXT DEFAULT '',
    gy_expected_file_count INTEGER NOT NULL DEFAULT 0,
    gy_settle_observed_file_count INTEGER NOT NULL DEFAULT 0,
    gy_settle_attempts INTEGER NOT NULL DEFAULT 0,
    gy_settle_snapshot TEXT DEFAULT '',
    gy_settle_stable_count INTEGER NOT NULL DEFAULT 0,
    gy_selection_mode TEXT DEFAULT '',
    gy_unverified_manifest INTEGER NOT NULL DEFAULT 0,
    qb_status TEXT DEFAULT '',
    gy_status TEXT DEFAULT '',
    qb_task_missing_since TEXT,
    gy_task_missing_since TEXT,
    gy_target_dir TEXT DEFAULT '',
    gy_target_name TEXT DEFAULT '',
    organize_started INTEGER DEFAULT 0,
    organize_attempts INTEGER NOT NULL DEFAULT 0,
    organize_next_retry_at TEXT,
    organize_task_id TEXT DEFAULT '',
    organize_run_id INTEGER,
    organize_status TEXT DEFAULT '',
    organize_error TEXT DEFAULT '',
    organize_finished_at TEXT,
    strm_run_id INTEGER,
    strm_status TEXT DEFAULT '',
    strm_error TEXT DEFAULT '',
    strm_finished_at TEXT,
    qb_content_path TEXT DEFAULT '',
    local_import_status TEXT DEFAULT '',
    local_import_attempts INTEGER DEFAULT 0,
    local_import_error TEXT DEFAULT '',
    local_import_target TEXT DEFAULT '',
    local_import_started_at TEXT,
    local_import_completed_at TEXT,
    attention_cleared_at TEXT,
    attention_clear_note TEXT DEFAULT '',
    notification_event_status TEXT DEFAULT '',
    notification_delivery_status TEXT DEFAULT '',
    notification_attempts INTEGER NOT NULL DEFAULT 0,
    notification_next_retry_at TEXT,
    notification_sent_at TEXT,
    notification_payload_json TEXT DEFAULT '',
    notification_lease_token TEXT DEFAULT '',
    notification_lease_expires_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_download_requests_status ON download_requests(status, id);

CREATE TABLE IF NOT EXISTS download_request_keys (
    request_key TEXT PRIMARY KEY,
    request_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (request_id) REFERENCES download_requests(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_download_request_keys_request
    ON download_request_keys(request_id);

CREATE TABLE IF NOT EXISTS agent_download_verifications (
    request_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    tmdb_id TEXT NOT NULL,
    season INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    as_of TEXT NOT NULL,
    library_name TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','running','retry_wait','visible','attention')),
    result TEXT NOT NULL DEFAULT ''
        CHECK(result IN ('','visible','missing','inconclusive')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    next_check_at TEXT NOT NULL,
    last_checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (request_id) REFERENCES download_requests(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_download_verifications_due
    ON agent_download_verifications(status, next_check_at, request_id);
CREATE INDEX IF NOT EXISTS idx_agent_download_verifications_terminal_updated
    ON agent_download_verifications(status, updated_at, request_id);

CREATE TABLE IF NOT EXISTS agent_download_verification_notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL UNIQUE,
    owner TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','sending','retry_wait','sent','discarded')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT NOT NULL DEFAULT '',
    next_attempt_at TEXT NOT NULL,
    last_error_type TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (request_id) REFERENCES download_requests(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_download_verification_notification_due
    ON agent_download_verification_notification_outbox(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_agent_download_verification_notification_terminal_updated
    ON agent_download_verification_notification_outbox(status, updated_at, id);

CREATE TABLE IF NOT EXISTS agent_library_patrol (
    patrol_key TEXT PRIMARY KEY
        CHECK(patrol_key = 'default'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','running','retry_wait')),
    outcome TEXT NOT NULL DEFAULT ''
        CHECK(outcome IN ('','updates_available','up_to_date','inconclusive',
                          'not_configured','unavailable','failed')),
    result_revision INTEGER NOT NULL DEFAULT 0,
    result_fingerprint TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    next_run_at TEXT NOT NULL,
    last_started_at TEXT,
    last_finished_at TEXT,
    as_of TEXT NOT NULL DEFAULT '',
    checked_series_count INTEGER NOT NULL DEFAULT 0,
    updates_available_count INTEGER NOT NULL DEFAULT 0,
    missing_episode_count INTEGER NOT NULL DEFAULT 0,
    inconclusive_count INTEGER NOT NULL DEFAULT 0,
    unmapped_series_count INTEGER NOT NULL DEFAULT 0,
    projection_json TEXT NOT NULL DEFAULT '{}',
    findings_truncated INTEGER NOT NULL DEFAULT 0
        CHECK(findings_truncated IN (0,1)),
    error_type TEXT NOT NULL DEFAULT '',
    cycle_as_of TEXT NOT NULL DEFAULT '',
    cycle_cursor_tmdb_id TEXT NOT NULL DEFAULT '',
    cycle_accumulator_json TEXT NOT NULL DEFAULT '{}',
    cycle_stall_attempts INTEGER NOT NULL DEFAULT 0,
    cycle_started_at TEXT,
    cycle_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_library_patrol_due
    ON agent_library_patrol(status, next_run_at, patrol_key);

CREATE TABLE IF NOT EXISTS agent_library_patrol_notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patrol_key TEXT NOT NULL DEFAULT 'default'
        CHECK(patrol_key = 'default'),
    result_revision INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    outcome TEXT NOT NULL
        CHECK(outcome IN ('updates_available','up_to_date')),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','sending','retry_wait','sent','discarded')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT NOT NULL DEFAULT '',
    next_attempt_at TEXT NOT NULL,
    last_error_type TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(patrol_key, result_revision)
);
CREATE INDEX IF NOT EXISTS idx_agent_library_patrol_notification_due
    ON agent_library_patrol_notification_outbox(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_agent_library_patrol_notification_terminal_updated
    ON agent_library_patrol_notification_outbox(status, updated_at, id);

CREATE TABLE IF NOT EXISTS agent_jobs (
    job_id TEXT PRIMARY KEY,
    owner_digest TEXT NOT NULL,
    job_type TEXT NOT NULL
        CHECK(job_type IN ('library_episode_audit')),
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','running','retry_wait','succeeded','failed','cancelled')),
    input_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    projection_json TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 10),
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
    next_run_at TEXT NOT NULL,
    progress_current INTEGER NOT NULL DEFAULT 0 CHECK(progress_current >= 0),
    progress_total INTEGER NOT NULL DEFAULT 0 CHECK(progress_total >= 0),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_due
    ON agent_jobs(status, next_run_at, job_type, job_id);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_owner_updated
    ON agent_jobs(owner_digest, updated_at DESC, job_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_jobs_owner_active
    ON agent_jobs(owner_digest, job_type, dedupe_key)
    WHERE status IN ('pending','running','retry_wait');

CREATE TABLE IF NOT EXISTS agent_maintenance (
    task_key TEXT PRIMARY KEY
        CHECK(task_key = 'history_cleanup'),
    next_run_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rss_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    refresh_cron TEXT,
    refresh_interval_minutes INTEGER DEFAULT 0,
    last_refreshed_at TEXT,
    urls TEXT NOT NULL,              -- 多条用换行分隔
    parser TEXT DEFAULT 'mikan',
    exclude_keywords TEXT,
    action TEXT DEFAULT 'subscribe', -- download / subscribe
    download_method TEXT DEFAULT '', -- 留空继承全局，或 qb / guangya
    qb_save_path TEXT DEFAULT '',
    gy_target_dir TEXT DEFAULT '',
    gy_target_dir_name TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rss_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rss_item_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT DEFAULT 'pending',   -- pending / submitting / downloaded / failed / skipped
    processed INTEGER DEFAULT 0,
    submitted_at TEXT,
    processed_at TEXT,
    failure_code TEXT NOT NULL DEFAULT '',
    failure_retryable INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    failed_at TEXT,
    pub_date TEXT,
    guid TEXT,
    payload TEXT,                    -- 原始 JSON
    created_at TEXT NOT NULL,
    FOREIGN KEY (rss_item_id) REFERENCES rss_items(id)
);
CREATE INDEX IF NOT EXISTS idx_rss_entries_status_processed
    ON rss_entries(status, processed);
CREATE INDEX IF NOT EXISTS idx_rss_entries_subscription_status
    ON rss_entries(rss_item_id, status, processed);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_entries_item_guid
    ON rss_entries(rss_item_id, guid);
CREATE INDEX IF NOT EXISTS idx_rss_entries_failure_retry
    ON rss_entries(status, failure_retryable, processed, id DESC);
CREATE INDEX IF NOT EXISTS idx_rss_entries_item_status_id
    ON rss_entries(rss_item_id, status, id DESC);

CREATE TABLE IF NOT EXISTS rss_guangya_download_claims (
    infohash TEXT PRIMARY KEY,
    first_entry_id INTEGER NOT NULL,
    lease_token TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitting'
        CHECK(status IN ('submitting','submitted','unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rss_guangya_claims_status_updated
    ON rss_guangya_download_claims(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS rss_qb_download_claims (
    infohash TEXT PRIMARY KEY,
    first_entry_id INTEGER NOT NULL,
    lease_token TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitting'
        CHECK(status IN ('submitting','submitted','unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rss_qb_claims_status_updated
    ON rss_qb_download_claims(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS rss_media_bindings (
    rss_item_id INTEGER PRIMARY KEY,
    tmdb_id TEXT NOT NULL,
    default_season INTEGER NOT NULL DEFAULT 1
        CHECK(default_season BETWEEN 0 AND 100),
    skip_existing_episodes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (rss_item_id) REFERENCES rss_items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rss_entry_media (
    rss_entry_id INTEGER PRIMARY KEY,
    media_key TEXT NOT NULL DEFAULT '',
    tmdb_id TEXT NOT NULL DEFAULT '',
    season INTEGER,
    episode INTEGER,
    skip_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (rss_entry_id) REFERENCES rss_entries(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rss_entry_media_key
    ON rss_entry_media(media_key) WHERE media_key!='';

CREATE TABLE IF NOT EXISTS media_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL DEFAULT 'tmdb',
    external_id TEXT NOT NULL DEFAULT '',
    tmdb_id TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK(media_type IN ('movie','tv')),
    title TEXT NOT NULL,
    original_title TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '',
    poster_key TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    monitor_mode TEXT NOT NULL DEFAULT 'missing'
        CHECK(monitor_mode IN ('missing','future','selected')),
    seasons_json TEXT NOT NULL DEFAULT '[]',
    include_specials INTEGER NOT NULL DEFAULT 0 CHECK(include_specials IN (0,1)),
    action TEXT NOT NULL DEFAULT 'confirm'
        CHECK(action IN ('notify','confirm','auto')),
    download_target TEXT NOT NULL DEFAULT 'guangya'
        CHECK(download_target IN ('qb','guangya','both')),
    sites_json TEXT NOT NULL DEFAULT '[]',
    check_interval_minutes INTEGER NOT NULL DEFAULT 4320,
    last_checked_at TEXT,
    next_check_at TEXT,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK(status IN ('new','checking','satisfied','missing','inconclusive','error','paused')),
    expected_count INTEGER NOT NULL DEFAULT 0,
    local_count INTEGER NOT NULL DEFAULT 0,
    missing_count INTEGER NOT NULL DEFAULT 0,
    missing_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tmdb_id, media_type)
);
CREATE INDEX IF NOT EXISTS idx_media_subscriptions_due
    ON media_subscriptions(enabled, next_check_at, id);
CREATE INDEX IF NOT EXISTS idx_media_subscriptions_status
    ON media_subscriptions(status, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS media_subscription_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    trigger_type TEXT NOT NULL DEFAULT 'manual'
        CHECK(trigger_type IN ('manual','scheduler','watchlist','retry')),
    subscription_revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK(status IN ('running','satisfied','missing','inconclusive','failed','cancelled')),
    summary TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (subscription_id) REFERENCES media_subscriptions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_media_subscription_runs_subscription
    ON media_subscription_runs(subscription_id, id DESC);

CREATE TABLE IF NOT EXISTS media_subscription_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    media_key TEXT NOT NULL,
    season INTEGER,
    episode INTEGER,
    result_id TEXT NOT NULL,
    site_id TEXT NOT NULL DEFAULT '',
    site_name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    size_text TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER,
    seeders INTEGER,
    published_at TEXT,
    relevance_score INTEGER,
    download_state TEXT NOT NULL DEFAULT 'unavailable',
    match_reasons_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'available'
        CHECK(status IN ('available','submitted','expired','dismissed')),
    request_id INTEGER,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (subscription_id) REFERENCES media_subscriptions(id) ON DELETE CASCADE,
    FOREIGN KEY (request_id) REFERENCES download_requests(id) ON DELETE SET NULL,
    UNIQUE(subscription_id, media_key, result_id)
);
CREATE INDEX IF NOT EXISTS idx_media_subscription_candidates_lookup
    ON media_subscription_candidates(subscription_id, status, media_key, id DESC);

CREATE TABLE IF NOT EXISTS agent_media_preferences (
    owner_digest TEXT PRIMARY KEY,
    preferred_server TEXT NOT NULL DEFAULT 'any'
        CHECK(preferred_server IN ('any','jellyfin','emby')),
    preferred_download_target TEXT NOT NULL DEFAULT 'guangya'
        CHECK(preferred_download_target IN ('qb','guangya','both')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_subscription_notification_rules (
    subscription_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    notify_on_missing INTEGER NOT NULL DEFAULT 1 CHECK(notify_on_missing IN (0,1)),
    notify_on_satisfied INTEGER NOT NULL DEFAULT 0 CHECK(notify_on_satisfied IN (0,1)),
    notify_on_error INTEGER NOT NULL DEFAULT 1 CHECK(notify_on_error IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (subscription_id) REFERENCES media_subscriptions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS media_subscription_notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    subscription_id INTEGER NOT NULL,
    subscription_revision INTEGER NOT NULL,
    run_id INTEGER,
    event_type TEXT NOT NULL
        CHECK(event_type IN ('missing','satisfied','inconclusive','error')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','sending','retry_wait','sent','failed','discarded')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT NOT NULL DEFAULT '',
    next_attempt_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (subscription_id) REFERENCES media_subscriptions(id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES media_subscription_runs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_media_subscription_notification_due
    ON media_subscription_notification_outbox(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_media_subscription_notification_subscription
    ON media_subscription_notification_outbox(subscription_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_media_subscription_candidates_expiry
    ON media_subscription_candidates(status, expires_at, id);

CREATE TABLE IF NOT EXISTS media_download_admissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_key TEXT NOT NULL,
    tmdb_id TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK(media_type IN ('movie','tv')),
    season INTEGER,
    episode INTEGER,
    subscription_id INTEGER NOT NULL,
    subscription_revision INTEGER NOT NULL DEFAULT 1,
    candidate_id INTEGER,
    request_id INTEGER,
    status TEXT NOT NULL DEFAULT 'claimed'
        CHECK(status IN ('claimed','dispatching','submitted','downloading','processing','completed','failed','released','cancelled')),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (subscription_id) REFERENCES media_subscriptions(id) ON DELETE CASCADE,
    FOREIGN KEY (candidate_id) REFERENCES media_subscription_candidates(id) ON DELETE SET NULL,
    FOREIGN KEY (request_id) REFERENCES download_requests(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_download_admissions_active
    ON media_download_admissions(media_key)
    WHERE status IN ('claimed','dispatching','submitted','downloading','processing');
CREATE INDEX IF NOT EXISTS idx_media_download_admissions_subscription
    ON media_download_admissions(subscription_id, status, media_key);
CREATE INDEX IF NOT EXISTS idx_media_download_admissions_latest
    ON media_download_admissions(subscription_id, media_key, id DESC);

CREATE TABLE IF NOT EXISTS tmdb_lock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_name TEXT NOT NULL,
    parent_path TEXT NOT NULL DEFAULT '',
    tmdb_id TEXT NOT NULL,
    title TEXT,
    year TEXT,
    media_type TEXT DEFAULT '',
    season INTEGER NOT NULL DEFAULT -1,
    key_version INTEGER NOT NULL DEFAULT 1,
    lock_source TEXT NOT NULL CHECK(lock_source IN ('manual','automatic')),
    locked_at TEXT NOT NULL,
    UNIQUE(raw_name, parent_path, media_type, season)
);

CREATE TABLE IF NOT EXISTS media_title_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_alias TEXT NOT NULL,
    alias TEXT NOT NULL,
    tmdb_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL CHECK(media_type IN ('movie','tv')),
    source TEXT NOT NULL DEFAULT 'manual',
    blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0,1)),
    hit_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(normalized_alias, media_type, tmdb_id)
);
CREATE INDEX IF NOT EXISTS idx_media_title_alias_lookup
    ON media_title_aliases(normalized_alias, media_type, blocked);

CREATE TABLE IF NOT EXISTS tmdb_regex_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    match_target TEXT NOT NULL DEFAULT 'filename'
        CHECK(match_target IN ('filename','parent','both')),
    tmdb_id TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'any'
        CHECK(media_type IN ('any','movie','tv')),
    season_override INTEGER,
    priority INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tmdb_regex_rules_match
    ON tmdb_regex_rules(disabled, priority DESC, id ASC);

CREATE TABLE IF NOT EXISTS recognition_preprocess_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    matcher_type TEXT NOT NULL DEFAULT 'text'
        CHECK(matcher_type IN ('text','regex')),
    pattern TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'filename'
        CHECK(scope IN ('filename','parent','both')),
    action TEXT NOT NULL
        CHECK(action IN ('delete','replace','season_override','season_offset','episode_offset')),
    replacement TEXT NOT NULL DEFAULT '',
    numeric_value INTEGER,
    priority INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)),
    builtin_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recognition_preprocess_rules_match
    ON recognition_preprocess_rules(disabled, priority DESC, id ASC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_recognition_preprocess_rules_builtin
    ON recognition_preprocess_rules(builtin_key) WHERE builtin_key <> '';

CREATE TABLE IF NOT EXISTS recognition_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_key TEXT NOT NULL UNIQUE,
    knowledge_type TEXT NOT NULL
        CHECK(knowledge_type IN ('release_group','release_suffix')),
    canonical_value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'user'
        CHECK(source IN ('builtin','learned','user')),
    confidence REAL NOT NULL DEFAULT 1.0,
    hit_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)),
    user_modified INTEGER NOT NULL DEFAULT 0 CHECK(user_modified IN (0,1)),
    seed_revision INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recognition_knowledge_lookup
    ON recognition_knowledge(disabled, knowledge_type, normalized_value);
CREATE INDEX IF NOT EXISTS idx_recognition_knowledge_source
    ON recognition_knowledge(source, updated_at DESC);

CREATE TABLE IF NOT EXISTS strm_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,            -- guangya / 115 / 123
    file_id TEXT NOT NULL,
    etag TEXT,
    size INTEGER,
    filename TEXT,
    strm_path TEXT,
    content_fingerprint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(source, file_id)
);

CREATE INDEX IF NOT EXISTS idx_strm_index_path ON strm_index(strm_path);

-- STRM 变化目标队列：整理产生的目标目录变化必须跨进程重启存活。
-- pending 与 inflight 分离，保证 running 期间到达的新变化不会被本轮消费覆盖。
CREATE TABLE IF NOT EXISTS strm_change_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL DEFAULT 'guangya',
    source_id TEXT NOT NULL,
    rel_dir TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK(state IN ('queued','running','dirty','completed','failed','stopped')),
    dirty INTEGER NOT NULL DEFAULT 0 CHECK(dirty IN (0,1)),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    pending_changes_json TEXT NOT NULL DEFAULT '[]',
    inflight_changes_json TEXT NOT NULL DEFAULT '[]',
    last_error TEXT NOT NULL DEFAULT '',
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_until REAL NOT NULL DEFAULT 0 CHECK(lease_until >= 0),
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
    next_attempt_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider, source_id, rel_dir)
);
CREATE INDEX IF NOT EXISTS idx_strm_change_queue_due
    ON strm_change_queue(state, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_strm_change_queue_lease
    ON strm_change_queue(state, lease_until);

-- STRM 伴随元数据下载队列：扫描只登记远端快照，后台消费者负责安全落盘。
-- revision/lease_generation 用于隔离运行中更新与过期 worker 的迟到结果。
CREATE TABLE IF NOT EXISTS strm_metadata_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL DEFAULT 'guangya',
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    file_id TEXT NOT NULL,
    parent_id TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    etag TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0 CHECK(size >= 0),
    rel_dir TEXT NOT NULL DEFAULT '',
    target_rel_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued','running','retry_wait','completed','failed','cancelled')),
    dirty INTEGER NOT NULL DEFAULT 0 CHECK(dirty IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 6 CHECK(max_attempts >= 1),
    next_attempt_at TEXT NOT NULL DEFAULT '',
    last_attempt_at TEXT,
    completed_at TEXT,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_until REAL NOT NULL DEFAULT 0 CHECK(lease_until >= 0),
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
    last_error_type TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider, source_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_strm_metadata_queue_due
    ON strm_metadata_queue(provider, status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_strm_metadata_queue_lease
    ON strm_metadata_queue(provider, status, lease_until, id);
CREATE INDEX IF NOT EXISTS idx_strm_metadata_queue_diagnostics
    ON strm_metadata_queue(status, source_id, updated_at DESC, id DESC);

-- 元数据已经落盘但媒体库尚未确认刷新的持久化 outbox。文件写入与入队在
-- 同一事务完成，进程在两者之间退出也不会永久漏掉 Jellyfin/Emby 刷新。
CREATE TABLE IF NOT EXISTS strm_metadata_refresh_outbox (
    path TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strm_metadata_refresh_outbox_updated
    ON strm_metadata_refresh_outbox(updated_at, path);

CREATE TABLE IF NOT EXISTS strm_retired_sources (
    source_id TEXT PRIMARY KEY,
    source_name TEXT DEFAULT '',
    strm_root TEXT NOT NULL DEFAULT '',
    queued_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS strm_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    source_name TEXT DEFAULT '',
    file_id TEXT NOT NULL,
    parent_id TEXT DEFAULT '',
    filename TEXT NOT NULL,
    action TEXT NOT NULL,
    rel_dir TEXT DEFAULT '',
    target_rel_path TEXT DEFAULT '',
    error TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    failure_count INTEGER NOT NULL DEFAULT 1,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(source_id, file_id, action)
);
CREATE INDEX IF NOT EXISTS idx_strm_failures_status_source
ON strm_failures(status, source_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_strm_failures_status_action
ON strm_failures(status, action, id DESC);

CREATE TABLE IF NOT EXISTS discovery_cache (
    cache_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    stale_until TEXT NOT NULL,
    last_error TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'success'
);
CREATE INDEX IF NOT EXISTS idx_discovery_cache_provider_expiry
    ON discovery_cache(provider, expires_at);

CREATE TABLE IF NOT EXISTS media_external_ids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    tmdb_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    year TEXT DEFAULT '',
    confidence REAL DEFAULT 0,
    confirmed INTEGER DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(provider, external_id, media_type)
);

CREATE TABLE IF NOT EXISTS media_watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    title TEXT DEFAULT '',
    year TEXT DEFAULT '',
    poster_key TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(provider, external_id, media_type)
);
CREATE INDEX IF NOT EXISTS idx_media_watchlist_created ON media_watchlist(id DESC);

CREATE TABLE IF NOT EXISTS settings_kv (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS local_media_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'admin',
    name TEXT NOT NULL,
    qb_profile TEXT DEFAULT '',
    qb_path_prefix TEXT DEFAULT '',
    local_root TEXT NOT NULL,
    smb_user TEXT DEFAULT '',
    smb_pass TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    stable_seconds INTEGER NOT NULL DEFAULT 300,
    scan_enabled INTEGER NOT NULL DEFAULT 0,
    scan_interval_minutes INTEGER NOT NULL DEFAULT 10,
    media_type TEXT NOT NULL DEFAULT 'auto',
    mode TEXT NOT NULL DEFAULT 'move' CHECK(mode IN ('move','preview_only')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner, name)
);
CREATE INDEX IF NOT EXISTS idx_local_media_sources_owner_enabled
    ON local_media_sources(owner, enabled, id);

CREATE TABLE IF NOT EXISTS local_library_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    owner TEXT NOT NULL DEFAULT 'admin',
    category TEXT NOT NULL,
    path TEXT NOT NULL,
    provider TEXT DEFAULT '',
    library_id TEXT DEFAULT '',
    library_name TEXT DEFAULT '',
    server_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES local_media_sources(id) ON DELETE CASCADE,
    UNIQUE(source_id, category)
);
CREATE INDEX IF NOT EXISTS idx_local_library_targets_owner_source
    ON local_library_targets(owner, source_id, category);

CREATE TABLE IF NOT EXISTS local_media_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'admin',
    source_id INTEGER NOT NULL,
    qb_hash TEXT,
    content_path TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'manual'
        CHECK(trigger IN ('qb_completed','scan','manual')),
    status TEXT NOT NULL DEFAULT 'waiting_stable'
        CHECK(status IN ('waiting_stable','recognizing','requires_manual','planned',
                         'moving','verifying','refreshing','completed','rolling_back','failed')),
    operation_token TEXT NOT NULL UNIQUE,
    stable_since TEXT DEFAULT '',
    snapshot_digest TEXT DEFAULT '',
    rules_snapshot TEXT NOT NULL DEFAULT '',
    recognition_summary TEXT NOT NULL DEFAULT '',
    tmdb_id TEXT DEFAULT '',
    media_type TEXT DEFAULT '',
    season_override INTEGER,
    episode_override INTEGER,
    numbering_mode TEXT NOT NULL DEFAULT 'auto',
    title TEXT DEFAULT '',
    year TEXT DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    error TEXT DEFAULT '',
    warning TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (source_id) REFERENCES local_media_sources(id) ON DELETE CASCADE,
    UNIQUE(source_id, qb_hash)
);
CREATE INDEX IF NOT EXISTS idx_local_media_tasks_owner_status
    ON local_media_tasks(owner, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_local_media_tasks_source_path
    ON local_media_tasks(source_id, content_path, id DESC);

CREATE TABLE IF NOT EXISTS local_media_task_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    owner TEXT NOT NULL DEFAULT 'admin',
    source_path TEXT NOT NULL,
    target_path TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'metadata',
    media_group TEXT DEFAULT '',
    action TEXT NOT NULL DEFAULT 'move',
    size INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    device INTEGER NOT NULL DEFAULT 0,
    inode INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'planned',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES local_media_tasks(id) ON DELETE CASCADE,
    UNIQUE(task_id, source_path)
);
CREATE INDEX IF NOT EXISTS idx_local_media_task_items_task
    ON local_media_task_items(task_id, status, id);

CREATE TABLE IF NOT EXISTS local_media_operation_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    operation_token TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    action TEXT NOT NULL,
    source_path TEXT DEFAULT '',
    target_path TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (task_id) REFERENCES local_media_tasks(id) ON DELETE CASCADE,
    UNIQUE(task_id, operation_token, step_index)
);
CREATE INDEX IF NOT EXISTS idx_local_media_operation_steps_task
    ON local_media_operation_steps(task_id, id);

CREATE TABLE IF NOT EXISTS gcid_import_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_token TEXT NOT NULL UNIQUE,
    manifest_digest TEXT NOT NULL,
    target_dir_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'previewed'
        CHECK(status IN ('previewed','running','success','partial_success','failed')),
    file_count INTEGER NOT NULL DEFAULT 0,
    total_size INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gcid_import_tasks_status_id
    ON gcid_import_tasks(status, id DESC);

CREATE TABLE IF NOT EXISTS gcid_import_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    gcid TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'previewed'
        CHECK(status IN ('previewed','running','success','failed')),
    remote_file_id TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES gcid_import_tasks(id) ON DELETE CASCADE,
    UNIQUE(task_id, path)
);
CREATE INDEX IF NOT EXISTS idx_gcid_import_items_task_status
    ON gcid_import_items(task_id, status, id);

CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name TEXT NOT NULL,
    trigger_type TEXT NOT NULL,       -- manual / cron / telegram
    status TEXT NOT NULL,             -- running / success / failed / skipped
    started_at TEXT NOT NULL,
    finished_at TEXT,
    result TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_runs_name_id ON task_runs(task_name, id DESC);

CREATE TABLE IF NOT EXISTS organize_operation_jobs (
    job_id TEXT PRIMARY KEY,
    job_kind TEXT NOT NULL
        CHECK(job_kind IN ('agent_directory_scrape','agent_guangya_cleanup','agent_guangya_rename','directory_scrape')),
    owner_digest TEXT NOT NULL,
    operation TEXT NOT NULL,
    reference TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    payload_auth TEXT NOT NULL DEFAULT '',
    dedupe_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','running','completed','partial','failed','cancelled','manual_review')),
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
    result_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
    expires_at REAL NOT NULL DEFAULT 0,
    purged_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_organize_operation_jobs_pending
    ON organize_operation_jobs(status, created_at, job_id);
CREATE INDEX IF NOT EXISTS idx_organize_operation_jobs_updated
    ON organize_operation_jobs(updated_at DESC, job_id);
CREATE INDEX IF NOT EXISTS idx_organize_operation_jobs_owner_updated
    ON organize_operation_jobs(owner_digest, updated_at DESC, job_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_organize_operation_jobs_active_dedupe
    ON organize_operation_jobs(owner_digest, dedupe_digest)
    WHERE status IN ('pending','running');

CREATE TABLE IF NOT EXISTS agent_action_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_id TEXT NOT NULL DEFAULT '',
    owner_digest TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'confirmed_action',
    summary TEXT NOT NULL,
    safe_details TEXT NOT NULL DEFAULT '{}',
    error_code TEXT NOT NULL DEFAULT '',
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_action_history_id
    ON agent_action_history(id DESC);
CREATE INDEX IF NOT EXISTS idx_agent_action_history_owner_id
    ON agent_action_history(owner_digest, id DESC);
CREATE INDEX IF NOT EXISTS idx_agent_action_history_tool_id
    ON agent_action_history(tool_name, id DESC);
CREATE INDEX IF NOT EXISTS idx_agent_action_history_ok_id
    ON agent_action_history(ok, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_action_history_confirmation
    ON agent_action_history(confirmation_id)
    WHERE confirmation_id<>'';

CREATE TABLE IF NOT EXISTS agent_session_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_digest TEXT NOT NULL,
    context_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    expires_at REAL NOT NULL,
    context_generation INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_session_context_lookup
    ON agent_session_context(owner_digest, context_type, expires_at, id DESC);
CREATE INDEX IF NOT EXISTS idx_agent_session_context_expiry
    ON agent_session_context(expires_at);

CREATE TABLE IF NOT EXISTS agent_session_context_epochs (
    owner_digest TEXT NOT NULL,
    context_type TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation > 0),
    touched_at REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(owner_digest, context_type)
);
CREATE INDEX IF NOT EXISTS idx_agent_session_context_epochs_touched
    ON agent_session_context_epochs(touched_at);

CREATE TABLE IF NOT EXISTS agent_session_context_generation_sequence (
    id INTEGER PRIMARY KEY AUTOINCREMENT
);

CREATE TABLE IF NOT EXISTS agent_confirmation_epochs (
    owner_digest TEXT PRIMARY KEY,
    generation INTEGER NOT NULL CHECK(generation > 0),
    touched_at REAL NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_confirmation_epochs_touched
    ON agent_confirmation_epochs(touched_at);

CREATE TABLE IF NOT EXISTS agent_confirmations (
    confirmation_id TEXT PRIMARY KEY,
    owner_digest TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    context_fingerprint TEXT NOT NULL DEFAULT '',
    expires_at REAL NOT NULL,
    owner_generation INTEGER NOT NULL CHECK(owner_generation > 0),
    followup_context_json TEXT NOT NULL DEFAULT '{}',
    confirmation_contract_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_confirmations_owner_expiry
    ON agent_confirmations(owner_digest, expires_at);
CREATE INDEX IF NOT EXISTS idx_agent_confirmations_expiry
    ON agent_confirmations(expires_at);

CREATE TABLE IF NOT EXISTS agent_action_leases (
    lease_key TEXT PRIMARY KEY,
    lease_token TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_action_leases_expiry
    ON agent_action_leases(expires_at);

CREATE TABLE IF NOT EXISTS telegram_agent_actions (
    action_id TEXT PRIMARY KEY,
    owner_digest TEXT NOT NULL,
    action_kind TEXT NOT NULL,
    group_id TEXT NOT NULL,
    confirmation_id TEXT NOT NULL DEFAULT '',
    result_id TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    arguments_json TEXT NOT NULL DEFAULT '{}',
    action_key TEXT NOT NULL DEFAULT '',
    expires_at REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telegram_agent_actions_owner_group
    ON telegram_agent_actions(owner_digest, group_id);
CREATE INDEX IF NOT EXISTS idx_telegram_agent_actions_owner_expiry
    ON telegram_agent_actions(owner_digest, expires_at);
CREATE INDEX IF NOT EXISTS idx_telegram_agent_actions_expiry
    ON telegram_agent_actions(expires_at);

CREATE TABLE IF NOT EXISTS telegram_write_confirmations (
    action_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    owner_digest TEXT NOT NULL,
    decision TEXT NOT NULL,
    operation TEXT NOT NULL,
    value_json TEXT NOT NULL DEFAULT '{}',
    expires_at REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telegram_write_confirmations_group
    ON telegram_write_confirmations(group_id);
CREATE INDEX IF NOT EXISTS idx_telegram_write_confirmations_owner_expiry
    ON telegram_write_confirmations(owner_digest, expires_at);
CREATE INDEX IF NOT EXISTS idx_telegram_write_confirmations_expiry
    ON telegram_write_confirmations(expires_at);

CREATE TABLE IF NOT EXISTS agent_missing_media_workflows (
    workflow_id TEXT PRIMARY KEY,
    owner_digest TEXT NOT NULL,
    source_tool TEXT NOT NULL
        CHECK(source_tool IN ('library.search_missing_episode_resources',
                              'library.search_missing_season_resources')),
    title TEXT NOT NULL,
    tmdb_id TEXT NOT NULL,
    season INTEGER NOT NULL CHECK(season BETWEEN 1 AND 100),
    as_of TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK(state IN ('search_ready','selection_required','confirmation_required',
                        'submitted','verification_pending','visible','attention',
                        'stale','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_missing_workflows_owner_updated
    ON agent_missing_media_workflows(owner_digest, updated_at DESC, workflow_id);
CREATE INDEX IF NOT EXISTS idx_agent_missing_workflows_state_updated
    ON agent_missing_media_workflows(state, updated_at, workflow_id);

CREATE TABLE IF NOT EXISTS agent_missing_media_workflow_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL UNIQUE,
    workflow_id TEXT NOT NULL,
    title TEXT NOT NULL,
    tmdb_id TEXT NOT NULL,
    season INTEGER NOT NULL CHECK(season BETWEEN 1 AND 100),
    episode INTEGER NOT NULL CHECK(episode BETWEEN 1 AND 1000),
    as_of TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK(state IN ('search_ready','selection_required','confirmation_required',
                        'submitted','verification_pending','visible','attention',
                        'stale','cancelled')),
    candidate_title TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '' CHECK(target IN ('','qb','guangya','both')),
    download_request_id INTEGER,
    last_error_code TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workflow_id) REFERENCES agent_missing_media_workflows(workflow_id)
        ON DELETE CASCADE,
    FOREIGN KEY (download_request_id) REFERENCES download_requests(id)
        ON DELETE SET NULL,
    UNIQUE(workflow_id, season, episode)
);
CREATE INDEX IF NOT EXISTS idx_agent_missing_workflow_items_workflow_episode
    ON agent_missing_media_workflow_items(workflow_id, episode);
CREATE INDEX IF NOT EXISTS idx_agent_missing_workflow_items_request
    ON agent_missing_media_workflow_items(download_request_id);

CREATE TABLE IF NOT EXISTS agent_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principal_digest TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0 CHECK(message_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(principal_digest, session_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_conversations_principal_updated
    ON agent_conversations(principal_digest, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS agent_conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES agent_conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_conversation_messages_conversation
    ON agent_conversation_messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS agent_conversation_summaries (
    conversation_id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,
    through_message_id INTEGER NOT NULL DEFAULT 0 CHECK(through_message_id >= 0),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES agent_conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_conversation_epochs (
    principal_digest TEXT NOT NULL,
    session_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation > 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(principal_digest, session_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_conversation_epochs_updated
    ON agent_conversation_epochs(updated_at);

CREATE TABLE IF NOT EXISTS agent_web_search_daily_usage (
    provider TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    credits_used INTEGER NOT NULL DEFAULT 0 CHECK(credits_used >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, usage_date)
);

CREATE TABLE IF NOT EXISTS agent_web_search_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_web_search_cache_expires
    ON agent_web_search_cache(expires_at);

CREATE TABLE IF NOT EXISTS agent_rate_limit_buckets (
    limiter_key TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0 CHECK(count >= 0),
    expires_at INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_proxy_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    server_type TEXT NOT NULL DEFAULT 'jellyfin',
    config_source TEXT NOT NULL DEFAULT 'custom',
    upstream_url TEXT NOT NULL,
    api_key TEXT DEFAULT '',
    listen_host TEXT NOT NULL DEFAULT '0.0.0.0',
    listen_port INTEGER NOT NULL,
    local_root TEXT DEFAULT '',
    trust_forwarded_headers INTEGER NOT NULL DEFAULT 0,
    trusted_proxy_cidrs_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'stopped',
    last_error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(listen_host, listen_port)
);
CREATE INDEX IF NOT EXISTS idx_media_proxy_instances_enabled ON media_proxy_instances(enabled, id);

CREATE TABLE IF NOT EXISTS media_proxy_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL,
    media_item_id TEXT NOT NULL,
    media_source_id TEXT DEFAULT '',
    source_type TEXT NOT NULL,
    guangya_file_id TEXT DEFAULT '',
    local_relative_path TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (instance_id) REFERENCES media_proxy_instances(id) ON DELETE CASCADE,
    UNIQUE(instance_id, media_item_id, media_source_id)
);
CREATE INDEX IF NOT EXISTS idx_media_proxy_bindings_lookup
ON media_proxy_bindings(instance_id, media_item_id, enabled);

CREATE TABLE IF NOT EXISTS media_proxy_playback_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL,
    session_key TEXT NOT NULL,
    media_item_id TEXT NOT NULL DEFAULT '',
    media_source_id TEXT NOT NULL DEFAULT '',
    media_name TEXT NOT NULL DEFAULT '',
    guangya_file_id TEXT NOT NULL DEFAULT '',
    request_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    cache_hit_count INTEGER NOT NULL DEFAULT 0,
    cache_miss_count INTEGER NOT NULL DEFAULT 0,
    upstream_latency_ms_total INTEGER NOT NULL DEFAULT 0,
    total_latency_ms_total INTEGER NOT NULL DEFAULT 0,
    max_total_latency_ms INTEGER NOT NULL DEFAULT 0,
    last_route_class TEXT NOT NULL DEFAULT '',
    last_source TEXT NOT NULL DEFAULT 'unknown',
    last_status_code INTEGER NOT NULL DEFAULT 0,
    last_failure_stage TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    last_request_at TEXT NOT NULL,
    UNIQUE(instance_id, session_key)
);
CREATE INDEX IF NOT EXISTS idx_media_proxy_sessions_instance_last
ON media_proxy_playback_sessions(instance_id, last_request_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_media_proxy_sessions_last
ON media_proxy_playback_sessions(last_request_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS media_proxy_playback_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL,
    session_id INTEGER,
    route_class TEXT NOT NULL,
    method TEXT NOT NULL,
    status_code INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'upstream',
    cache_hit INTEGER NOT NULL DEFAULT 0,
    upstream_latency_ms INTEGER NOT NULL DEFAULT 0,
    total_latency_ms INTEGER NOT NULL DEFAULT 0,
    failure_stage TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_proxy_records_instance_id
ON media_proxy_playback_records(instance_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_media_proxy_records_session_id
ON media_proxy_playback_records(session_id, id ASC);
CREATE INDEX IF NOT EXISTS idx_media_proxy_records_created
ON media_proxy_playback_records(created_at, id);

CREATE TABLE IF NOT EXISTS media_probe_cache (
    file_id TEXT PRIMARY KEY,
    etag TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_probe_cache_fingerprint_updated
ON media_probe_cache(etag, size, updated_at DESC, file_id DESC);
"""


def _protect_database_files(path: Path | None = None) -> None:
    """尽力收紧数据库文件权限，不让权限修复掩盖数据库业务异常。"""
    try:
        target = path if path is not None else resolve_db_path()
        if not protect_sqlite_files(target):
            logger.warning("数据库私有文件权限收紧失败")
    except Exception as exc:  # 权限防护为 best-effort，不能破坏数据库可用性
        logger.warning(
            "数据库私有文件权限收紧异常 type=%s", type(exc).__name__
        )


def _connect() -> sqlite3.Connection:
    started = time.monotonic()
    conn: sqlite3.Connection | None = None
    try:
        db_path = resolve_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        _protect_database_files(db_path)
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
    conn.execute(
        "ALTER TABLE agent_session_context_v1 RENAME TO agent_session_context"
    )
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
        "('agent_directory_scrape','agent_guangya_cleanup','agent_guangya_rename','directory_scrape')),"
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
    """兼容最小/旧数据库，在首条确认审计写入前补齐表与索引。"""
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
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(local_media_tasks)")
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
    if (
        "agent_guangya_rename" in table_sql
        and "agent_guangya_cleanup" in table_sql
    ):
        return
    for index_name in (
        "idx_organize_operation_jobs_pending",
        "idx_organize_operation_jobs_updated",
        "idx_organize_operation_jobs_owner_updated",
        "idx_organize_operation_jobs_active_dedupe",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {index_name}")
    conn.execute(
        "ALTER TABLE organize_operation_jobs "
        "RENAME TO organize_operation_jobs_v9"
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


def _migrate_local_media_numbering_mode_v11(conn: sqlite3.Connection) -> None:
    """持久化本地剧集编号方式，保证预览与最终执行使用同一映射。"""
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(local_media_tasks)")
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
    conn.execute(
        "DROP INDEX IF EXISTS idx_media_subscription_notification_due"
    )
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
}


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
        migration = _SCHEMA_MIGRATIONS.get(version)
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
                str(r[1])
                for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()
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


def init_db() -> None:
    """初始化首个正式数据库基线，并恢复上次异常中断的运行状态。"""
    with _lock:
        database_existed = resolve_db_path().exists()
        conn = _connect()
        try:
            _prepare_schema_migration(conn, database_existed=database_existed)
            # 未打版本的早期数据库也可能已有 v1 约束表；补列与重建必须作为
            # 一个原子步骤完成，避免退出后留下半迁移 schema。
            def prepare_legacy_schema(connection: sqlite3.Connection) -> None:
                _sync_missing_schema_columns(connection)
                _migrate_agent_session_context_v2(connection)

            _run_schema_savepoint(conn, operation=prepare_legacy_schema)
            _run_schema_savepoint(
                conn,
                operation=lambda connection: _execute_schema_script(connection, _SCHEMA),
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
            # 播放诊断保留期在启动时也执行，避免长期无新播放时旧媒体标识滞留。
            conn.execute(
                "DELETE FROM media_proxy_playback_records "
                "WHERE created_at < datetime('now','-30 days','localtime')"
            )
            conn.execute(
                "DELETE FROM media_proxy_playback_records WHERE id IN ("
                "SELECT id FROM media_proxy_playback_records "
                "ORDER BY id DESC LIMIT -1 OFFSET 10000"
                ")"
            )
            conn.execute(
                "DELETE FROM media_proxy_playback_sessions WHERE id NOT IN ("
                "SELECT DISTINCT session_id FROM media_proxy_playback_records "
                "WHERE session_id IS NOT NULL"
                ")"
            )
            conn.execute(
                "DELETE FROM agent_session_context WHERE expires_at<=?",
                (time.time(),),
            )
            conn.execute(
                "DELETE FROM agent_confirmations WHERE expires_at<=?",
                (time.time(),),
            )
            conn.execute(
                "DELETE FROM agent_action_leases WHERE expires_at<=?",
                (time.time(),),
            )
            conn.execute(
                "DELETE FROM telegram_agent_actions WHERE expires_at<=?",
                (time.time(),),
            )
            conn.execute(
                "DELETE FROM telegram_write_confirmations WHERE expires_at<=?",
                (time.time(),),
            )
            timestamp = now()
            interrupted_confirmations = conn.execute(
                "SELECT token,chat_id,directory_path,payload_json FROM organize_confirmations "
                "WHERE status='running'"
            ).fetchall()
            for interrupted in interrupted_confirmations:
                try:
                    stored_payload = json.loads(str(interrupted["payload_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    stored_payload = {}
                if not isinstance(stored_payload, dict):
                    stored_payload = {}
                try:
                    message_id = int(stored_payload.get("_telegram_message_id") or 0)
                except (TypeError, ValueError):
                    message_id = 0
                event_json = json.dumps(
                    {
                        "title": "❌ Telegram 确认整理已中断",
                        "fields": [["目录", str(interrupted["directory_path"] or "/")]],
                        "lines": [],
                        "image_url": "",
                        "footer": "上次进程在执行期间中断，结果未确认。请重新执行整理生成新候选。",
                        "actions": [],
                        "layout": "relaxed",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                conn.execute(
                    "INSERT INTO organize_confirmation_delivery_outbox("
                    "confirmation_token,event_json,chat_id,message_id,status,attempts,"
                    "lease_generation,next_attempt_at,last_error,sent_at,created_at,updated_at"
                    ") VALUES(?,?,?,?,'pending',0,0,?,'',NULL,?,?) "
                    "ON CONFLICT(confirmation_token) DO UPDATE SET "
                    "event_json=excluded.event_json,chat_id=excluded.chat_id,"
                    "message_id=excluded.message_id,status='pending',attempts=0,"
                    "lease_generation=organize_confirmation_delivery_outbox.lease_generation+1,"
                    "next_attempt_at=excluded.next_attempt_at,last_error='',sent_at=NULL,"
                    "updated_at=excluded.updated_at",
                    (
                        str(interrupted["token"] or ""),
                        event_json,
                        str(interrupted["chat_id"] or ""),
                        message_id or None,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
            conn.execute(
                "UPDATE organize_confirmations SET status='failed',"
                "error=CASE WHEN COALESCE(error,'')='' "
                "THEN '上次进程在 Telegram 确认整理执行期间中断，请重新执行整理' "
                "ELSE error END,completed_at=COALESCE(completed_at,?),updated_at=? "
                "WHERE status='running'",
                (timestamp, timestamp),
            )
            conn.execute(
                "UPDATE organize_confirmations SET status='expired',updated_at=? "
                "WHERE status='pending' AND expires_at<=?",
                (timestamp, timestamp),
            )
            conn.execute(
                "UPDATE organize_log SET status='interrupted',error=CASE "
                "WHEN COALESCE(error,'')='' THEN '上次进程在云端写操作期间中断，必须重新核验快照' "
                "ELSE error END,updated_at=? "
                "WHERE status IN ('reorganizing','returning','reverting','deleting')",
                (timestamp,),
            )
            conn.execute(
                "UPDATE organize_operation_steps SET status='interrupted',"
                "error=CASE WHEN COALESCE(error,'')='' THEN '进程中断，步骤结果需要人工核验' ELSE error END,"
                "finished_at=COALESCE(finished_at,?) WHERE status='running'",
                (timestamp,),
            )
            interrupted_organize_runs = conn.execute(
                "SELECT id,result,error FROM task_runs "
                "WHERE task_name='guangya_organize' AND status='running'"
            ).fetchall()
            if interrupted_organize_runs:
                # 局部导入避免底层数据库模块在正常路径依赖整理实现；恢复时仍
                # 复用唯一的版本化协议，禁止重新落回自由格式 result。
                from app.modules.organize_results import read_organize_result

                interrupted_message = "上次进程在整理任务运行期间中断"
                for interrupted_run in interrupted_organize_runs:
                    try:
                        raw_result = json.loads(str(interrupted_run["result"] or "{}"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        raw_result = {}
                    normalized_result = read_organize_result(raw_result)
                    previous_error = str(
                        normalized_result.get("error")
                        or interrupted_run["error"]
                        or ""
                    ).strip()
                    if previous_error and previous_error != interrupted_message:
                        normalized_result["previous_error"] = previous_error
                    normalized_result["status"] = "failed"
                    normalized_result["error"] = interrupted_message
                    conn.execute(
                        "UPDATE task_runs SET status='failed',"
                        "finished_at=COALESCE(finished_at,?),result=?,"
                        "error=? "
                        "WHERE id=? AND status='running'",
                        (
                            timestamp,
                            json.dumps(
                                normalized_result,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                default=str,
                            ),
                            interrupted_message,
                            int(interrupted_run["id"]),
                        ),
                    )
            conn.execute(
                "UPDATE task_runs SET status='failed',finished_at=COALESCE(finished_at,?),"
                "error=CASE WHEN COALESCE(error,'')='' "
                "THEN '上次进程在 STRM 同步期间中断' ELSE error END "
                "WHERE task_name='strm_sync' AND status='running'",
                (timestamp,),
            )
            conn.execute(
                "UPDATE organize_delete_audit SET status='interrupted',"
                "error='上次进程在光鸭 provider 调用期间中断，结果未知，需人工核验',"
                "provider_result='光鸭回收站结果未知，需人工核验',updated_at=? "
                "WHERE status='pending'",
                (timestamp,),
            )
            conn.execute(
                "UPDATE strm_failures SET status='open',"
                "error=CASE WHEN COALESCE(error,'')='' "
                "THEN '上次 STRM 重试进程中断，已释放为可重试' "
                "ELSE '上次 STRM 重试进程中断，已释放为可重试；原错误：' "
                "|| substr(error,1,350) END,"
                "updated_at=?,resolved_at=NULL WHERE status='retrying'",
                (timestamp,),
            )
            conn.execute(
                "UPDATE local_media_tasks SET status='failed',"
                "error=CASE WHEN COALESCE(error,'')='' "
                "THEN '上次进程在本地媒体写操作期间中断，文件及 qB 状态需人工核验后重试' "
                "ELSE error END,completed_at=COALESCE(completed_at,?),updated_at=? "
                "WHERE status IN ('recognizing','planned','moving','verifying','refreshing','rolling_back')",
                (timestamp, timestamp),
            )
            conn.execute(
                "UPDATE local_media_operation_steps SET status='failed',"
                "error=CASE WHEN COALESCE(error,'')='' THEN '进程中断，步骤结果需要人工核验' ELSE error END,"
                "finished_at=COALESCE(finished_at,?) WHERE status='running'",
                (timestamp,),
            )
            conn.execute(
                "UPDATE download_requests SET organize_started=-1,organize_status='failed',"
                "organize_error=CASE WHEN COALESCE(organize_error,'')='' "
                "THEN '上次进程在整理任务运行期间中断，需人工核验' ELSE organize_error END,"
                "organize_finished_at=COALESCE(organize_finished_at,?),updated_at=? "
                "WHERE organize_status='running'",
                (timestamp, timestamp),
            )
            conn.execute(
                "UPDATE download_requests SET strm_status='failed',"
                "strm_error=CASE WHEN COALESCE(strm_error,'')='' "
                "THEN '上次进程在 STRM 同步或排队期间中断' ELSE strm_error END,"
                "strm_finished_at=COALESCE(strm_finished_at,?),updated_at=? "
                "WHERE strm_status IN ('pending','queued','running')",
                (timestamp, timestamp),
            )
            conn.execute(
                "UPDATE download_requests SET status='manual_review',gy_status='manual_review',"
                "error=CASE WHEN COALESCE(error,'')='' "
                "THEN '上次进程在光鸭分享转存期间中断，云端写入结果未知；请核对目标目录，勿直接重试' "
                "ELSE error END,completed_at=COALESCE(completed_at,?),updated_at=? "
                "WHERE kind='guangya_share' AND status IN ('pending','submitting')",
                (timestamp, timestamp),
            )
            conn.execute(
                "UPDATE download_requests SET notification_delivery_status='retry_wait',"
                "notification_lease_token='',notification_lease_expires_at=NULL,"
                "notification_next_retry_at=?,updated_at=? "
                "WHERE notification_delivery_status='sending'",
                (timestamp, timestamp),
            )
            conn.execute(
                "UPDATE download_requests SET status='manual_review',"
                "qb_status=CASE WHEN qb_status='submitting' THEN 'manual_review' ELSE qb_status END,"
                "gy_status=CASE WHEN gy_status='submitting' THEN 'manual_review' ELSE gy_status END,"
                "error=CASE WHEN COALESCE(error,'')='' "
                "THEN '上次进程在下载后端提交期间中断，远端接收结果未知；请先核对下载器，勿直接重复提交' "
                "ELSE substr(error || char(10) || "
                "'上次进程在下载后端提交期间中断，远端接收结果未知；请先核对下载器，勿直接重复提交',1,1000) END,"
                "completed_at=COALESCE(completed_at,?),updated_at=? "
                "WHERE COALESCE(kind,'')<>'guangya_share' AND "
                "(status='submitting' OR qb_status='submitting' OR gy_status='submitting')",
                (timestamp, timestamp),
            )
            conn.execute(
                "UPDATE agent_download_verifications SET status='retry_wait',"
                "lease_generation=lease_generation+1,"
                "next_check_at=?,updated_at=? WHERE status='running'",
                (timestamp, timestamp),
            )
            conn.execute(
                "UPDATE agent_library_patrol SET status='retry_wait',"
                "lease_generation=lease_generation+1,"
                "next_run_at=?,updated_at=? WHERE status='running'",
                (timestamp, timestamp),
            )
            conn.execute(
                "UPDATE agent_jobs SET "
                "status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'retry_wait' END,"
                "lease_generation=lease_generation+1,next_run_at=?,"
                "error_code=CASE WHEN cancel_requested=1 THEN '' ELSE 'ProcessInterrupted' END,"
                "finished_at=CASE WHEN cancel_requested=1 THEN ? ELSE finished_at END,"
                "updated_at=? WHERE status='running'",
                (timestamp, timestamp, timestamp),
            )
            conn.execute(
                "UPDATE organize_confirmation_delivery_outbox "
                "SET status='retry_wait',lease_generation=lease_generation+1,"
                "next_attempt_at=?,updated_at=? WHERE status='sending'",
                (timestamp, timestamp),
            )
            conn.execute(
                "UPDATE telegram_notification_outbox "
                "SET status=CASE WHEN COALESCE(message_id,0)>0 "
                "THEN 'retry_wait' ELSE 'outcome_unknown' END,"
                "lease_generation=lease_generation+1,next_attempt_at=?,"
                "last_error=CASE WHEN COALESCE(message_id,0)>0 "
                "THEN 'ProcessInterrupted' ELSE 'DeliveryOutcomeUnknown' END,"
                "updated_at=? WHERE status='sending'",
                (timestamp, timestamp),
            )
            conn.execute(
                "UPDATE agent_download_verification_notification_outbox "
                "SET status='discarded',lease_generation=lease_generation+1,"
                "payload_json='',last_error_type='DeliveryOutcomeUnknown',updated_at=? "
                "WHERE status='sending'",
                (timestamp,),
            )
            conn.execute(
                "UPDATE agent_library_patrol_notification_outbox "
                "SET status='discarded',lease_generation=lease_generation+1,"
                "payload_json='',last_error_type='DeliveryOutcomeUnknown',updated_at=? "
                "WHERE status='sending'",
                (timestamp,),
            )
            conn.execute(
                "UPDATE rss_entries SET status='failed', processed=0, processed_at=NULL, "
                "failure_code='submission_outcome_unknown', failure_retryable=0, "
                "failed_at=COALESCE(NULLIF(submitted_at,''),?) "
                "WHERE status='submitting' "
                "AND datetime(COALESCE(NULLIF(submitted_at,''),created_at)) "
                "< datetime('now','localtime','-15 minutes')",
                (timestamp,),
            )
            conn.execute(
                "UPDATE agent_action_history SET status='outcome_unknown',ok=0,"
                "summary='Agent 受确认动作：结果待核对',"
                "error_code='execution_interrupted',finished_at=?,elapsed_ms=0 "
                "WHERE status='executing'",
                (timestamp,),
            )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
            _protect_database_files()
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
            conn.close()
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
            logger.warning(
                "数据库回滚失败 type=%s", type(rollback_exc).__name__
            )
        raise
    finally:
        try:
            conn.close()
        except sqlite3.Error as close_exc:
            logger.warning(
                "数据库连接关闭失败 type=%s", type(close_exc).__name__
            )
        finally:
            _protect_database_files()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ===== Agent 网页搜索每日额度 =====
# 兼容门面：调用方继续使用 app.database.*，事务实现按业务域拆分。
from app.repositories.agent_web_search import (  # noqa: E402
    _validate_agent_web_search_usage_date,
    clear_agent_web_search_cache,
    get_agent_web_search_cache,
    get_agent_web_search_daily_usage,
    refund_agent_web_search_credits,
    reserve_agent_web_search_credits,
    set_agent_web_search_cache,
)


# ===== 媒体探测缓存 =====
# 兼容门面：调用方继续使用 app.database.*；批量读取保持单连接契约。
from app.repositories.media_probe import (  # noqa: E402, F401
    get_media_probe_cache,
    get_media_probe_cache_many,
    prune_media_probe_cache,
    upsert_media_probe_cache,
    upsert_media_probe_failure_cache,
)


# ===== 整理后媒体规格补全队列 =====
from app.repositories.organize_probe import (  # noqa: E402
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


# ===== Emby / Jellyfin 多实例媒体反代 =====
# 兼容门面：调用方继续使用 app.database.*；schema/连接仍由本模块持有。
from app.repositories.media_proxy import (  # noqa: E402
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




def _ensure_organize_delete_audit_schema(conn: sqlite3.Connection) -> None:
    """兼容未经过完整应用启动的维护脚本和单元测试连接。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS organize_delete_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organize_log_id INTEGER,
            trigger TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'guangya',
            file_id TEXT NOT NULL,
            file_name TEXT DEFAULT '',
            parent_id TEXT DEFAULT '',
            size INTEGER DEFAULT 0,
            gcid TEXT DEFAULT '',
            replacement_file_id TEXT DEFAULT '',
            replacement_name TEXT DEFAULT '',
            replacement_size INTEGER DEFAULT 0,
            replacement_gcid TEXT DEFAULT '',
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            provider_result TEXT DEFAULT '',
            error TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_organize_delete_audit_log_id
            ON organize_delete_audit(organize_log_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_organize_delete_audit_file_id
            ON organize_delete_audit(file_id, id DESC);
    """)

def add_organize_delete_audit(
    *, trigger: str, file_id: str, reason: str, status: str,
    organize_log_id: int | None = None, provider: str = "guangya",
    file_name: str = "", parent_id: str = "", size: int = 0, gcid: str = "",
    replacement_file_id: str = "", replacement_name: str = "",
    replacement_size: int = 0, replacement_gcid: str = "",
    provider_result: str = "", error: str = "",
) -> int:
    timestamp = now()
    with get_conn() as conn:
        _ensure_organize_delete_audit_schema(conn)
        cur = conn.execute(
            "INSERT INTO organize_delete_audit(organize_log_id,trigger,provider,file_id,"
            "file_name,parent_id,size,gcid,replacement_file_id,replacement_name,"
            "replacement_size,replacement_gcid,reason,status,provider_result,error,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (organize_log_id, trigger, provider, file_id, file_name, parent_id,
             int(size or 0), gcid, replacement_file_id, replacement_name,
             int(replacement_size or 0), replacement_gcid, reason, status,
             provider_result, error, timestamp, timestamp),
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
        _ensure_organize_delete_audit_schema(conn)
        cur = conn.execute(
            f"UPDATE organize_delete_audit SET {', '.join(sets)} WHERE id=?", values
        )
        return cur.rowcount == 1


def get_organize_delete_audit(audit_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        _ensure_organize_delete_audit_schema(conn)
        return conn.execute(
            "SELECT * FROM organize_delete_audit WHERE id=?", (int(audit_id),)
        ).fetchone()


def list_organize_delete_audits(
    organize_log_id: int | None = None, limit: int = 100,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM organize_delete_audit"
    params: list = []
    if organize_log_id is not None:
        sql += " WHERE organize_log_id=?"
        params.append(int(organize_log_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 100), 1000)))
    with get_conn() as conn:
        _ensure_organize_delete_audit_schema(conn)
        return conn.execute(sql, params).fetchall()

def _normalize_organize_position(value) -> int | None:
    """防止外部解析器的 list/dict 等值直接进入 SQLite INTEGER 参数。"""
    candidates = value if isinstance(value, (list, tuple, set)) else (value,)
    for candidate in candidates:
        if candidate in (None, "") or isinstance(candidate, bool):
            continue
        try:
            number = int(float(candidate))
        except (TypeError, ValueError, OverflowError):
            continue
        if number >= 0:
            return number
    return None


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
    incomplete = bool(legacy_incomplete) or not bool(original_name and original_parent_id)
    release_parse_json = (
        json.dumps(release_parse, ensure_ascii=False, separators=(",", ":"))
        if isinstance(release_parse, dict) and release_parse else ""
    )

    def insert(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            "INSERT INTO organize_log(source,original_path,new_path,file_id,status,tmdb_id,provider,external_id,"
            "operation_type,source_dir_id,original_parent_id,original_name,current_parent_id,"
            "current_name,target_parent_id,media_type,title,year,season,episode,error,release_parse_json,parent_log_id,"
            "operation_token,version,legacy_incomplete,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source, original_path, new_path, file_id, status, tmdb_id, provider, external_id,
                operation_type, source_dir_id, original_parent_id, original_name,
                current_parent_id, current_name, target_parent_id, media_type, title,
                year, season, episode, error, release_parse_json, parent_log_id,
                str(operation_token or "").strip(), 1, 1 if incomplete else 0,
                timestamp, timestamp,
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
        rows.append((
            log_id, file_id, str(item.get("role") or "metadata"),
            str(item.get("original_parent_id") or ""), str(item.get("original_name") or ""),
            str(item.get("current_parent_id") or ""), str(item.get("current_name") or ""),
            str(item.get("target_parent_id") or ""), str(item.get("target_name") or ""),
            int(item.get("size") or 0), str(item.get("etag") or ""),
            str(item.get("status") or "success"), str(item.get("error") or ""),
            timestamp, timestamp,
        ))
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
) -> int:
    """结束仍待人工处理的整理日志；用于取消或不可重试失败。"""
    normalized_status = str(status or "").strip()
    if normalized_status not in {"skipped", "failed"}:
        raise ValueError("不支持的人工确认终态")
    normalized_source = str(source or "").strip()
    normalized_ids = list(dict.fromkeys(
        str(item or "").strip() for item in file_ids if str(item or "").strip()
    ))
    if not normalized_source or not normalized_ids:
        return 0
    placeholders = ",".join("?" for _ in normalized_ids)
    stamp = now()
    message = str(error or "").strip()
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
            f"UPDATE organize_log SET status=?,error=?,version=version+1,updated_at=? "
            f"WHERE id IN ({log_placeholders})",
            (normalized_status, message, stamp, *log_ids),
        )
        conn.execute(
            f"UPDATE organize_log_items SET status=?,error=?,updated_at=? "
            f"WHERE log_id IN ({log_placeholders}) AND status IN ('manual','skipped')",
            (normalized_status, message, stamp, *log_ids),
        )
        return len(log_ids)


def _organize_log_filters(status: str | None = None, keyword: str = "") -> tuple[str, list]:
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


def list_organize_logs(status: str | None = None, keyword: str = "",
                       limit: int = 20, offset: int = 0) -> list[sqlite3.Row]:
    filters, params = _organize_log_filters(status, keyword)
    sql = "SELECT * FROM organize_log" + filters + " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, int(limit)), max(0, int(offset))])
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def latest_organize_log_id() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id),0) AS id FROM organize_log").fetchone()
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
        return int(conn.execute(
            "SELECT COUNT(*) FROM organize_log" + filters, params
        ).fetchone()[0])


_TIMELINE_STATUS_VALUES = {"success", "failed", "skipped", "reverted", "processing", "manual"}
_TIMELINE_ORIGIN_VALUES = {"all", "guangya", "local"}


def _organize_timeline_query(*, owner: str = "admin", origin: str = "all",
                             status: str = "", keyword: str = "") -> tuple[str, list[object]]:
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
                '' AS completed_at, l.version AS version, l.legacy_incomplete AS legacy_incomplete
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
                0 AS legacy_incomplete
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
    if normalized_status:
        sql += " AND status=?"
        params.append(normalized_status)
    clean_keyword = str(keyword or "").strip()
    if clean_keyword:
        value = f"%{clean_keyword}%"
        sql += " AND (original_path LIKE ? OR original_name LIKE ? OR new_path LIKE ? OR current_name LIKE ? OR title LIKE ? OR error LIKE ? OR warning LIKE ? OR source_label LIKE ?)"
        params.extend([value] * 8)
    return sql, params


def list_organize_timeline(*, owner: str = "admin", origin: str = "all", status: str = "",
                           keyword: str = "", limit: int = 20, offset: int = 0) -> list[sqlite3.Row]:
    sql, params = _organize_timeline_query(owner=owner, origin=origin, status=status, keyword=keyword)
    sql += " ORDER BY updated_at DESC, origin DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, int(limit)), max(0, int(offset))])
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def count_organize_timeline(*, owner: str = "admin", origin: str = "all", status: str = "",
                            keyword: str = "") -> int:
    sql, params = _organize_timeline_query(owner=owner, origin=origin, status=status, keyword=keyword)
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
        total = int(conn.execute(f"SELECT COUNT(*) FROM ({base_sql})", params).fetchone()[0])
        origin_rows = conn.execute(
            f"SELECT origin, COUNT(*) AS count FROM ({base_sql}) GROUP BY origin", params
        ).fetchall()
        status_rows = conn.execute(
            f"SELECT status, COUNT(*) AS count FROM ({base_sql}) GROUP BY status", params
        ).fetchall()
        rows = conn.execute(
            "SELECT origin,status,title,media_type,year,season,episode,updated_at "
            f"FROM ({base_sql}) ORDER BY updated_at DESC, origin DESC, id DESC LIMIT ?",
            [*params, safe_limit + 1],
        ).fetchall()

    return {
        "total": total,
        "by_origin": {str(row["origin"]): int(row["count"] or 0) for row in origin_rows},
        "by_status": {str(row["status"]): int(row["count"] or 0) for row in status_rows},
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
            "WHEN 'video' THEN 0 ELSE 1 END,id", (log_id,)
        ).fetchall()


def list_organize_operation_steps(log_id: int, limit: int = 300) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_operation_steps WHERE log_id=? ORDER BY id DESC LIMIT ?",
            (log_id, max(1, min(int(limit or 300), 1000))),
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
    entries: list[dict], next_status: str, allowed_statuses: tuple[str, ...],
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
                cur = conn.execute(sql, (
                    next_status, token, now(), int(entry["log_id"]),
                    *statuses, int(entry["expected_version"]),
                ))
                if cur.rowcount != 1:
                    raise _BatchClaimRejected("batch claim rejected")
    except _BatchClaimRejected:
        return False
    return True

def update_organize_log(log_id: int, **fields) -> bool:
    allowed = {
        "status", "new_path", "current_parent_id", "current_name", "target_parent_id",
        "tmdb_id", "provider", "external_id", "media_type", "title", "year", "season", "episode", "error",
        "operation_type", "operation_token", "legacy_incomplete",
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
        "current_parent_id", "current_name", "target_parent_id", "target_name",
        "status", "error", "size", "etag",
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


def add_organize_operation_step(
    log_id: int,
    operation_token: str,
    step_index: int,
    action: str,
    **fields,
) -> int:
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO organize_operation_steps(log_id,operation_token,step_index,action,file_id,"
            "from_parent_id,from_name,to_parent_id,to_name,status,error,started_at,finished_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                log_id, operation_token, int(step_index), action,
                str(fields.get("file_id") or ""), str(fields.get("from_parent_id") or ""),
                str(fields.get("from_name") or ""), str(fields.get("to_parent_id") or ""),
                str(fields.get("to_name") or ""), str(fields.get("status") or "pending"),
                str(fields.get("error") or ""), fields.get("started_at") or timestamp,
                fields.get("finished_at"),
            ),
        )
        return int(cur.lastrowid)


def finish_organize_operation_step(step_id: int, status: str, error: str = "") -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE organize_operation_steps SET status=?,error=?,finished_at=? WHERE id=?",
            (status, error, now(), step_id),
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


def update_organize_log_status(log_id: int, status: str) -> None:
    """兼容旧调用；新写操作优先使用原子认领和 update_organize_log。"""
    update_organize_log(log_id, status=status)


def clear_organize_logs() -> dict[str, int]:
    """清理可安全删除的光鸭与本地整理记录，不触碰任何媒体文件。"""
    guangya_busy_statuses = ("reorganizing", "returning", "reverting", "deleting")
    local_busy_statuses = (
        "waiting_stable", "recognizing", "planned", "moving",
        "verifying", "refreshing", "rolling_back",
    )
    guangya_placeholders = ",".join("?" for _ in guangya_busy_statuses)
    local_placeholders = ",".join("?" for _ in local_busy_statuses)
    with get_conn() as conn:
        guangya_busy = int(conn.execute(
            f"SELECT COUNT(*) FROM organize_log WHERE status IN ({guangya_placeholders})",
            guangya_busy_statuses,
        ).fetchone()[0])
        local_busy = int(conn.execute(
            f"SELECT COUNT(*) FROM local_media_tasks WHERE status IN ({local_placeholders})",
            local_busy_statuses,
        ).fetchone()[0])

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
            deleted_guangya = int(conn.execute(
                f"DELETE FROM organize_log WHERE id IN ({id_placeholders})",
                log_ids,
            ).rowcount or 0)

        deleted_local = int(conn.execute(
            f"DELETE FROM local_media_tasks WHERE status NOT IN ({local_placeholders})",
            local_busy_statuses,
        ).rowcount or 0)

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
            "success": 0, "failed": 0, "skipped": 0, "reverted": 0,
            "interrupted": 0, "partial_failed": 0, "revert_failed": 0,
            "reorganizing": 0, "returning": 0, "reverting": 0,
            "deleting": 0, "deleted": 0,
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
# 兼容门面：管理 API 继续使用 app.database.*；识别业务通过 Repository 访问。
from app.repositories.recognition import (  # noqa: E402
    delete_tmdb_lock,
    get_tmdb_lock,
    list_tmdb_locks,
    upsert_tmdb_lock,
)


# ===== 下载日志与统一下载请求 =====
# 兼容门面：调用方继续使用 app.database.*；连接状态与迁移仍由本模块持有。
from app.repositories.download_requests import (  # noqa: E402
    _DOWNLOAD_ATTENTION_WHERE,
    add_download_log,
    bind_media_download_admission_request,
    bind_pending_download_request_owner,
    claim_failed_share_transfer_request,
    clear_download_request_attention,
    clear_download_request_attentions,
    count_download_requests_requiring_attention,
    count_download_logs,
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


# ===== Agent 下载结果自动复核 =====
# 兼容门面：终态与通知发件箱仍保持单事务写入。
from app.repositories.agent_download_verification import (  # noqa: E402
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
    retry_agent_download_verification_notification,
    renew_agent_download_verification_lease,
    update_agent_download_verification,
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
            "organize_operation_jobs": max(0, int(organize_jobs.rowcount)),
            "missing_media_workflows": max(0, int(workflows.rowcount)),
            "web_search_usage": max(0, int(web_usage.rowcount)),
        }


def purge_agent_subject_data(*, owner: str, principal: str | None = None) -> dict[str, int]:
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
        "action_history": digest(b"mediaflux-agent-action-history:v1\0", normalized_owner),
        "session_context": digest(b"mediaflux-agent-session-context:v1\0", normalized_owner),
        "confirmations": digest(b"mediaflux-agent-confirmation:v1\0", normalized_owner),
        "jobs": digest(b"mediaflux-agent-durable-job:v1\0", normalized_owner),
        "workflows": digest(b"mediaflux-agent-missing-workflow:v1\0", normalized_owner),
        "telegram_actions": digest(b"mediaflux-telegram-agent-action:v1\0", normalized_owner),
        "telegram_confirmations": digest(b"mediaflux-telegram-write-confirmation:v1\0", normalized_owner),
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
            ("telegram_confirmations", "telegram_write_confirmations", "owner_digest"),
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
        deleted["organize_operation_jobs"] = (
            max(0, int(running.rowcount or 0))
            + max(0, int(removable.rowcount or 0))
        )
    return deleted


def maintain_sqlite_database(*, incremental_pages: int = 200) -> dict[str, int | bool]:
    """低频执行 SQLite planner 优化，并在 incremental 模式下回收空闲页。"""
    pages = max(1, min(int(incremental_pages), 2000))
    with get_conn() as conn:
        conn.execute("PRAGMA optimize")
        auto_vacuum = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
        freelist_before = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        vacuumed = auto_vacuum == 2 and freelist_before >= pages
        if vacuumed:
            conn.execute(f"PRAGMA incremental_vacuum({pages})")
        freelist_after = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    return {
        "optimized": True,
        "incremental_vacuum": vacuumed,
        "freelist_before": freelist_before,
        "freelist_after": freelist_after,
    }


# ===== Agent 媒体库巡检 =====
# 兼容门面：巡检结果、版本与通知发件箱保持单事务一致性。
from app.repositories.agent_library_patrol import (  # noqa: E402
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


# ===== Agent owner 隔离的可恢复长任务 =====
from app.repositories.agent_jobs import (  # noqa: E402
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


# ===== 下载请求认领与本地入库状态 =====
from app.repositories.download_requests import (  # noqa: E402
    claim_download_request,
    claim_download_request_notification,
    claim_download_request_organize,
    claim_download_request_targets,
    get_download_request_by_request_key,
    get_download_request_by_request_keys,
    link_download_request_to_local_media_task,
    list_active_download_requests,
    mark_download_request_local_media_failed,
    mark_download_request_local_media_skipped,
    finalize_download_request_notification,
    renew_download_request_notification_lease,
    update_download_request,
    update_download_request_and_sync_media_admission,
    update_download_request_for_local_media_task,
)


# ===== RSS 订阅、条目状态机与诊断 =====
# 兼容门面：schema/migration 留在 init_db，调用方继续使用 app.database.*。
from app.repositories.rss import (  # noqa: E402
    add_rss_entry,
    add_rss_entry_with_media,
    add_rss_subscription,
    claim_pending_rss_qb_entries,
    claim_retryable_failed_rss_qb_entries,
    claim_rss_entry,
    claim_rss_guangya_download,
    claim_rss_qb_download,
    finalize_rss_guangya_download,
    finalize_rss_qb_download,
    count_rss_downloaded_entries_since,
    delete_rss_subscription,
    find_rss_subscriptions_by_normalized_name,
    get_pending_rss_qb_snapshot,
    get_retryable_failed_rss_qb_snapshot,
    get_rss_diagnostic_summary,
    get_rss_manual_review_summary,
    get_rss_subscription_safe_summary,
    get_rss_entry,
    get_rss_stats,
    get_rss_subscription,
    list_enabled_rss_subscription_safe_targets,
    list_enabled_rss_subscriptions,
    list_due_rss_subscriptions,
    list_rss_entries,
    list_rss_subscription_safe_summaries,
    list_rss_subscriptions,
    purge_processed_rss_entries,
    recover_stale_submitting_rss_entries,
    record_rss_entry_failure,
    skip_pending_rss_entries,
    update_rss_entries_processed,
    update_rss_entries_processed_snapshot,
    update_rss_entry_status,
    update_rss_subscription,
)


# ===== 媒体订阅、候选资源与下载准入 =====
# 独立于 RSS schema；统一订阅中心只在应用层聚合。
from app.repositories.media_subscriptions import (  # noqa: E402
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
    replace_media_subscription_candidates,
    recover_stale_media_subscription_checks,
    reconcile_media_download_admissions,
    reconcile_startup_media_download_admissions,
    sync_media_download_admission_for_request,
    update_media_download_admission,
    update_media_subscription_config,
    update_media_subscription_candidate,
    upsert_media_subscription,
)


# ===== STRM 索引 =====
# 兼容门面：调用方继续使用 app.database.*；核心索引 CRUD 按业务域拆分。
from app.repositories.strm import (  # noqa: E402
    cancel_stale_strm_metadata_jobs,
    cancel_retired_strm_metadata_jobs,
    cancel_strm_metadata_job,
    claim_due_strm_metadata_jobs,
    claim_strm_change_targets,
    complete_strm_metadata_job,
    complete_strm_change_target,
    count_due_strm_change_targets,
    count_strm_metadata_jobs,
    count_strm_metadata_refresh_paths,
    count_pending_strm_change_targets,
    delete_strm_index_ids,
    enqueue_strm_metadata_jobs,
    enqueue_strm_change_targets,
    fail_or_retry_strm_metadata_job,
    fail_strm_change_target,
    group_changes_by_target,
    list_strm_metadata_queue,
    list_strm_metadata_refresh_paths,
    list_strm_change_queue,
    list_strm_index,
    list_strm_index_by_prefix,
    list_strm_indexes_by_file_id,
    merge_strm_changes,
    recover_stale_strm_metadata_jobs,
    recover_stale_strm_change_targets,
    reschedule_strm_change_targets,
    seconds_until_next_strm_change_target,
    renew_strm_change_target_leases,
    renew_strm_metadata_job_lease,
    acknowledge_strm_metadata_refresh_paths,
    requeue_strm_metadata_jobs,
    release_strm_change_targets,
    strm_metadata_job_is_current,
    upsert_strm_index,
    upsert_strm_index_batch,
)


def _ensure_strm_retired_sources_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS strm_retired_sources ("
        "source_id TEXT PRIMARY KEY,source_name TEXT DEFAULT '',"
        "strm_root TEXT NOT NULL DEFAULT '',queued_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,"
        "last_error TEXT DEFAULT '')"
    )


@contextmanager
def reconcile_strm_retired_sources_transaction(
    active_source_ids: Iterable[object],
    retired_sources: Iterable[tuple[object, object, object]],
):
    """在调用方配置发布期间暂存 STRM 来源退役变更。

    调用方必须在 ``with`` 块内完成配置文件发布。配置发布抛错时，SQLite
    变更随上下文一并回滚；只有配置发布成功且上下文正常退出后才提交退役队列。
    """
    active_ids = tuple(
        dict.fromkeys(str(item).strip() for item in active_source_ids if str(item).strip())
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

    timestamp = now()
    with get_conn() as conn:
        _ensure_strm_retired_sources_table(conn)
        if active_ids:
            placeholders = ",".join("?" for _ in active_ids)
            conn.execute(
                f"DELETE FROM strm_retired_sources WHERE source_id IN ({placeholders})",
                active_ids,
            )
        for source_id, (source_name, strm_root) in normalized_retired.items():
            conn.execute(
                "INSERT INTO strm_retired_sources(source_id,source_name,strm_root,queued_at,"
                "updated_at,attempts,last_error) VALUES(?,?,?,?,?,0,'') "
                "ON CONFLICT(source_id) DO UPDATE SET source_name=excluded.source_name,"
                "strm_root=excluded.strm_root,updated_at=excluded.updated_at,last_error=''",
                (source_id, source_name, strm_root, timestamp, timestamp),
            )
        yield list(normalized_retired)


def enqueue_strm_retired_source(source_id: str, source_name: str, strm_root: str) -> None:
    timestamp = now()
    with get_conn() as conn:
        _ensure_strm_retired_sources_table(conn)
        conn.execute(
            "INSERT INTO strm_retired_sources(source_id,source_name,strm_root,queued_at,"
            "updated_at,attempts,last_error) VALUES(?,?,?,?,?,0,'') "
            "ON CONFLICT(source_id) DO UPDATE SET source_name=excluded.source_name,"
            "strm_root=excluded.strm_root,updated_at=excluded.updated_at,last_error=''",
            (str(source_id), str(source_name or ""), str(strm_root or ""), timestamp, timestamp),
        )


def cancel_strm_retired_sources(source_ids: list[str]) -> int:
    ids = list(dict.fromkeys(str(item) for item in source_ids if str(item)))
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with get_conn() as conn:
        _ensure_strm_retired_sources_table(conn)
        cur = conn.execute(
            f"DELETE FROM strm_retired_sources WHERE source_id IN ({placeholders})", ids
        )
        return cur.rowcount


def list_strm_retired_sources() -> list[sqlite3.Row]:
    with get_conn() as conn:
        _ensure_strm_retired_sources_table(conn)
        return conn.execute(
            "SELECT * FROM strm_retired_sources ORDER BY queued_at, source_id"
        ).fetchall()


def update_strm_retired_source_error(source_id: str, error: str) -> None:
    with get_conn() as conn:
        _ensure_strm_retired_sources_table(conn)
        conn.execute(
            "UPDATE strm_retired_sources SET attempts=attempts+1,last_error=?,updated_at=? "
            "WHERE source_id=?",
            (str(error or "")[:500], now(), str(source_id)),
        )


def delete_strm_retired_source(source_id: str) -> int:
    with get_conn() as conn:
        _ensure_strm_retired_sources_table(conn)
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
    *, source_id: str, source_name: str, file_id: str, parent_id: str,
    filename: str, action: str, rel_dir: str, target_rel_path: str, error: object,
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
            (str(source_id), str(source_name or ""), str(file_id), str(parent_id or ""),
             str(filename), action, str(rel_dir or ""), str(target_rel_path or ""),
             _sanitize_strm_failure_error(error), timestamp, timestamp),
        )
        row = conn.execute(
            "SELECT id FROM strm_failures WHERE source_id=? AND file_id=? AND action=?",
            (str(source_id), str(file_id), action),
        ).fetchone()
        return int(row["id"])


def list_strm_failures(
    *, status: str = "open", source_id: str = "", action: str = "",
    ids: list[int] | None = None, before_id: int | None = None, limit: int = 200,
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
            f"SELECT * FROM strm_failures{where} ORDER BY id DESC LIMIT ?{offset_sql}", values
        ).fetchall()


def get_strm_failure_retry_snapshot(
    *, action: str = "", limit: int = 101,
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
    normalized = list(dict.fromkeys(int(item) for item in ids))[:max(1, int(limit))]
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
        return int(conn.execute(
            "SELECT COUNT(*) AS count FROM strm_failures" + where, values
        ).fetchone()["count"])


def summarize_strm_failures() -> dict:
    with get_conn() as conn:
        open_count = int(conn.execute(
            "SELECT COUNT(*) AS count FROM strm_failures WHERE status='open'"
        ).fetchone()["count"])
        resolved_count = int(conn.execute(
            "SELECT COUNT(*) AS count FROM strm_failures WHERE status='resolved'"
        ).fetchone()["count"])
        rows = conn.execute(
            "SELECT source_id,MAX(source_name) AS source_name,COUNT(*) AS count "
            "FROM strm_failures WHERE status='open' GROUP BY source_id ORDER BY source_name,source_id"
        ).fetchall()
    sources = [
        {"id": str(row["source_id"]), "name": str(row["source_name"] or row["source_id"]),
         "open": int(row["count"])}
        for row in rows
    ]
    return {
        "open": open_count, "resolved": resolved_count, "total": open_count + resolved_count,
        "by_source": {item["id"]: item["open"] for item in sources}, "sources": sources,
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
        summary["active_repeated"] = int(summary["active_repeated"]) + max(0, int(row["active_repeated"] or 0))
        summary["active_retried"] = int(summary["active_retried"]) + max(0, int(row["active_retried"] or 0))
    return summary


def resolve_strm_failure(
    failure_id: int, *, expected_status: str = "retrying"
) -> bool:
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE strm_failures SET status='resolved',updated_at=?,resolved_at=? "
            "WHERE id=? AND status=?",
            (timestamp, timestamp, int(failure_id), str(expected_status)),
        )
        return cur.rowcount == 1


def resolve_strm_failure_for_item(source_id: str, file_id: str, action: str) -> int:
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE strm_failures SET status='resolved',updated_at=?,resolved_at=? "
            "WHERE source_id=? AND file_id=? AND action=? AND status='open'",
            (timestamp, timestamp, str(source_id), str(file_id), str(action)),
        )
        return cur.rowcount


def resolve_strm_failures_for_items(
    source_id: str, file_ids: Iterable[str], action: str, *, chunk_size: int = 400,
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
            chunk = ids[offset:offset + safe_chunk]
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
            (_sanitize_strm_failure_error(error), now(), int(failure_id),
             str(expected_status)),
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
            (_sanitize_strm_failure_error(error), now(), int(failure_id),
             str(expected_status)),
        )
        return cur.rowcount == 1


def update_strm_failure_retry(
    failure_id: int, *, source_id: str, source_name: str, file, rel_dir: str,
    target_rel_path: str, error: object, expected_status: str = "retrying",
) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE strm_failures SET source_id=?,source_name=?,parent_id=?,filename=?,"
            "rel_dir=?,target_rel_path=?,error=?,status='open',"
            "failure_count=failure_count+1,updated_at=?,resolved_at=NULL "
            "WHERE id=? AND status=?",
            (str(source_id), str(source_name or ""), str(file.parent_id or ""),
             str(file.name), str(rel_dir or ""), str(target_rel_path or ""),
             _sanitize_strm_failure_error(error), now(), int(failure_id),
             str(expected_status)),
        )
        return cur.rowcount == 1


def delete_strm_failures(
    *, ids: list[int] | None = None, all_items: bool = False,
    status: str = "", source_id: str = "", action: str = "",
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
            return source[len(prefix):]
    return source


def _resolve_strm_index_path(strm_path: str, strm_root: str) -> Path:
    path = Path(str(strm_path or "")).expanduser()
    if not path.is_absolute():
        path = Path(str(strm_root or "")).expanduser() / path
    return path.resolve(strict=False)


def _classify_strm_index_row(
    row: sqlite3.Row, strm_root: str, configured_source_ids: set[str]
) -> dict[str, bool]:
    resolved_path = _resolve_strm_index_path(row["strm_path"], strm_root)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    try:
        in_temp_dir = resolved_path.is_relative_to(temp_root)
    except AttributeError:  # pragma: no cover - Python < 3.9 compatibility
        in_temp_dir = temp_root == resolved_path or temp_root in resolved_path.parents
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
    if source.startswith("guangya-meta:") or source.startswith("local-meta:") or "-meta:" in source:
        return "metadata"
    if source.startswith("guangya:") or source.startswith("local:") or source.startswith("115:") or source.startswith("quark:"):
        return "video"
    return "other"


def list_strm_index_diagnostics(strm_root: str) -> dict:
    """只读统计 STRM 索引状态，仅返回安全计数和确认测试行 ID。"""
    configured_source_ids = _configured_strm_source_ids()
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
        classification = _classify_strm_index_row(
            row, strm_root, configured_source_ids
        )
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
    normalized_ids: list[int] = []
    for item in ids:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ValueError("索引 ID 必须为正整数")
        if item not in normalized_ids:
            normalized_ids.append(item)
    if not normalized_ids:
        return 0

    from app import config

    strm_root = config.get("STRM_ROOT", "")
    configured_source_ids = _configured_strm_source_ids()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for row_id in normalized_ids:
            row = conn.execute(
                "SELECT id,source,strm_path FROM strm_index WHERE id=?",
                (row_id,),
            ).fetchone()
            if row is None or not _classify_strm_index_row(
                row, strm_root, configured_source_ids
            )["confirmed_test_artifact"]:
                raise ValueError("请求包含非确认测试索引，已拒绝整个清理请求")
        placeholders = ",".join("?" for _ in normalized_ids)
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
) -> int:
    """持久化一组 Telegram 整理候选，并使同指纹旧按钮失效。"""
    timestamp = now()
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE organize_confirmations SET status='expired',updated_at=? "
            "WHERE fingerprint=? AND status='pending'",
            (timestamp, str(fingerprint)),
        )
        cursor = conn.execute(
            "INSERT INTO organize_confirmations("
            "token,fingerprint,chat_id,source_name,directory_path,payload_json,status,"
            "expires_at,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,'pending',?,?,?)",
            (
                str(token), str(fingerprint), str(chat_id or ""),
                str(source_name or ""), str(directory_path or ""), encoded,
                str(expires_at), timestamp, timestamp,
            ),
        )
        return int(cursor.lastrowid)


def get_organize_confirmation(token: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE token=?",
            (str(token or ""),),
        ).fetchone()


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
    token: str, *, chat_id: str, selected_index: int
) -> sqlite3.Row:
    """原子认领待确认按钮；过期、越权和重放统一拒绝。"""
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
                "UPDATE organize_confirmations SET status='expired',updated_at=? WHERE id=?",
                (timestamp, int(row["id"])),
            )
            claimed = None
        else:
            cursor = conn.execute(
                "UPDATE organize_confirmations SET status='queued',selected_index=?,"
                "queued_at=?,updated_at=? WHERE id=? AND status='pending'",
                (int(selected_index), queued_at, timestamp, int(row["id"])),
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
                "UPDATE organize_confirmations SET status='expired',updated_at=? WHERE id=?",
                (timestamp, int(row["id"])),
            )
            cancelled = None
        else:
            cursor = conn.execute(
                "UPDATE organize_confirmations SET status='cancelled',selected_index=NULL,"
                "queued_at=NULL,task_id='',result_json=?,error='',completed_at=?,updated_at=? "
                "WHERE id=? AND status='pending'",
                (
                    json.dumps({"cancelled": True}, ensure_ascii=False),
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
                _enqueue_organize_confirmation_delivery(
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


def expire_queued_organize_confirmations() -> int:
    """原子失效已经超过确认窗口、但尚未开始执行的排队任务。"""
    timestamp = now()
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE organize_confirmations SET status='expired',error='确认操作已过期',"
            "completed_at=?,updated_at=? WHERE status='queued' AND expires_at<=?",
            (timestamp, timestamp, timestamp),
        )
        return int(cursor.rowcount)


def get_next_queued_organize_confirmation() -> sqlite3.Row | None:
    """返回最早且仍在有效期内的 Telegram 整理任务。"""
    expire_queued_organize_confirmations()
    timestamp = now()
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_confirmations WHERE status='queued' AND expires_at>? "
            "ORDER BY COALESCE(queued_at,created_at) ASC,id ASC LIMIT 1",
            (timestamp,),
        ).fetchone()


def get_organize_confirmation_queue_position(row_id: int) -> int:
    """按用户点击入队时间返回前方任务数量，并计入当前运行项。"""
    expire_queued_organize_confirmations()
    timestamp = now()
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
            "WHERE id<>? AND (status='running' OR (status='queued' AND expires_at>? AND ("
            "COALESCE(queued_at,created_at)<? OR "
            "(COALESCE(queued_at,created_at)=? AND id<?))))",
            (
                int(row_id), timestamp, str(target["queue_time"] or ""),
                str(target["queue_time"] or ""), int(row_id),
            ),
        ).fetchone()
    return int(row["total"] or 0) if row is not None else 0


def claim_queued_organize_confirmation(token: str) -> sqlite3.Row | None:
    """原子领取一个已排队确认；跨进程竞争时仅一个调度器成功。"""
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE organize_confirmations SET status='expired',error='确认操作已过期',"
            "completed_at=?,updated_at=? WHERE token=? AND status='queued' AND expires_at<=?",
            (timestamp, timestamp, str(token or ""), timestamp),
        )
        cursor = conn.execute(
            "UPDATE organize_confirmations SET status='running',error='',updated_at=? "
            "WHERE token=? AND status='queued' AND expires_at>?",
            (timestamp, str(token or ""), timestamp),
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
    allowed = {"status", "task_id", "result_json", "error", "completed_at"}
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


def _enqueue_organize_confirmation_delivery(
    conn: sqlite3.Connection,
    *,
    token: str,
    event_json: str,
    chat_id: str,
    message_id: int | None,
    timestamp: str,
) -> None:
    conn.execute(
        "INSERT INTO organize_confirmation_delivery_outbox("
        "confirmation_token,event_json,chat_id,message_id,status,attempts,"
        "lease_generation,next_attempt_at,last_error,sent_at,created_at,updated_at"
        ") VALUES(?,?,?,?,'pending',0,0,?,'',NULL,?,?) "
        "ON CONFLICT(confirmation_token) DO UPDATE SET "
        "event_json=excluded.event_json,chat_id=excluded.chat_id,"
        "message_id=excluded.message_id,status='pending',attempts=0,"
        "lease_generation=organize_confirmation_delivery_outbox.lease_generation+1,"
        "next_attempt_at=excluded.next_attempt_at,last_error='',sent_at=NULL,"
        "updated_at=excluded.updated_at",
        (
            str(token or ""), str(event_json or "{}"), str(chat_id or ""),
            int(message_id) if message_id else None,
            timestamp, timestamp, timestamp,
        ),
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
    """原子保存成功终态；升级前回执队列可按需继续写入。"""
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE organize_confirmations SET status='completed',result_json=?,error='',"
            "completed_at=?,updated_at=? WHERE token=? AND status='running'",
            (str(result_json or "{}"), timestamp, timestamp, str(token or "")),
        )
        if cursor.rowcount != 1:
            raise ValueError("确认操作不存在或已失效")
        if enqueue_delivery:
            _enqueue_organize_confirmation_delivery(
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
) -> None:
    """原子保存失败/可重试状态与待投递回执。"""
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if retryable:
            cursor = conn.execute(
                "UPDATE organize_confirmations SET status='pending',selected_index=NULL,"
                "queued_at=NULL,task_id='',error=?,completed_at=NULL,updated_at=? "
                "WHERE token=? AND status IN ('queued','running')",
                (str(error or ""), timestamp, str(token or "")),
            )
        else:
            cursor = conn.execute(
                "UPDATE organize_confirmations SET status='failed',error=?,completed_at=?,"
                "updated_at=? WHERE token=? AND status IN ('queued','running')",
                (str(error or ""), timestamp, timestamp, str(token or "")),
            )
        if cursor.rowcount != 1:
            raise ValueError("确认操作不存在或已失效")
        if enqueue_delivery:
            _enqueue_organize_confirmation_delivery(
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
            (str(current_time), int(row["id"]), int(row["lease_generation"]), str(row["status"])),
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
            (str(sent_at), str(sent_at), int(delivery_id), int(expected_lease_generation)),
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
                str(next_attempt_at), str(error or "DeliveryFailed")[:500], now(),
                int(delivery_id), int(expected_lease_generation),
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
    "previewed", "running", "success", "partial_success", "failed",
}
_GCID_IMPORT_ITEM_STATUSES = {"previewed", "running", "success", "failed"}


def create_gcid_import_task(*, operation_token: str, manifest_digest: str,
                            target_dir_id: str, file_count: int,
                            total_size: int) -> int:
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
        normalized.append((
            int(task_id), path, size, gcid, status,
            str(item.get("remote_file_id") or "").strip(),
            str(item.get("error") or "")[:1000], timestamp, timestamp,
        ))
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM gcid_import_tasks WHERE id=?", (int(task_id),)
        ).fetchone() is None:
            raise ValueError("GCID 导入任务不存在")
        conn.execute("DELETE FROM gcid_import_items WHERE task_id=?", (int(task_id),))
        conn.executemany(
            "INSERT INTO gcid_import_items("
            "task_id,path,size,gcid,status,remote_file_id,error,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            normalized,
        )


def update_gcid_import_task(task_id: int, *, status: str | None = None,
                            success_count: int | None = None,
                            failed_count: int | None = None,
                            error: str | None = None) -> None:
    updates: list[str] = []
    params: list = []
    if status is not None:
        normalized_status = str(status).strip()
        if normalized_status not in _GCID_IMPORT_TASK_STATUSES:
            raise ValueError("GCID 导入任务状态无效")
        updates.append("status=?")
        params.append(normalized_status)
    for key, value in (("success_count", success_count), ("failed_count", failed_count)):
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


def list_gcid_import_items(task_id: int, status: str = "") -> list[sqlite3.Row]:
    sql = "SELECT * FROM gcid_import_items WHERE task_id=?"
    params: list = [int(task_id)]
    if status:
        if status not in _GCID_IMPORT_ITEM_STATUSES:
            raise ValueError("GCID 导入明细状态无效")
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY id"
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


# ===== 后台任务运行记录 =====
def add_task_run(task_name: str, trigger_type: str,
                 status: str = "running", result: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO task_runs(task_name,trigger_type,status,started_at,result) "
            "VALUES(?,?,?,?,?)",
            (task_name, trigger_type, status, now(), result),
        )
        return cur.lastrowid


def finish_task_run(run_id: int, status: str, result: str = "",
                    error: str = "") -> None:
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


def add_agent_action_history(*, owner_digest: str, tool_name: str, risk: str,
                             status: str, ok: bool, summary: str,
                             safe_details: dict | None = None,
                             error_code: str = "", elapsed_ms: int = 0,
                             started_at: str = "", finished_at: str = "",
                             confirmation_id: str = "",
                             connection: sqlite3.Connection | None = None) -> int:
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
    if normalized_error and (len(normalized_error) > 64 or not re.fullmatch(r"[a-z0-9_]+", normalized_error)):
        raise ValueError("Agent 审计错误码无效")
    if normalized_confirmation and (
        len(normalized_confirmation) > 128
        or not re.fullmatch(r"[A-Za-z0-9_-]+", normalized_confirmation)
    ):
        raise ValueError("Agent 审计确认标识无效")
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
    encoded = json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 4096:
        raise ValueError("Agent 审计详情过大")
    elapsed = max(0, min(int(elapsed_ms or 0), 86_400_000))
    finished = str(finished_at or now()).strip()
    started = str(started_at or finished).strip()
    def write(conn: sqlite3.Connection) -> int:
        _ensure_agent_action_history_schema(conn)
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
        total = int(conn.execute(
            "SELECT COUNT(*) FROM agent_action_history"
        ).fetchone()[0] or 0)
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


def list_agent_action_history(*, owner_digest: str, limit: int = 20,
                              outcome: str = "all") -> list[sqlite3.Row]:
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
        downloads_active = int(conn.execute(
            "SELECT COUNT(*) FROM download_requests WHERE "
            "(status IN ('submitting','submitted','downloading') OR "
            "(status='completed' AND gy_status='completed' AND organize_started=0)) AND "
            "COALESCE(qb_status,'') NOT IN ('failed','manual_review') AND "
            "COALESCE(gy_status,'') NOT IN ('failed','manual_review') AND "
            "COALESCE(local_import_status,'')!='failed' AND COALESCE(organize_started,0)>=0"
        ).fetchone()[0])
        downloads_review = int(conn.execute(
            f"SELECT COUNT(*) FROM download_requests WHERE {_DOWNLOAD_ATTENTION_WHERE}"
        ).fetchone()[0])
        rss_subscriptions = int(conn.execute(
            "SELECT COUNT(*) FROM rss_items WHERE enabled=1"
        ).fetchone()[0])
        # 看板「订阅」是全站订阅总量：RSS 订阅源与媒体追更订阅都要计入，
        # 否则只做媒体追更的用户会在顶栏看到 0。
        media_subscriptions = int(conn.execute(
            "SELECT COUNT(*) FROM media_subscriptions WHERE enabled=1"
        ).fetchone()[0])
        rss_row = conn.execute(
            "SELECT "
            "SUM(CASE WHEN status IN ('pending','submitting') THEN 1 ELSE 0 END) AS pending,"
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
            "FROM rss_entries"
        ).fetchone()
        organize_issues = int(conn.execute(
            "SELECT COUNT(*) FROM organize_log WHERE "
            "status IN ('failed','interrupted','partial_failed','revert_failed')"
        ).fetchone()[0])
        strm_failures = int(conn.execute(
            "SELECT COUNT(*) FROM strm_failures WHERE status='open'"
        ).fetchone()[0])
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
        "total", "pending", "running", "retry_wait", "visible", "attention",
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
            "checked_series_count": max(0, int(patrol_row["checked_series_count"] or 0)),
            "updates_available_count": max(0, int(patrol_row["updates_available_count"] or 0)),
            "missing_episode_count": max(0, int(patrol_row["missing_episode_count"] or 0)),
            "inconclusive_count": max(0, int(patrol_row["inconclusive_count"] or 0)),
            "unmapped_series_count": max(0, int(patrol_row["unmapped_series_count"] or 0)),
            "findings_truncated": bool(patrol_row["findings_truncated"]),
        }
    return {"download_verification": verification, "library_patrol": patrol}


# ===== 媒体探索缓存、跨来源映射与收藏 =====
# 兼容门面：外部调用和测试继续使用 app.database.*；实现已按业务域拆分。
from app.repositories.discovery import (  # noqa: E402, F401
    add_media_watchlist,
    confirm_media_external_id_if_unchanged,
    delete_media_watchlist,
    get_discovery_cache,
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
def _local_media_owner(owner: str) -> str:
    value = str(owner or "").strip()
    if not value:
        raise ValueError("owner 不能为空")
    return value


def create_local_media_source(
    name: str,
    qb_profile: str,
    qb_path_prefix: str,
    local_root: str,
    enabled: int = 1,
    stable_seconds: int = 300,
    scan_enabled: int = 0,
    scan_interval_minutes: int = 10,
    *,
    owner: str = "admin",
    media_type: str = "auto",
    mode: str = "move",
    smb_user: str = "",
    smb_pass: str = "",
) -> int:
    safe_owner = _local_media_owner(owner)
    safe_name = str(name or "").strip()
    safe_root = str(local_root or "").strip()
    safe_mode = str(mode or "move").strip().lower()
    safe_media_type = str(media_type or "auto").strip().lower()
    if not safe_name or not safe_root:
        raise ValueError("本地媒体来源名称和路径不能为空")
    if safe_mode not in {"move", "preview_only"}:
        raise ValueError("本地媒体来源仅支持 move 或 preview_only")
    if safe_media_type not in {"auto", "movie", "tv", "nsfw"}:
        raise ValueError("本地媒体来源类型必须是 auto、movie、tv 或 nsfw")
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO local_media_sources(owner,name,qb_profile,qb_path_prefix,local_root,"
            "smb_user,smb_pass,"
            "enabled,stable_seconds,scan_enabled,scan_interval_minutes,media_type,mode,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                safe_owner, safe_name, str(qb_profile or ""), str(qb_path_prefix or ""), safe_root,
                "", "",
                1 if enabled else 0, max(0, int(stable_seconds)), 1 if scan_enabled else 0,
                max(1, int(scan_interval_minutes)), safe_media_type, safe_mode,
                timestamp, timestamp,
            ),
        )
        return int(cur.lastrowid)


def save_local_media_source_bundle(
    *, source_id: int | None = None, name: str, qb_profile: str, qb_path_prefix: str,
    local_root: str, enabled: bool, stable_seconds: int, scan_enabled: bool,
    scan_interval_minutes: int, media_type: str, mode: str,
    targets: list[dict[str, str]] | None, owner: str = "admin",
    smb_user: str = "", smb_pass: str = "",
) -> int:
    """在单个事务内保存来源与全部分类目标。"""
    from app.modules.local_media_models import LOCAL_MEDIA_CATEGORIES

    safe_owner = _local_media_owner(owner)
    safe_name = str(name or "").strip()
    safe_root = str(local_root or "").strip()
    safe_mode = str(mode or "move").strip().lower()
    safe_media_type = str(media_type or "auto").strip().lower()
    if not safe_name or not safe_root:
        raise ValueError("本地媒体来源名称和路径不能为空")
    if safe_mode not in {"move", "preview_only"}:
        raise ValueError("本地媒体来源仅支持 move 或 preview_only")
    if safe_media_type not in {"auto", "movie", "tv", "nsfw"}:
        raise ValueError("本地媒体来源类型必须是 auto、movie、tv 或 nsfw")
    if any("\x00" in value for value in (safe_root, str(qb_path_prefix or ""))):
        raise ValueError("本地媒体来源路径包含非法字符")
    normalized_targets: list[dict[str, str]] | None = None
    if targets is not None:
        normalized_targets = []
        seen: set[str] = set()
        for item in targets:
            category = str(item.get("category") or "").strip().lower()
            target_path = str(item.get("path") or "").strip()
            if category not in LOCAL_MEDIA_CATEGORIES or category in seen:
                raise ValueError("目标分类无效或重复")
            if not target_path or "\x00" in target_path:
                raise ValueError("媒体库目标路径无效")
            provider = str(item.get("provider") or "").strip().lower()
            library_id = str(item.get("library_id") or "").strip()
            library_name = str(item.get("library_name") or "").strip()
            server_path = str(item.get("server_path") or "").strip()
            if provider not in {"", "jellyfin", "emby"}:
                raise ValueError("目标媒体服务器类型无效")
            if provider and not library_name:
                raise ValueError("媒体服务器和媒体库名称必须同时选择")
            if not provider and (library_id or library_name or server_path):
                raise ValueError("未选择媒体服务器时不能绑定媒体库或服务端路径")
            if server_path:
                from app.modules.media_server_path_mapping import MediaServerPathMapping
                server_path = MediaServerPathMapping(target_path, server_path).server_prefix
            seen.add(category)
            normalized_targets.append({
                "category": category, "path": target_path, "provider": provider,
                "library_id": library_id, "library_name": library_name,
                "server_path": server_path,
            })
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if source_id is None:
            cur = conn.execute(
                "INSERT INTO local_media_sources(owner,name,qb_profile,qb_path_prefix,local_root,"
                "smb_user,smb_pass,"
                "enabled,stable_seconds,scan_enabled,scan_interval_minutes,media_type,mode,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (safe_owner, safe_name, str(qb_profile or "").strip(), str(qb_path_prefix or "").strip(),
                 safe_root, "", "",
                 1 if enabled else 0, max(0, int(stable_seconds)),
                 1 if scan_enabled else 0, max(1, int(scan_interval_minutes)),
                 safe_media_type, safe_mode, timestamp, timestamp),
            )
            saved_id = int(cur.lastrowid)
        else:
            saved_id = int(source_id)
            cur = conn.execute(
                "UPDATE local_media_sources SET name=?,qb_profile=?,qb_path_prefix=?,local_root=?,"
                "smb_user=?,smb_pass=?,"
                "enabled=?,stable_seconds=?,scan_enabled=?,scan_interval_minutes=?,media_type=?,mode=?,updated_at=? "
                "WHERE id=? AND owner=?",
                (safe_name, str(qb_profile or "").strip(), str(qb_path_prefix or "").strip(),
                 safe_root, "", "",
                 1 if enabled else 0, max(0, int(stable_seconds)),
                 1 if scan_enabled else 0, max(1, int(scan_interval_minutes)),
                 safe_media_type, safe_mode, timestamp, saved_id, safe_owner),
            )
            if cur.rowcount != 1:
                raise LookupError("本地媒体来源不存在")
        if normalized_targets is not None:
            conn.execute("DELETE FROM local_library_targets WHERE source_id=? AND owner=?", (saved_id, safe_owner))
            for item in normalized_targets:
                conn.execute(
                    "INSERT INTO local_library_targets(source_id,owner,category,path,provider,library_id,library_name,server_path,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (saved_id, safe_owner, item["category"], item["path"], item["provider"],
                     item["library_id"], item["library_name"], item["server_path"], timestamp, timestamp),
                )
        return saved_id


def get_local_media_source(source_id: int, *, owner: str = "admin"):
    from app.modules.local_media_models import LocalMediaSource

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM local_media_sources WHERE id=? AND owner=?",
            (int(source_id), _local_media_owner(owner)),
        ).fetchone()
    return LocalMediaSource.from_row(row) if row else None


def list_local_media_sources(*, owner: str = "admin", enabled_only: bool = False):
    from app.modules.local_media_models import LocalMediaSource

    sql = "SELECT * FROM local_media_sources WHERE owner=?"
    params: list[object] = [_local_media_owner(owner)]
    if enabled_only:
        sql += " AND enabled=1"
    sql += " ORDER BY id ASC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [LocalMediaSource.from_row(row) for row in rows]


def upsert_local_library_target(
    source_id: int,
    category: str,
    path: str,
    provider: str = "",
    library_name: str = "",
    library_id: str = "",
    server_path: str = "",
    *,
    owner: str = "admin",
) -> int:
    from app.modules.local_media_models import LOCAL_MEDIA_CATEGORIES

    safe_owner = _local_media_owner(owner)
    safe_category = str(category or "").strip().lower()
    safe_path = str(path or "").strip()
    safe_provider = str(provider or "").strip().lower()
    safe_library_id = str(library_id or "").strip()
    safe_library_name = str(library_name or "").strip()
    safe_server_path = str(server_path or "").strip()
    if safe_category not in LOCAL_MEDIA_CATEGORIES:
        raise ValueError("不支持的本地媒体分类")
    if not safe_path:
        raise ValueError("媒体库目标路径不能为空")
    if safe_provider not in {"", "jellyfin", "emby"}:
        raise ValueError("目标媒体服务器类型无效")
    if safe_provider and not safe_library_name:
        raise ValueError("媒体服务器和媒体库名称必须同时选择")
    if not safe_provider and (safe_library_id or safe_library_name or safe_server_path):
        raise ValueError("未选择媒体服务器时不能绑定媒体库或服务端路径")
    if safe_server_path:
        from app.modules.media_server_path_mapping import MediaServerPathMapping
        safe_server_path = MediaServerPathMapping(safe_path, safe_server_path).server_prefix
    timestamp = now()
    with get_conn() as conn:
        source = conn.execute(
            "SELECT id FROM local_media_sources WHERE id=? AND owner=?",
            (int(source_id), safe_owner),
        ).fetchone()
        if not source:
            raise LookupError("本地媒体来源不存在")
        conn.execute(
            "INSERT INTO local_library_targets(source_id,owner,category,path,provider,library_id,library_name,server_path,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,category) DO UPDATE SET "
            "owner=excluded.owner,path=excluded.path,provider=excluded.provider,"
            "library_id=excluded.library_id,library_name=excluded.library_name,"
            "server_path=excluded.server_path,updated_at=excluded.updated_at",
            (
                int(source_id), safe_owner, safe_category, safe_path,
                safe_provider, safe_library_id, safe_library_name, safe_server_path, timestamp, timestamp,
            ),
        )
        row = conn.execute(
            "SELECT id FROM local_library_targets WHERE source_id=? AND category=? AND owner=?",
            (int(source_id), safe_category, safe_owner),
        ).fetchone()
        return int(row["id"])


def list_local_library_targets(source_id: int, *, owner: str = "admin"):
    from app.modules.local_media_models import LocalLibraryTarget

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM local_library_targets WHERE source_id=? AND owner=? ORDER BY category,id",
            (int(source_id), _local_media_owner(owner)),
        ).fetchall()
    return [LocalLibraryTarget.from_row(row) for row in rows]


def replace_local_library_targets(
    bindings: list[dict[str, object]],
    *,
    owner: str = "admin",
) -> None:
    """原子替换当前用户的全部本地归档目标，供统一媒体库页面保存。"""
    from app.modules.local_media_models import LOCAL_MEDIA_CATEGORIES

    safe_owner = _local_media_owner(owner)
    normalized: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for item in bindings:
        source_id = int(item.get("source_id") or 0)
        category = str(item.get("category") or "").strip().lower()
        path = str(item.get("path") or "").strip()
        provider = str(item.get("provider") or "").strip().lower()
        library_id = str(item.get("library_id") or "").strip()
        library_name = str(item.get("library_name") or "").strip()
        server_path = str(item.get("server_path") or "").strip()
        key = (source_id, category)
        if source_id <= 0 or category not in LOCAL_MEDIA_CATEGORIES or key in seen:
            raise ValueError("本地归档目标的来源或分类无效、重复")
        if not path or "\x00" in path:
            raise ValueError("本地归档目标路径无效")
        if provider not in {"", "jellyfin", "emby"}:
            raise ValueError("本地归档目标媒体服务器类型无效")
        if provider and not library_name:
            raise ValueError("媒体服务器和媒体库名称必须同时选择")
        if not provider and (library_id or library_name or server_path):
            raise ValueError("未选择媒体服务器时不能绑定媒体库或服务端路径")
        seen.add(key)
        normalized.append({
            "source_id": source_id,
            "category": category,
            "path": path,
            "provider": provider,
            "library_id": library_id,
            "library_name": library_name,
            "server_path": server_path,
        })

    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_source_ids = {
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM local_media_sources WHERE owner=?",
                (safe_owner,),
            ).fetchall()
        }
        unknown = sorted({int(item["source_id"]) for item in normalized} - existing_source_ids)
        if unknown:
            raise LookupError("本地媒体来源不存在")
        conn.execute("DELETE FROM local_library_targets WHERE owner=?", (safe_owner,))
        for item in normalized:
            conn.execute(
                "INSERT INTO local_library_targets(source_id,owner,category,path,provider,"
                "library_id,library_name,server_path,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    int(item["source_id"]), safe_owner, item["category"], item["path"],
                    item["provider"], item["library_id"], item["library_name"],
                    item["server_path"], timestamp, timestamp,
                ),
            )


def create_local_media_task(
    source_id: int,
    qb_hash: str,
    content_path: str,
    *,
    owner: str = "admin",
    trigger: str = "qb_completed",
    operation_token: str = "",
) -> int:
    import uuid
    from app.modules.local_media_models import LOCAL_MEDIA_TRIGGERS

    safe_owner = _local_media_owner(owner)
    safe_trigger = str(trigger or "").strip().lower()
    safe_path = str(content_path or "").strip()
    normalized_hash = str(qb_hash or "").strip().lower() or None
    if safe_trigger not in LOCAL_MEDIA_TRIGGERS:
        raise ValueError("不支持的本地媒体任务触发方式")
    if not safe_path:
        raise ValueError("本地媒体任务路径不能为空")
    timestamp = now()
    token = str(operation_token or uuid.uuid4().hex)
    with get_conn() as conn:
        source = conn.execute(
            "SELECT id FROM local_media_sources WHERE id=? AND owner=?",
            (int(source_id), safe_owner),
        ).fetchone()
        if not source:
            raise LookupError("本地媒体来源不存在")
        if normalized_hash:
            existing = conn.execute(
                "SELECT id,content_path FROM local_media_tasks WHERE source_id=? AND qb_hash=? AND owner=?",
                (int(source_id), normalized_hash, safe_owner),
            ).fetchone()
            if existing:
                if str(existing["content_path"]) != safe_path:
                    raise ValueError("相同 qB 任务对应的内容路径不一致")
                return int(existing["id"])
        else:
            existing = conn.execute(
                "SELECT id FROM local_media_tasks WHERE source_id=? AND content_path=? AND owner=? "
                "AND status NOT IN ('completed','failed') ORDER BY id DESC LIMIT 1",
                (int(source_id), safe_path, safe_owner),
            ).fetchone()
            if existing:
                return int(existing["id"])
        cur = conn.execute(
            "INSERT INTO local_media_tasks(owner,source_id,qb_hash,content_path,trigger,status,"
            "operation_token,created_at,updated_at) VALUES(?,?,?,?,?,'waiting_stable',?,?,?)",
            (safe_owner, int(source_id), normalized_hash, safe_path, safe_trigger, token, timestamp, timestamp),
        )
        return int(cur.lastrowid)


def create_and_link_qb_local_media_task(
    request_id: int,
    source_id: int,
    qb_hash: str,
    content_path: str,
    *,
    owner: str = "admin",
) -> tuple[int, bool]:
    """原子创建/复用 qB 本地整理任务并绑定下载请求。

    返回 ``(task_id, restarted)``。新下载请求命中同 hash 的旧终态任务时，
    会开启新的 attempt；已经绑定到该任务的请求只复用当前状态，避免跟踪器
    在任务完成后再次把它重置为等待态。
    """
    import uuid

    safe_owner = _local_media_owner(owner)
    safe_path = str(content_path or "").strip()
    normalized_hash = str(qb_hash or "").strip().lower()
    if not safe_path:
        raise ValueError("本地媒体任务路径不能为空")
    if not normalized_hash:
        raise ValueError("qB 任务标识不能为空")

    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        request_row = conn.execute(
            "SELECT local_import_status,local_import_target FROM download_requests WHERE id=?",
            (int(request_id),),
        ).fetchone()
        if request_row is None:
            raise LookupError("下载请求不存在")
        local_status = str(request_row["local_import_status"] or "")
        if local_status not in {"", "pending"}:
            raise ValueError("下载请求的本地入库状态已结束")

        source = conn.execute(
            "SELECT id FROM local_media_sources WHERE id=? AND owner=?",
            (int(source_id), safe_owner),
        ).fetchone()
        if not source:
            raise LookupError("本地媒体来源不存在")

        existing = conn.execute(
            "SELECT id,content_path,status FROM local_media_tasks "
            "WHERE source_id=? AND qb_hash=? AND owner=?",
            (int(source_id), normalized_hash, safe_owner),
        ).fetchone()
        restarted = False
        if existing is None:
            cur = conn.execute(
                "INSERT INTO local_media_tasks("
                "owner,source_id,qb_hash,content_path,trigger,status,operation_token,created_at,updated_at"
                ") VALUES(?,?,?,?,?,'waiting_stable',?,?,?)",
                (
                    safe_owner,
                    int(source_id),
                    normalized_hash,
                    safe_path,
                    "qb_completed",
                    uuid.uuid4().hex,
                    timestamp,
                    timestamp,
                ),
            )
            task_id = int(cur.lastrowid)
        else:
            task_id = int(existing["id"])
            target = f"local-media-task:{task_id}"
            already_linked = str(request_row["local_import_target"] or "") == target
            terminal = str(existing["status"] or "") in {
                "completed", "failed", "requires_manual",
            }
            path_changed = str(existing["content_path"] or "") != safe_path
            if path_changed and (already_linked or not terminal):
                raise ValueError("相同 qB 任务对应的内容路径不一致")
            if not already_linked and terminal:
                conn.execute(
                    "UPDATE local_media_tasks SET content_path=?,trigger='qb_completed',"
                    "status='waiting_stable',stable_since='',snapshot_digest='',rules_snapshot='',"
                    "recognition_summary='',tmdb_id='',media_type='',season_override=NULL,"
                    "episode_override=NULL,numbering_mode='auto',title='',year='',"
                    "operation_token=?,error='',warning='',"
                    "completed_at=NULL,version=version+1,updated_at=? WHERE id=? AND owner=?",
                    (safe_path, uuid.uuid4().hex, timestamp, task_id, safe_owner),
                )
                conn.execute(
                    "DELETE FROM local_media_task_items WHERE task_id=?",
                    (task_id,),
                )
                restarted = True

        target = f"local-media-task:{task_id}"
        current_target = str(request_row["local_import_target"] or "")
        if current_target and current_target != target:
            raise ValueError("下载请求已绑定其他本地整理任务")
        cur = conn.execute(
            "UPDATE download_requests SET local_import_status='pending',local_import_target=?,"
            "qb_content_path=?,local_import_error='',local_import_started_at="
            "COALESCE(NULLIF(local_import_started_at,''),?),"
            "local_import_completed_at=NULL,updated_at=? "
            "WHERE id=? AND COALESCE(local_import_status,'') IN ('','pending')",
            (target, safe_path, timestamp, timestamp, int(request_id)),
        )
        if cur.rowcount != 1:
            raise ValueError("下载请求的本地入库状态已变化")
        return task_id, restarted


def list_download_requests_for_local_media_task(task_id: int) -> list[sqlite3.Row]:
    """返回绑定到同一本地整理任务的下载事务。"""
    target = f"local-media-task:{int(task_id)}"
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM download_requests WHERE local_import_target=? ORDER BY id",
            (target,),
        ).fetchall()


def get_local_media_task(task_id: int, *, owner: str = "admin"):
    from app.modules.local_media_models import LocalMediaTask

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM local_media_tasks WHERE id=? AND owner=?",
            (int(task_id), _local_media_owner(owner)),
        ).fetchone()
    return LocalMediaTask.from_row(row) if row else None


def list_local_media_tasks(*, owner: str = "admin", status: str = "", limit: int = 200):
    from app.modules.local_media_models import LOCAL_TASK_STATUSES, LocalMediaTask

    sql = "SELECT * FROM local_media_tasks WHERE owner=?"
    params: list[object] = [_local_media_owner(owner)]
    if status:
        if status not in LOCAL_TASK_STATUSES:
            raise ValueError("不支持的本地媒体任务状态")
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [LocalMediaTask.from_row(row) for row in rows]


def delete_local_media_tasks(task_ids: list[int], *, owner: str = "admin") -> dict[str, int]:
    """删除指定的非运行中本地整理日志；关联条目和步骤由外键级联清理。"""
    from app.modules.local_media_models import LOCAL_BUSY_TASK_STATUSES

    safe_owner = _local_media_owner(owner)
    normalized_ids: list[int] = []
    for raw_task_id in task_ids:
        task_id = int(raw_task_id)
        if task_id > 0 and task_id not in normalized_ids:
            normalized_ids.append(task_id)
    if not normalized_ids:
        return {"requested": 0, "deleted": 0, "skipped_busy": 0, "missing": 0}
    if len(normalized_ids) > 500:
        raise ValueError("单次最多清除 500 条本地整理日志")

    placeholders = ",".join("?" for _ in normalized_ids)
    busy_placeholders = ",".join("?" for _ in LOCAL_BUSY_TASK_STATUSES)
    with get_conn() as conn:
        deleted = int(conn.execute(
            f"DELETE FROM local_media_tasks WHERE owner=? AND id IN ({placeholders}) "
            f"AND status NOT IN ({busy_placeholders})",
            [safe_owner, *normalized_ids, *LOCAL_BUSY_TASK_STATUSES],
        ).rowcount)
        remaining_rows = conn.execute(
            f"SELECT id,status FROM local_media_tasks WHERE owner=? AND id IN ({placeholders})",
            [safe_owner, *normalized_ids],
        ).fetchall()
        remaining = {int(row["id"]): str(row["status"]) for row in remaining_rows}
    return {
        "requested": len(normalized_ids),
        "deleted": deleted,
        "skipped_busy": sum(status in LOCAL_BUSY_TASK_STATUSES for status in remaining.values()),
        "missing": len(normalized_ids) - deleted - len(remaining),
    }



def _agent_workspace_title_pattern(query: str) -> str:
    """为 Agent 标题搜索转义 LIKE 通配符，避免把用户输入解释为查询语法。"""
    value = str(query or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{value}%"


def _agent_workspace_search_rows(sql: str, query: str, limit: int) -> dict[str, object]:
    safe_limit = max(1, min(int(limit), 20))
    with get_conn() as conn:
        rows = conn.execute(sql, (_agent_workspace_title_pattern(query), safe_limit + 1)).fetchall()
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


def search_agent_workspace_downloads(query: str, *, limit: int = 8) -> dict[str, object]:
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
            (_local_media_owner(owner), _agent_workspace_title_pattern(query), safe_limit + 1),
        ).fetchall()
    return {
        "items": [dict(row) for row in rows[:safe_limit]],
        "truncated": len(rows) > safe_limit,
    }


def get_local_media_diagnostic_summary(*, owner: str = "admin") -> dict[str, dict[str, int]]:
    """只读聚合本地媒体运行状态，不读取路径、标题、哈希或错误正文。"""
    safe_owner = _local_media_owner(owner)
    with get_conn() as conn:
        source_row = conn.execute(
            "SELECT COUNT(*) AS total,"
            "SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled,"
            "SUM(CASE WHEN enabled=1 THEN 0 ELSE 1 END) AS disabled,"
            "SUM(CASE WHEN enabled=1 AND scan_enabled=1 THEN 1 ELSE 0 END) AS scan_enabled,"
            "SUM(CASE WHEN mode='move' THEN 1 ELSE 0 END) AS move_mode,"
            "SUM(CASE WHEN mode='preview_only' THEN 1 ELSE 0 END) AS preview_only_mode,"
            "SUM(CASE WHEN enabled=1 AND NOT EXISTS ("
            "SELECT 1 FROM local_library_targets t WHERE t.source_id=s.id AND t.owner=s.owner"
            ") THEN 1 ELSE 0 END) AS enabled_without_targets "
            "FROM local_media_sources s WHERE owner=?",
            (safe_owner,),
        ).fetchone()
        task_row = conn.execute(
            "SELECT COUNT(*) AS total,"
            "SUM(CASE WHEN status='waiting_stable' THEN 1 ELSE 0 END) AS waiting_stable,"
            "SUM(CASE WHEN status IN ('recognizing','moving','verifying','refreshing','rolling_back') "
            "THEN 1 ELSE 0 END) AS active,"
            "SUM(CASE WHEN status='requires_manual' THEN 1 ELSE 0 END) AS requires_manual,"
            "SUM(CASE WHEN status='planned' THEN 1 ELSE 0 END) AS planned,"
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,"
            "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,"
            "SUM(CASE WHEN trigger='qb_completed' THEN 1 ELSE 0 END) AS qb_completed,"
            "SUM(CASE WHEN trigger='scan' THEN 1 ELSE 0 END) AS scan,"
            "SUM(CASE WHEN trigger='manual' THEN 1 ELSE 0 END) AS manual "
            "FROM local_media_tasks WHERE owner=?",
            (safe_owner,),
        ).fetchone()

    def counts(row, keys: tuple[str, ...]) -> dict[str, int]:
        return {key: max(0, int(row[key] or 0)) for key in keys}

    return {
        "sources": counts(source_row, (
            "total", "enabled", "disabled", "scan_enabled", "move_mode",
            "preview_only_mode", "enabled_without_targets",
        )),
        "tasks": counts(task_row, (
            "total", "waiting_stable", "active", "requires_manual", "planned",
            "failed", "completed", "qb_completed", "scan", "manual",
        )),
    }


def _local_media_age_bucket_sql(timestamp_expression: str) -> str:
    return (
        "CASE "
        f"WHEN {timestamp_expression}='' THEN 'unknown' "
        f"WHEN datetime({timestamp_expression}) >= datetime('now','-1 hour') THEN 'under_1h' "
        f"WHEN datetime({timestamp_expression}) >= datetime('now','-1 day') THEN '1h_to_24h' "
        f"WHEN datetime({timestamp_expression}) >= datetime('now','-7 days') THEN '1d_to_7d' "
        "ELSE 'over_7d' END"
    )


def get_local_media_review_queue_summary(*, owner: str = "admin") -> dict[str, object]:
    """聚合待人工确认队列；只返回数量、触发来源和年龄分桶。"""
    safe_owner = _local_media_owner(owner)
    age_sql = _local_media_age_bucket_sql("COALESCE(NULLIF(updated_at,''),created_at,'')")
    with get_conn() as conn:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM local_media_tasks WHERE owner=? AND status='requires_manual'",
            (safe_owner,),
        ).fetchone()[0])
        trigger_rows = conn.execute(
            "SELECT trigger,COUNT(*) AS count FROM local_media_tasks "
            "WHERE owner=? AND status='requires_manual' GROUP BY trigger",
            (safe_owner,),
        ).fetchall()
        age_rows = conn.execute(
            f"SELECT {age_sql} AS age_bucket,COUNT(*) AS count FROM local_media_tasks "
            "WHERE owner=? AND status='requires_manual' GROUP BY age_bucket",
            (safe_owner,),
        ).fetchall()
    return {
        "total": total,
        "by_trigger": {str(row["trigger"] or "unknown"): int(row["count"] or 0) for row in trigger_rows},
        "age_buckets": {str(row["age_bucket"]): int(row["count"] or 0) for row in age_rows},
    }


def get_local_media_history_summary(*, owner: str = "admin") -> dict[str, object]:
    """聚合本地媒体终态历史；不读取标题、路径、标识或错误正文。"""
    safe_owner = _local_media_owner(owner)
    timestamp_expression = "COALESCE(NULLIF(completed_at,''),NULLIF(updated_at,''),created_at,'')"
    age_sql = _local_media_age_bucket_sql(timestamp_expression)
    with get_conn() as conn:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM local_media_tasks WHERE owner=? AND status IN ('completed','failed')",
            (safe_owner,),
        ).fetchone()[0])
        status_rows = conn.execute(
            "SELECT status,COUNT(*) AS count FROM local_media_tasks "
            "WHERE owner=? AND status IN ('completed','failed') GROUP BY status",
            (safe_owner,),
        ).fetchall()
        trigger_rows = conn.execute(
            "SELECT trigger,COUNT(*) AS count FROM local_media_tasks "
            "WHERE owner=? AND status IN ('completed','failed') GROUP BY trigger",
            (safe_owner,),
        ).fetchall()
        age_rows = conn.execute(
            f"SELECT {age_sql} AS age_bucket,COUNT(*) AS count FROM local_media_tasks "
            "WHERE owner=? AND status IN ('completed','failed') GROUP BY age_bucket",
            (safe_owner,),
        ).fetchall()
    return {
        "total": total,
        "by_status": {str(row["status"]): int(row["count"] or 0) for row in status_rows},
        "by_trigger": {str(row["trigger"] or "unknown"): int(row["count"] or 0) for row in trigger_rows},
        "age_buckets": {str(row["age_bucket"]): int(row["count"] or 0) for row in age_rows},
    }


def prepare_manual_local_media_task(
    source_id: int, content_path: str, *, owner: str = "admin",
    tmdb_id: str = "", media_type: str = "", rules_snapshot: str = "",
    season_override: int | None = None, episode_override: int | None = None,
    numbering_mode: str = "auto",
) -> int:
    """原子创建或重置可重试的手动任务；活动任务绝不被改回等待态。"""
    import uuid

    safe_owner = _local_media_owner(owner)
    safe_path = str(content_path or "").strip()
    normalized_type = str(media_type or "").strip().lower()
    from app.modules.episode_mapping import NUMBERING_MODES, normalize_numbering_mode
    raw_numbering_mode = str(numbering_mode or "auto").strip().lower()
    if raw_numbering_mode not in NUMBERING_MODES:
        raise ValueError("剧集编号模式无效")
    normalized_numbering_mode = normalize_numbering_mode(raw_numbering_mode)
    if not safe_path:
        raise ValueError("本地媒体任务路径不能为空")
    if normalized_type and normalized_type not in {"movie", "tv"}:
        raise ValueError("媒体类型必须是 movie 或 tv")
    if season_override is not None:
        if isinstance(season_override, bool) or not isinstance(season_override, int):
            raise ValueError("季数必须是整数")
        if not 0 <= season_override <= 99:
            raise ValueError("季数超出允许范围")
    if episode_override is not None:
        if isinstance(episode_override, bool) or not isinstance(episode_override, int):
            raise ValueError("集数必须是整数")
        if not 1 <= episode_override <= 999:
            raise ValueError("集数超出允许范围")
    if normalized_type == "movie" and (season_override is not None or episode_override is not None):
        raise ValueError("电影任务不能指定季数或集数")
    if normalized_type == "movie":
        normalized_numbering_mode = "auto"
    timestamp = now()
    token = uuid.uuid4().hex
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute(
            "SELECT id FROM local_media_sources WHERE id=? AND owner=?", (int(source_id), safe_owner)
        ).fetchone()
        if not source:
            raise LookupError("本地媒体来源不存在")
        existing = conn.execute(
            "SELECT id,status FROM local_media_tasks WHERE source_id=? AND content_path=? AND owner=? "
            "ORDER BY id DESC LIMIT 1", (int(source_id), safe_path, safe_owner),
        ).fetchone()
        if existing and existing["status"] in {"failed", "requires_manual"}:
            task_id = int(existing["id"])
            cur = conn.execute(
                "UPDATE local_media_tasks SET status='waiting_stable',stable_since='',snapshot_digest='',"
                "recognition_summary='',rules_snapshot=?,tmdb_id=?,media_type=?,"
                "season_override=?,episode_override=?,numbering_mode=?,title='',year='',"
                "operation_token=?,error='',warning='',completed_at=NULL,"
                "version=version+1,updated_at=? WHERE id=? AND owner=? AND status IN ('failed','requires_manual')",
                (str(rules_snapshot or ""), str(tmdb_id or "").strip(), normalized_type,
                 season_override, episode_override, normalized_numbering_mode,
                 token, timestamp, task_id, safe_owner),
            )
            if cur.rowcount != 1:
                raise ValueError("任务状态已变化，请刷新后重试")
            conn.execute(
                "DELETE FROM local_media_task_items WHERE task_id=?",
                (task_id,),
            )
            return task_id
        if existing and existing["status"] != "completed":
            raise ValueError("该目录已有任务正在处理中")
        cur = conn.execute(
            "INSERT INTO local_media_tasks(owner,source_id,qb_hash,content_path,trigger,status,"
            "operation_token,rules_snapshot,tmdb_id,media_type,season_override,episode_override,"
            "numbering_mode,created_at,updated_at) "
            "VALUES(?,?,NULL,?,'manual','waiting_stable',?,?,?,?,?,?,?,?,?)",
            (safe_owner, int(source_id), safe_path, token, str(rules_snapshot or ""),
             str(tmdb_id or "").strip(), normalized_type, season_override, episode_override,
             normalized_numbering_mode, timestamp, timestamp),
        )
        return int(cur.lastrowid)


def claim_local_media_task(
    task_id: int,
    *,
    expected: str = "waiting_stable",
    next_status: str = "recognizing",
    owner: str = "admin",
) -> bool:
    from app.modules.local_media_models import LOCAL_TASK_STATUSES

    if expected not in LOCAL_TASK_STATUSES or next_status not in LOCAL_TASK_STATUSES:
        raise ValueError("不支持的本地媒体任务状态")
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE local_media_tasks SET status=?,attempts=attempts+1,version=version+1,"
            "error='',updated_at=? WHERE id=? AND owner=? AND status=?",
            (next_status, timestamp, int(task_id), _local_media_owner(owner), expected),
        )
        return cur.rowcount == 1


def claim_local_media_confirmation_task(
    task_id: int,
    *,
    owner: str = "admin",
    expected_version: int,
    expected_snapshot_digest: str = "",
    tmdb_id: str,
    media_type: str,
    rules_snapshot: str,
    season_override: int | None = None,
    episode_override: int | None = None,
    numbering_mode: str = "auto",
    title: str = "",
    year: str = "",
) -> bool:
    """把仍然有效的本地待确认任务原子转换为执行态。"""
    normalized_type = str(media_type or "").strip().lower()
    normalized_tmdb_id = str(tmdb_id or "").strip()
    from app.modules.episode_mapping import NUMBERING_MODES, normalize_numbering_mode
    raw_numbering_mode = str(numbering_mode or "auto").strip().lower()
    if raw_numbering_mode not in NUMBERING_MODES:
        raise ValueError("剧集编号模式无效")
    normalized_numbering_mode = normalize_numbering_mode(raw_numbering_mode)
    if not normalized_tmdb_id or normalized_type not in {"movie", "tv"}:
        raise ValueError("候选媒体参数无效")
    if isinstance(expected_version, bool) or int(expected_version) <= 0:
        raise ValueError("本地媒体任务版本无效")
    for value, minimum, maximum, label in (
        (season_override, 0, 99, "季数"),
        (episode_override, 1, 999, "集数"),
    ):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label}必须是整数")
        if not minimum <= value <= maximum:
            raise ValueError(f"{label}超出允许范围")
    if normalized_type == "movie":
        season_override = None
        episode_override = None
        normalized_numbering_mode = "auto"

    where = "id=? AND owner=? AND status='requires_manual' AND version=?"
    params: list[object] = [
        str(rules_snapshot or ""), normalized_tmdb_id, normalized_type,
        season_override, episode_override, normalized_numbering_mode,
        str(title or ""), str(year or ""),
        now(), int(task_id), _local_media_owner(owner), int(expected_version),
    ]
    expected_digest = str(expected_snapshot_digest or "").strip()
    if expected_digest:
        where += " AND snapshot_digest=?"
        params.append(expected_digest)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE local_media_tasks SET status='recognizing',attempts=attempts+1,"
            "recognition_summary='',rules_snapshot=?,tmdb_id=?,media_type=?,"
            "season_override=?,episode_override=?,numbering_mode=?,"
            "title=?,year=?,error='',warning='',completed_at=NULL,version=version+1,updated_at=? "
            f"WHERE {where}",
            params,
        )
        return cur.rowcount == 1


def update_local_media_task(task_id: int, *, owner: str = "admin", **fields) -> bool:
    from app.modules.local_media_models import LOCAL_TASK_STATUSES

    allowed = {
        "status", "stable_since", "snapshot_digest", "rules_snapshot", "recognition_summary",
        "tmdb_id", "media_type",
        "season_override", "episode_override", "numbering_mode", "title", "year",
        "error", "warning", "completed_at", "content_path",
    }
    sets: list[str] = []
    params: list[object] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "status" and value not in LOCAL_TASK_STATUSES:
            raise ValueError("不支持的本地媒体任务状态")
        sets.append(f"{key}=?")
        params.append(value)
    if not sets:
        return False
    sets.extend(["version=version+1", "updated_at=?"])
    params.extend([now(), int(task_id), _local_media_owner(owner)])
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE local_media_tasks SET {', '.join(sets)} WHERE id=? AND owner=?",
            params,
        )
        return cur.rowcount == 1


def add_local_media_task_item(
    task_id: int,
    source_path: str,
    target_path: str = "",
    *,
    role: str = "metadata",
    media_group: str = "",
    action: str = "move",
    size: int = 0,
    mtime_ns: int = 0,
    device: int = 0,
    inode: int = 0,
    owner: str = "admin",
) -> int:
    safe_owner = _local_media_owner(owner)
    timestamp = now()
    with get_conn() as conn:
        task = conn.execute(
            "SELECT id FROM local_media_tasks WHERE id=? AND owner=?", (int(task_id), safe_owner)
        ).fetchone()
        if not task:
            raise LookupError("本地媒体任务不存在")
        conn.execute(
            "INSERT INTO local_media_task_items(task_id,owner,source_path,target_path,role,media_group,"
            "action,size,mtime_ns,device,inode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id,source_path) DO UPDATE SET target_path=excluded.target_path,"
            "role=excluded.role,media_group=excluded.media_group,action=excluded.action,size=excluded.size,"
            "mtime_ns=excluded.mtime_ns,device=excluded.device,inode=excluded.inode,updated_at=excluded.updated_at",
            (
                int(task_id), safe_owner, str(source_path), str(target_path), str(role), str(media_group),
                str(action), max(0, int(size)), int(mtime_ns), int(device), int(inode), timestamp, timestamp,
            ),
        )
        row = conn.execute(
            "SELECT id FROM local_media_task_items WHERE task_id=? AND source_path=? AND owner=?",
            (int(task_id), str(source_path), safe_owner),
        ).fetchone()
        return int(row["id"])


def list_local_media_task_items(task_id: int, *, owner: str = "admin") -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM local_media_task_items WHERE task_id=? AND owner=? ORDER BY id",
            (int(task_id), _local_media_owner(owner)),
        ).fetchall()


def add_local_media_operation_step(
    task_id: int,
    operation_token: str,
    step_index: int,
    action: str,
    source_path: str = "",
    target_path: str = "",
    *,
    owner: str = "admin",
) -> int:
    safe_owner = _local_media_owner(owner)
    with get_conn() as conn:
        task = conn.execute(
            "SELECT id FROM local_media_tasks WHERE id=? AND owner=?", (int(task_id), safe_owner)
        ).fetchone()
        if not task:
            raise LookupError("本地媒体任务不存在")
        conn.execute(
            "INSERT INTO local_media_operation_steps(task_id,operation_token,step_index,action,"
            "source_path,target_path,status) VALUES(?,?,?,?,?,?,'pending') "
            "ON CONFLICT(task_id,operation_token,step_index) DO UPDATE SET "
            "action=excluded.action,source_path=excluded.source_path,target_path=excluded.target_path,"
            "status='pending',error='',started_at=NULL,finished_at=NULL",
            (
                int(task_id), str(operation_token), int(step_index), str(action),
                str(source_path), str(target_path),
            ),
        )
        row = conn.execute(
            "SELECT id FROM local_media_operation_steps WHERE task_id=? AND operation_token=? AND step_index=?",
            (int(task_id), str(operation_token), int(step_index)),
        ).fetchone()
        return int(row["id"])


def update_local_media_operation_step(
    step_id: int,
    status: str,
    *,
    error: str = "",
) -> bool:
    safe_status = str(status or "").strip().lower()
    if safe_status not in {"pending", "running", "completed", "failed", "rolled_back"}:
        raise ValueError("不支持的本地媒体操作步骤状态")
    timestamp = now()
    assignments = ["status=?", "error=?"]
    params: list[object] = [safe_status, str(error or "")[:1000]]
    if safe_status == "running":
        assignments.append("started_at=COALESCE(started_at,?)")
        params.append(timestamp)
    if safe_status in {"completed", "failed", "rolled_back"}:
        assignments.append("finished_at=?")
        params.append(timestamp)
    params.append(int(step_id))
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE local_media_operation_steps SET {', '.join(assignments)} WHERE id=?",
            params,
        )
        return cur.rowcount == 1


def list_local_media_operation_steps(task_id: int, *, owner: str = "admin") -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT steps.* FROM local_media_operation_steps AS steps "
            "JOIN local_media_tasks AS tasks ON tasks.id=steps.task_id "
            "WHERE steps.task_id=? AND tasks.owner=? ORDER BY steps.step_index,steps.id",
            (int(task_id), _local_media_owner(owner)),
        ).fetchall()


def update_local_media_source(source_id: int, *, owner: str = "admin", **fields) -> bool:
    allowed = {
        "name", "qb_profile", "qb_path_prefix", "local_root", "enabled", "stable_seconds",
        "scan_enabled", "scan_interval_minutes", "media_type", "mode",
    }
    sets: list[str] = []
    params: list[object] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "mode" and value not in {"move", "preview_only"}:
            raise ValueError("本地媒体来源仅支持 move 或 preview_only")
        if key == "media_type" and value not in {"auto", "movie", "tv", "nsfw"}:
            raise ValueError("本地媒体来源类型必须是 auto、movie、tv 或 nsfw")
        if key in {"enabled", "scan_enabled"}:
            value = 1 if value else 0
        elif key == "stable_seconds":
            value = max(0, int(value))
        elif key == "scan_interval_minutes":
            value = max(1, int(value))
        else:
            value = str(value or "").strip()
        if key in {"name", "local_root"} and not value:
            raise ValueError("本地媒体来源名称和路径不能为空")
        sets.append(f"{key}=?")
        params.append(value)
    if not sets:
        return False
    sets.append("updated_at=?")
    params.extend([now(), int(source_id), _local_media_owner(owner)])
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE local_media_sources SET {', '.join(sets)} WHERE id=? AND owner=?", params
        )
        return cur.rowcount == 1


def delete_local_media_source(source_id: int, *, owner: str = "admin") -> bool:
    safe_owner = _local_media_owner(owner)
    with get_conn() as conn:
        active = conn.execute(
            "SELECT 1 FROM local_media_tasks WHERE source_id=? AND owner=? "
            "AND status NOT IN ('completed','failed') LIMIT 1",
            (int(source_id), safe_owner),
        ).fetchone()
        if active:
            raise ValueError("来源仍有未完成任务，不能删除")
        cur = conn.execute(
            "DELETE FROM local_media_sources WHERE id=? AND owner=?", (int(source_id), safe_owner)
        )
        return cur.rowcount == 1


def reset_local_media_task(
    task_id: int,
    *,
    owner: str = "admin",
    tmdb_id: str | None = None,
    media_type: str | None = None,
    season_override: int | None = None,
    episode_override: int | None = None,
    numbering_mode: str | None = None,
) -> bool:
    import uuid

    normalized_type = None if media_type is None else str(media_type or "").strip().lower()
    normalized_numbering_mode = None
    if numbering_mode is not None:
        from app.modules.episode_mapping import NUMBERING_MODES, normalize_numbering_mode
        raw_numbering_mode = str(numbering_mode or "auto").strip().lower()
        if raw_numbering_mode not in NUMBERING_MODES:
            raise ValueError("剧集编号模式无效")
        normalized_numbering_mode = normalize_numbering_mode(raw_numbering_mode)
    if normalized_type and normalized_type not in {"movie", "tv"}:
        raise ValueError("媒体类型必须是 movie 或 tv")
    for value, minimum, maximum, label in (
        (season_override, 0, 99, "季数"),
        (episode_override, 1, 999, "集数"),
    ):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label}必须是整数")
        if not minimum <= value <= maximum:
            raise ValueError(f"{label}超出允许范围")
    if normalized_type == "movie" and (season_override is not None or episode_override is not None):
        raise ValueError("电影任务不能指定季数或集数")
    if normalized_type == "movie":
        normalized_numbering_mode = "auto"
    assignments = [
        "status='waiting_stable'", "stable_since=''", "snapshot_digest=''",
        "recognition_summary=''", "title=''", "year=''", "operation_token=?",
        "error=''", "warning=''", "completed_at=NULL",
        "version=version+1", "updated_at=?",
    ]
    params: list[object] = [uuid.uuid4().hex, now()]
    if tmdb_id is not None:
        assignments.append("tmdb_id=?")
        params.append(str(tmdb_id or "").strip())
    if normalized_type is not None:
        assignments.append("media_type=?")
        params.append(normalized_type)
        if normalized_type == "movie":
            assignments.extend(["season_override=NULL", "episode_override=NULL"])
    if season_override is not None:
        assignments.append("season_override=?")
        params.append(season_override)
    if episode_override is not None:
        assignments.append("episode_override=?")
        params.append(episode_override)
    if normalized_numbering_mode is not None:
        assignments.append("numbering_mode=?")
        params.append(normalized_numbering_mode)
    params.extend([int(task_id), _local_media_owner(owner)])
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            f"UPDATE local_media_tasks SET {', '.join(assignments)} "
            "WHERE id=? AND owner=? AND status IN ('failed','requires_manual')",
            params,
        )
        if cur.rowcount != 1:
            return False
        # 新 attempt 不得继承旧计划，否则刷新范围和可见性核验会读到过期路径。
        conn.execute(
            "DELETE FROM local_media_task_items WHERE task_id=?",
            (int(task_id),),
        )
        return True


def claim_agent_action_lease(
    lease_key: str, *, ttl_seconds: int = 90
) -> str | None:
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
        conn.execute(
            "DELETE FROM agent_action_leases WHERE expires_at<=?", (current,)
        )
        cur = conn.execute(
            "INSERT OR IGNORE INTO agent_action_leases("
            "lease_key,lease_token,expires_at,created_at) VALUES(?,?,?,?)",
            (normalized, token, current + ttl, now()),
        )
        return token if cur.rowcount == 1 else None


def reset_local_media_task_if_current(
    task_id: int,
    *,
    owner: str = "admin",
    expected_version: int,
    expected_status: str,
) -> bool:
    """按任务版本与可重试终态原子重新排队，并切换新的操作幂等标识。"""
    import uuid

    safe_status = str(expected_status or "").strip().lower()
    if safe_status not in {"failed", "requires_manual"}:
        return False
    if isinstance(expected_version, bool) or int(expected_version) < 1:
        return False
    stamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE local_media_tasks SET status='waiting_stable',stable_since='',"
            "snapshot_digest='',recognition_summary='',title='',year='',operation_token=?,"
            "error='',warning='',completed_at=NULL,"
            "version=version+1,updated_at=? WHERE id=? AND owner=? AND version=? AND status=?",
            (
                uuid.uuid4().hex,
                stamp,
                int(task_id),
                _local_media_owner(owner),
                int(expected_version),
                safe_status,
            ),
        )
        if cur.rowcount == 1:
            # 旧 attempt 的目标路径不可参与新 attempt 的精准刷新或可见性核验。
            conn.execute(
                "DELETE FROM local_media_task_items WHERE task_id=?",
                (int(task_id),),
            )
            return True
        return False


def delete_local_library_target(source_id: int, category: str, *, owner: str = "admin") -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM local_library_targets WHERE source_id=? AND category=? AND owner=?",
            (int(source_id), str(category or "").strip().lower(), _local_media_owner(owner)),
        )
        return cur.rowcount == 1
