
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
    tmdb_id TEXT DEFAULT '',
    media_type TEXT DEFAULT '',
    season_override INTEGER,
    episode_override INTEGER,
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

CREATE TABLE IF NOT EXISTS agent_action_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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

CREATE TABLE IF NOT EXISTS agent_session_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_digest TEXT NOT NULL,
    context_type TEXT NOT NULL
        CHECK(context_type IN ('patrol','download_submission')),
    payload TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_session_context_lookup
    ON agent_session_context(owner_digest, context_type, expires_at, id DESC);
CREATE INDEX IF NOT EXISTS idx_agent_session_context_expiry
    ON agent_session_context(expires_at);

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
