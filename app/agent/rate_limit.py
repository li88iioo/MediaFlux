"""Agent API 与 LLM 回退共用的轻量进程内限流。"""
from __future__ import annotations

import threading
import time
import hashlib
from collections import deque
from collections.abc import Callable


_TOOL_RATE_LIMIT_SCOPES = {
    "agent.action_history": "agent-action-history",
    "config.test_media_server": "media-server-test",
    "media_proxy.status_summary": "media-proxy-status",
    "media_proxy.test_instance": "media-proxy-test",
    "media_proxy.set_instance_enabled": "media-proxy-control",
    "recognition.set_rule_enabled": "recognition-rule-control",
    "config.diagnose_media_servers": "media-server-diagnosis",
    "config.feature_summary": "feature-summary",
    "config.safe_policy_summary": "safe-policy-summary",
    "config.set_safe_policy": "safe-policy-control",
    "telegram.send_test_notification": "telegram-test-notification",
    "config.explain_component": "config-component-explain",
    "library.search_missing_episode_resources": "missing-episode-resources",
    "library.search_missing_season_resources": "missing-episode-resources",
    "library.missing_media_workflows": "missing-media-workflows",
    "downloads.diagnose_queue": "download-queue-diagnosis",
    "downloads.pause_task": "download-task-control",
    "downloads.resume_task": "download-task-control",
    "downloads.delete_task": "download-task-control",
    "downloads.retry_submission": "download-submission-retry",
    "guangya.connection_status": "guangya-connection-status",
    "guangya.organize.schedule_policy": "guangya-organize-schedule-policy",
    "guangya.organize.set_schedule_policy": "guangya-organize-schedule-policy-write",
    "guangya.organize.clean_empty": "guangya-organize-clean-empty",
    "rss.diagnose": "rss-diagnosis",
    "rss.subscription_summaries": "rss-subscription-summary",
    "rss.recent_activity": "rss-subscription-summary",
    "rss.get_subscription_summary": "rss-subscription-summary",
    "rss.set_subscription_enabled": "rss-subscription-control",
    "rss.set_refresh_interval": "rss-subscription-control",
    "rss.delete_subscription": "rss-subscription-control",
    "media.create_subscription": "media-subscription-control",
    "media.delete_subscription": "media-subscription-control",
    "media.set_subscription_enabled": "media-subscription-control",
    "rss.submit_pending_to_qb": "rss-pending-download",
    "rss.retry_failed_to_qb": "rss-failure-retry",
    "automation.diagnose_pipeline": "automation-pipeline-diagnosis",
    "local_media.diagnose": "local-media-diagnosis",
    "local_media.source_summaries": "local-media-source-read",
    "local_media.get_source_summary": "local-media-source-read",
    "local_media.set_source_trigger_enabled": "local-media-source-control",
    "local_media.review_queue_summary": "local-media-diagnosis",
    "local_media.history_summary": "local-media-diagnosis",
    "organize.audit_logs": "organize-audit-logs",
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
    "library.patrol_status": "library-patrol-status",
    "library.audit_episodes": "library-update-check",
    "library.check_updates": "library-update-check",
    "media.subscription_updates": "media-subscription-updates",
    "discovery.recommend": "discovery-recommend",
    "discovery.watchlist_summaries": "discovery-watchlist-read",
    "discovery.get_watchlist_summary": "discovery-watchlist-read",
    "discovery.add_watchlist": "discovery-watchlist-write",
    "discovery.remove_watchlist": "discovery-watchlist-write",
    "bangumi.calendar": "bangumi-calendar",
    "web.search": "web-search",
}

_TOOL_RATE_LIMITS = {
    "agent.action_history": 12,
    "config.test_media_server": 6,
    "media_proxy.status_summary": 12,
    "media_proxy.test_instance": 4,
    "media_proxy.set_instance_enabled": 4,
    "recognition.set_rule_enabled": 4,
    "config.diagnose_media_servers": 4,
    "config.feature_summary": 12,
    "config.safe_policy_summary": 12,
    "config.set_safe_policy": 4,
    "telegram.send_test_notification": 3,
    "config.explain_component": 12,
    "discovery.search": 6,
    "discovery.recommend": 6,
    "discovery.watchlist_summaries": 8,
    "discovery.get_watchlist_summary": 12,
    "discovery.add_watchlist": 4,
    "discovery.remove_watchlist": 4,
    "bangumi.calendar": 6,
    "web.search": 6,
    "guangya.connection_status": 4,
    "guangya.organize.schedule_policy": 12,
    "guangya.organize.set_schedule_policy": 4,
    "guangya.organize.clean_empty": 4,
    "guangya.organize.preview": 4,
    "indexer.search_resources": 6,
    "indexer.diagnose_readiness": 4,
    "library.audit_library_episodes": 2,
    "library.start_episode_audit": 2,
    "library.patrol_status": 12,
    "library.audit_episodes": 6,
    "library.check_updates": 6,
    "media.subscription_updates": 4,
    "media.create_subscription": 4,
    "media.delete_subscription": 3,
    "media.set_subscription_enabled": 4,
    "library.search_missing_episode_resources": 4,
    "library.search_missing_season_resources": 4,
    "library.missing_media_workflows": 12,
    "library.search": 12,
    "downloads.diagnose_queue": 4,
    "downloads.pause_task": 4,
    "downloads.resume_task": 4,
    "downloads.delete_task": 4,
    "downloads.retry_submission": 3,
    "rss.diagnose": 4,
    "rss.subscription_summaries": 8,
    "rss.recent_activity": 8,
    "rss.get_subscription_summary": 12,
    "rss.set_subscription_enabled": 4,
    "rss.set_refresh_interval": 4,
    "rss.delete_subscription": 4,
    "rss.submit_pending_to_qb": 3,
    "rss.retry_failed_to_qb": 3,
    "automation.diagnose_pipeline": 4,
    "local_media.diagnose": 4,
    "local_media.source_summaries": 8,
    "local_media.get_source_summary": 8,
    "local_media.set_source_trigger_enabled": 4,
    "local_media.review_queue_summary": 4,
    "local_media.history_summary": 4,
    "organize.audit_logs": 4,
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
                row = conn.execute(
                    "SELECT COUNT(*) AS total FROM agent_rate_limit_buckets "
                    "WHERE window_start>?",
                    (now_epoch - 86400,),
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
    def _ensure_shared_table(conn: object) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_rate_limit_buckets ("
            "limiter_key TEXT PRIMARY KEY,window_start INTEGER NOT NULL,"
            "count INTEGER NOT NULL DEFAULT 0 CHECK(count>=0),updated_at TEXT NOT NULL)"
        )

    @staticmethod
    def _allow_shared(
        key: str, *, limit: int, window_seconds: int, cost: int
    ) -> bool:
        """单行固定窗口桶；跨 Worker 原子判定且不写高频事件日志。"""
        from app import database as db

        digest = hashlib.sha256(
            b"mediaflux-agent-rate:v1\0" + key.encode("utf-8", errors="replace")
        ).hexdigest()
        now_epoch = int(time.time())
        window_start = (now_epoch // window_seconds) * window_seconds
        with db.get_conn() as conn:
            AgentRateLimiter._ensure_shared_table(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT window_start,count FROM agent_rate_limit_buckets "
                "WHERE limiter_key=?",
                (digest,),
            ).fetchone()
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
                "(limiter_key,window_start,count,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(limiter_key) DO UPDATE SET "
                "window_start=excluded.window_start,count=excluded.count,"
                "updated_at=excluded.updated_at",
                (digest, window_start, new_count, db.now()),
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
