"""Agent API 与 LLM 回退共用的轻量进程内限流。"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from collections.abc import Callable

_TOOL_RATE_LIMIT_SCOPES = {
    "agent.action_history": "agent-action-history",
    "config.test_media_server": "media-server-test",
    "media_proxy.status_summary": "media-proxy-status",
    "media_proxy.test_instance": "media-proxy-test",
    "media_proxy.set_instance_enabled": "media-proxy-control",
    "media_proxy.restart_instance": "media-proxy-control",
    "media_proxy.playback_failure_summary": "media-proxy-playback-failures",
    "recognition.set_rule_enabled": "recognition-rule-control",
    "config.diagnose_media_servers": "media-server-diagnosis",
    "config.feature_summary": "feature-summary",
    "config.safe_policy_summary": "safe-policy-summary",
    "config.set_safe_policy": "safe-policy-control",
    "telegram.send_test_notification": "telegram-test-notification",
    "config.explain_component": "config-component-explain",
    "library.search_missing_episode_resources": "missing-episode-resources",
    "library.search_missing_season_resources": "missing-episode-resources",
    "library.batch_presence": "library-batch-presence",
    "library.missing_media_workflows": "missing-media-workflows",
    "downloads.diagnose_queue": "download-queue-diagnosis",
    "downloads.retry_submission": "download-submission-retry",
    "guangya.connection_status": "guangya-connection-status",
    "guangya.media_hygiene.preview": "guangya-media-hygiene",
    "guangya.rename.preview": "guangya-rename",
    "guangya.rename.execute": "guangya-rename-write",
    "guangya.directory_scrape.inspect": "guangya-directory-scrape",
    "guangya.directory_scrape.search": "guangya-directory-scrape",
    "guangya.directory_scrape.preview": "guangya-directory-scrape",
    "guangya.directory_scrape.run": "guangya-directory-scrape-write",
    "guangya.organize.schedule_policy": "guangya-organize-schedule-policy",
    "guangya.organize.set_schedule_policy": "guangya-organize-schedule-policy-write",
    "guangya.organize.cleanup.preview": "guangya-organize-cleanup",
    "guangya.organize.cleanup.classify": "guangya-organize-cleanup",
    "guangya.organize.cleanup.execute": "guangya-organize-cleanup-write",
    "ingest.inspect": "ingest-read",
    "ingest.submit": "ingest-write",
    "ingest.status": "ingest-status",
    "rss.diagnose": "rss-diagnosis",
    "rss.subscription_summaries": "rss-subscription-summary",
    "rss.recent_activity": "rss-subscription-summary",
    "rss.get_subscription_summary": "rss-subscription-summary",
    "rss.refresh_subscription": "rss-refresh-write",
    "rss.refresh_subscriptions": "rss-refresh-write",
    "rss.create_subscription": "rss-subscription-control",
    "rss.update_subscription": "rss-subscription-control",
    "rss.delete_subscription": "rss-subscription-control",
    "media.create_subscription": "media-subscription-control",
    "media.delete_subscription": "media-subscription-control",
    "media.set_subscription_enabled": "media-subscription-control",
    "media.continue_watching": "media-consumption-resume",
    "media.recently_added": "media-consumption-recent",
    "media.recently_played": "media-consumption-recent",
    "media.recommend_from_library": "media-library-recommendation",
    "media.preferences": "media-preferences",
    "media.set_preferences": "media-preferences",
    "media.clear_preferences": "media-preferences",
    "media.today_summary": "media-today-summary",
    "media.subscription_notification_rule": "media-subscription-notification-control",
    "media.set_subscription_notification_rule": "media-subscription-notification-control",
    "media.reset_subscription_notification_rule": "media-subscription-notification-control",
    "rss.entry_summaries": "rss-entry-read",
    "rss.mark_entries": "rss-entry-write",
    "rss.submit_entries_to_qb": "rss-pending-download",
    "rss.submit_pending_to_qb": "rss-pending-download",
    "rss.retry_failed_to_qb": "rss-failure-retry",
    "automation.diagnose_pipeline": "automation-pipeline-diagnosis",
    "local_media.diagnose": "local-media-diagnosis",
    "local_media.source_summaries": "local-media-source-read",
    "local_media.get_source_summary": "local-media-source-read",
    "local_media.set_source_trigger_enabled": "local-media-source-control",
    "local_media.scan_sources": "local-media-source-control",
    "local_media.review_queue_summary": "local-media-diagnosis",
    "local_media.history_summary": "local-media-diagnosis",
    "local_media.task_summaries": "local-media-tasks",
    "local_media.inspect_task": "local-media-tasks",
    "local_media.preview_task": "local-media-tasks",
    "local_media.verify_task_library_visibility": "local-media-tasks",
    "local_media.retry_task": "local-media-task-write",
    "local_media.refresh_task_library": "local-media-task-write",
    "organize.audit_logs": "organize-audit-logs",
    "strm.run_history": "strm-history",
    "strm.triage_failures": "strm-failure-triage",
    "strm.retry_failures": "strm-failure-retry",
    "strm.schedule_policy": "strm-schedule-policy",
    "strm.set_schedule_policy": "strm-schedule-policy-write",
    "workspace.search": "workspace-search",
    "workspace.todo": "workspace-todo",
    "workspace.next_actions": "workspace-next-actions",
    "workspace.briefing": "workspace-briefing",
    "workspace.health": "workspace-health",
    "indexer.search_resources": "indexer-resource-search",
    "indexer.diagnose_readiness": "indexer-readiness",
    "library.audit_library_episodes": "library-full-audit",
    "library.start_episode_audit": "library-full-audit",
    "library.trigger_patrol_now": "library-full-audit",
    "library.patrol_status": "library-patrol-status",
    "library.audit_episodes": "library-update-check",
    "library.check_updates": "library-update-check",
    "media.subscription_updates": "media-subscription-updates",
    "discovery.recommend": "discovery-recommend",
    "discovery.person_filmography": "discovery-person-filmography",
    "discovery.watchlist_summaries": "discovery-watchlist-read",
    "discovery.get_watchlist_summary": "discovery-watchlist-read",
    "discovery.detail": "discovery-detail",
    "discovery.mapping_candidates": "discovery-mapping",
    "discovery.confirm_mapping": "discovery-mapping-write",
    "discovery.add_watchlist": "discovery-watchlist-write",
    "discovery.remove_watchlist": "discovery-watchlist-write",
    "bangumi.calendar": "bangumi-calendar",
    "web.search": "web-access",
    "web.read": "web-access",
}

_TOOL_RATE_LIMITS = {
    "agent.action_history": 12,
    "config.test_media_server": 6,
    "media_proxy.status_summary": 12,
    "media_proxy.test_instance": 4,
    "media_proxy.set_instance_enabled": 4,
    "media_proxy.restart_instance": 3,
    "media_proxy.playback_failure_summary": 8,
    "recognition.set_rule_enabled": 4,
    "config.diagnose_media_servers": 4,
    "config.feature_summary": 12,
    "config.safe_policy_summary": 12,
    "config.set_safe_policy": 4,
    "telegram.send_test_notification": 3,
    "config.explain_component": 12,
    "discovery.search": 6,
    "discovery.recommend": 6,
    "discovery.person_filmography": 6,
    "discovery.watchlist_summaries": 8,
    "discovery.get_watchlist_summary": 12,
    "discovery.detail": 8,
    "discovery.mapping_candidates": 4,
    "discovery.confirm_mapping": 4,
    "discovery.add_watchlist": 4,
    "discovery.remove_watchlist": 4,
    "bangumi.calendar": 6,
    "web.search": 6,
    "web.read": 6,
    "provider.capabilities": 12,
    "provider.query": 8,
    "provider.change.preview": 4,
    "provider.change.execute": 3,
    "provider.job.status": 12,
    "guangya.capabilities": 12,
    "guangya.account.status": 8,
    "guangya.connection_status": 4,
    "guangya.fs.query": 8,
    "guangya.fs.change.preview": 4,
    "guangya.fs.change.execute": 3,
    "guangya.recycle.list": 8,
    "guangya.recycle.restore": 3,
    "guangya.recycle.clear": 2,
    "guangya.operation.status": 12,
    "guangya.share.list": 8,
    "guangya.share.create": 3,
    "guangya.share.revoke": 3,
    "guangya.media_hygiene.preview": 4,
    "guangya.rename.preview": 4,
    "guangya.rename.execute": 3,
    "guangya.directory_scrape.inspect": 4,
    "guangya.directory_scrape.search": 4,
    "guangya.directory_scrape.preview": 4,
    "guangya.directory_scrape.run": 3,
    "guangya.organize.schedule_policy": 12,
    "guangya.organize.set_schedule_policy": 4,
    "guangya.organize.cleanup.preview": 4,
    "guangya.organize.cleanup.classify": 8,
    "guangya.organize.cleanup.execute": 3,
    "guangya.organize.preview": 4,
    "indexer.search_resources": 6,
    "indexer.diagnose_readiness": 4,
    "library.audit_library_episodes": 2,
    "library.start_episode_audit": 2,
    "library.trigger_patrol_now": 2,
    "library.patrol_status": 12,
    "library.audit_episodes": 6,
    "library.check_updates": 6,
    "media.subscription_updates": 4,
    "media.create_subscription": 4,
    "media.delete_subscription": 3,
    "media.set_subscription_enabled": 4,
    "media.continue_watching": 6,
    "media.recently_added": 6,
    "media.recently_played": 6,
    "media.recommend_from_library": 4,
    "media.preferences": 12,
    "media.set_preferences": 4,
    "media.clear_preferences": 4,
    "media.today_summary": 8,
    "media.subscription_notification_rule": 12,
    "media.set_subscription_notification_rule": 4,
    "media.reset_subscription_notification_rule": 4,
    "library.search_missing_episode_resources": 4,
    "library.search_missing_season_resources": 4,
    "library.missing_media_workflows": 12,
    "library.search": 12,
    "library.batch_presence": 4,
    "downloads.diagnose_queue": 4,
    "downloads.retry_submission": 3,
    "ingest.inspect": 6,
    "ingest.submit": 4,
    "ingest.status": 12,
    "rss.diagnose": 4,
    "rss.subscription_summaries": 8,
    "rss.recent_activity": 8,
    "rss.get_subscription_summary": 12,
    "rss.refresh_subscription": 3,
    "rss.refresh_subscriptions": 3,
    "rss.create_subscription": 4,
    "rss.update_subscription": 4,
    "rss.delete_subscription": 4,
    "rss.entry_summaries": 8,
    "rss.mark_entries": 4,
    "rss.submit_entries_to_qb": 3,
    "rss.submit_pending_to_qb": 3,
    "rss.retry_failed_to_qb": 3,
    "automation.diagnose_pipeline": 4,
    "local_media.diagnose": 4,
    "local_media.source_summaries": 8,
    "local_media.get_source_summary": 8,
    "local_media.set_source_trigger_enabled": 4,
    "local_media.scan_sources": 3,
    "local_media.review_queue_summary": 4,
    "local_media.history_summary": 4,
    "local_media.task_summaries": 8,
    "local_media.inspect_task": 8,
    "local_media.preview_task": 8,
    "local_media.verify_task_library_visibility": 8,
    "local_media.retry_task": 4,
    "local_media.refresh_task_library": 4,
    "organize.audit_logs": 4,
    "strm.run_history": 8,
    "strm.triage_failures": 4,
    "strm.retry_failures": 3,
    "strm.schedule_policy": 12,
    "strm.set_schedule_policy": 4,
    "workspace.search": 4,
    "workspace.todo": 4,
    "workspace.next_actions": 4,
    "workspace.briefing": 4,
    "workspace.health": 4,
}


class AgentRateLimiter:
    """线程安全的固定窗口滑动限流器，并对身份状态做有界回收。"""

    def __init__(
        self,
        *,
        max_keys: int = 4096,
        cleanup_interval: int = 128,
        clock: Callable[[], float] = time.monotonic,
        shared: bool = False,
    ) -> None:
        if max_keys < 1:
            raise ValueError("max_keys 必须大于 0")
        if cleanup_interval < 1:
            raise ValueError("cleanup_interval 必须大于 0")
        self._events: dict[str, deque[float]] = {}
        self._expires_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys
        self._cleanup_interval = cleanup_interval
        self._clock = clock
        self._operations = 0
        self._shared = bool(shared)

    def allow(self, key: str, *, limit: int, window_seconds: int, cost: int = 1) -> bool:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or window_seconds < 1
        ):
            raise ValueError("window_seconds 必须是正整数")
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 1 or cost > limit:
            raise ValueError("cost 必须是 1 到 limit 的整数")

        normalized_key = str(key)
        if self._shared:
            return self._allow_shared(
                normalized_key, limit=limit,
                window_seconds=window_seconds, cost=cost,
            )
        with self._lock:
            now = self._clock()
            self._operations += 1
            if self._operations % self._cleanup_interval == 0:
                self._prune_expired_locked(now)

            events = self._events.get(normalized_key)
            if events is not None:
                while events and now - events[0] >= window_seconds:
                    events.popleft()
                if not events:
                    self._events.pop(normalized_key, None)
                    self._expires_at.pop(normalized_key, None)
                    events = None

            if events is None:
                if len(self._events) >= self._max_keys:
                    self._prune_expired_locked(now)
                if len(self._events) >= self._max_keys:
                    # 身份空间已满时保守拒绝新 key，避免攻击者通过轮换 key 淘汰活跃预算。
                    return False
                events = deque()
                self._events[normalized_key] = events

            if len(events) + cost > limit:
                return False
            events.extend([now] * cost)
            self._expires_at[normalized_key] = now + window_seconds
            return True

    def tracked_keys(self) -> int:
        """返回清理过期状态后的当前身份数量，供诊断与测试使用。"""
        if self._shared:
            from app import database as db
            now_epoch = int(time.time())
            with db.get_conn() as conn:
                self._ensure_shared_table(conn)
                conn.execute(
                    "DELETE FROM agent_rate_limit_buckets WHERE expires_at<=?",
                    (now_epoch,),
                )
                row = conn.execute(
                    "SELECT COUNT(*) AS total FROM agent_rate_limit_buckets",
                ).fetchone()
            return int(row["total"] or 0)
        with self._lock:
            self._prune_expired_locked(self._clock())
            return len(self._events)

    def reset(self) -> None:
        if self._shared:
            from app import database as db
            with db.get_conn() as conn:
                self._ensure_shared_table(conn)
                conn.execute("DELETE FROM agent_rate_limit_buckets")
        with self._lock:
            self._events.clear()
            self._expires_at.clear()
            self._operations = 0

    @staticmethod
    def _ensure_shared_table(
        conn: object, *, legacy_window_seconds: int = 60,
    ) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_rate_limit_buckets ("
            "limiter_key TEXT PRIMARY KEY,window_start INTEGER NOT NULL,"
            "count INTEGER NOT NULL DEFAULT 0 CHECK(count>=0),"
            "expires_at INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL)"
        )
        columns = {
            str(row["name"] if hasattr(row, "keys") else row[1])
            for row in conn.execute("PRAGMA table_info(agent_rate_limit_buckets)").fetchall()
        }
        if "expires_at" not in columns:
            try:
                conn.execute(
                    "ALTER TABLE agent_rate_limit_buckets "
                    "ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0"
                )
            except Exception:
                refreshed = {
                    str(row["name"] if hasattr(row, "keys") else row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(agent_rate_limit_buckets)"
                    ).fetchall()
                }
                if "expires_at" not in refreshed:
                    raise
        now_epoch = int(time.time())
        legacy_window = max(1, int(legacy_window_seconds))
        # 历史表新增列时 DEFAULT 0 只代表“尚未迁移”，不能直接当作过期；
        # 先保守保留两个旧窗口，避免部署瞬间重置仍在生效的预算。
        conn.execute(
            "UPDATE agent_rate_limit_buckets SET expires_at="
            "MAX(window_start + ?, ?) WHERE expires_at<=0",
            (2 * legacy_window, now_epoch + legacy_window),
        )

    def _allow_shared(
        self, key: str, *, limit: int, window_seconds: int, cost: int
    ) -> bool:
        """单行固定窗口桶；跨 Worker 原子判定且不写高频事件日志。"""
        from app import database as db

        digest = hashlib.sha256(
            b"mediaflux-agent-rate:v1\0" + key.encode("utf-8", errors="replace")
        ).hexdigest()
        now_epoch = int(time.time())
        window_start = (now_epoch // window_seconds) * window_seconds
        with db.get_conn() as conn:
            AgentRateLimiter._ensure_shared_table(
                conn, legacy_window_seconds=window_seconds,
            )
            # 旧 schema 回填可能开启隐式事务；先提交迁移，再进入预算判定的
            # BEGIN IMMEDIATE，避免同一连接嵌套事务。
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM agent_rate_limit_buckets WHERE expires_at<=?",
                (now_epoch,),
            )
            row = conn.execute(
                "SELECT window_start,count FROM agent_rate_limit_buckets "
                "WHERE limiter_key=?",
                (digest,),
            ).fetchone()
            if row is None:
                total = conn.execute(
                    "SELECT COUNT(*) AS total FROM agent_rate_limit_buckets"
                ).fetchone()
                if int(total["total"] or 0) >= self._max_keys:
                    # 共享身份空间已满时同样保守拒绝新 key，避免轮换身份
                    # 挤掉仍在生效的其他调用方预算。
                    return False
            carried = 0
            if row is not None:
                previous_start = int(row["window_start"])
                previous_count = int(row["count"])
                if previous_start == window_start:
                    carried = previous_count
                elif previous_start == window_start - window_seconds:
                    remaining = max(0, window_seconds - (now_epoch - window_start))
                    carried = (
                        previous_count * remaining + window_seconds - 1
                    ) // window_seconds
            new_count = carried + cost
            if new_count > limit:
                return False
            conn.execute(
                "INSERT INTO agent_rate_limit_buckets"
                "(limiter_key,window_start,count,expires_at,updated_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(limiter_key) DO UPDATE SET "
                "window_start=excluded.window_start,count=excluded.count,"
                "expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                (
                    digest,
                    window_start,
                    new_count,
                    window_start + 2 * window_seconds,
                    db.now(),
                ),
            )
            return True

    def _prune_expired_locked(self, now: float) -> None:
        expired = [
            key for key, expires_at in self._expires_at.items() if expires_at <= now
        ]
        for key in expired:
            self._events.pop(key, None)
            self._expires_at.pop(key, None)


def tool_rate_limit_policy(tool_name: str) -> tuple[str, int, int]:
    """返回直调与 LLM 回退共用的 ``scope / limit / cost``。"""
    name = str(tool_name or "").strip()
    scope = _TOOL_RATE_LIMIT_SCOPES.get(name, f"tool:{name}")
    limit = _TOOL_RATE_LIMITS.get(name, 30)
    cost = 2 if name in {
        "library.search_missing_season_resources",
        "media.subscription_updates",
    } else 1
    return scope, limit, cost


def allow_agent_tool(identity: str, tool_name: str) -> bool:
    """按调用身份和真实工具执行预算限流。"""
    owner = str(identity or "unknown").strip() or "unknown"
    scope, limit, cost = tool_rate_limit_policy(tool_name)
    return agent_rate_limiter.allow(
        f"{owner}:{scope}", limit=limit, window_seconds=60, cost=cost
    )


agent_rate_limiter = AgentRateLimiter(shared=True)
