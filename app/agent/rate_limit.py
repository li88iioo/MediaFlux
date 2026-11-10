"""Agent 工具的统一限流策略与跨 Worker 共享预算。"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

_TOOL_RATE_LIMIT_SCOPES = {
    "rss.refresh": "rss-refresh-write",
    "rss.mark": "rss-entry-write",
    "rss.download": "rss-pending-download",
    "rss.retry": "rss-failure-retry",
    "agent.action_history": "agent-action-history",
    "playback.instances": "media-proxy-status",
    "playback.test": "media-proxy-test",
    "playback.set_enabled": "media-proxy-control",
    "playback.restart": "media-proxy-control",
    "playback.failures": "media-proxy-playback-failures",
    "recognition.set_rule_enabled": "recognition-rule-control",
    "settings.features": "feature-summary",
    "settings.policies": "safe-policy-summary",
    "settings.set_policy": "safe-policy-control",
    "settings.test_notification": "telegram-test-notification",
    "diagnostics.component": "config-component-explain",
    "downloads.diagnose": "download-queue-diagnosis",
    "downloads.pause": "tool:provider.change.execute",
    "downloads.resume": "tool:provider.change.execute",
    "downloads.remove": "tool:provider.change.execute",
    "downloads.retry": "download-submission-retry",
    "cloud.set_organize_schedule": "guangya-organize-schedule-policy-write",
    "downloads.submit": "ingest-write",
    "cloud.transfer_share": "ingest-write",
    "cloud.inspect_share": "ingest-read",
    "resources.download": "ingest-write",
    "cloud.scrape": "guangya-directory-scrape-write",
    "cloud.restore": "guangya.recycle.restore",
    "cloud.clear_recycle": "guangya.recycle.clear",
    "subscriptions.create": "media-subscription-control",
    "subscriptions.delete": "media-subscription-control",
    "subscriptions.update": "media-subscription-control",
    "preferences.update": "media-preferences",
    "preferences.clear": "media-preferences",
    "subscriptions.today": "media-today-summary",
    "subscriptions.notification": "media-subscription-notification-read",
    "subscriptions.set_notification": "media-subscription-notification-control",
    "subscriptions.reset_notification": "media-subscription-notification-control",
    "diagnostics.pipeline": "automation-pipeline-diagnosis",
    "local.diagnose": "local-media-diagnosis",
    "local.sources": "local-media-source-read",
    "local.source": "local-media-source-read",
    "local.create_source": "local-media-source-control",
    "local.update_source": "local-media-source-control",
    "local.delete_source": "local-media-source-control",
    "local.scan_sources": "local-media-source-control",
    "local.tasks": "local-media-tasks",
    "local.task": "local-media-tasks",
    "local.preview": "local-media-tasks",
    "local.verify_library": "local-media-tasks",
    "local.retry": "local-media-task-write",
    "local.refresh_library": "local-media-task-write",
    "strm.failures": "strm-failure-triage",
    "strm.retry_failures": "strm-failure-retry",
    "strm.set_schedule": "strm-schedule-policy-write",
    "library.start_audit": "library-full-audit",
    "library.run_patrol": "library-full-audit",
    "library.patrol_status": "library-patrol-status",
    "subscriptions.check_updates": "media-subscription-updates",
    "media.confirm_mapping": "media-mapping-write",
    "watchlist.add": "discovery-watchlist-write",
    "watchlist.remove": "discovery-watchlist-write",
}

_TOOL_RATE_LIMITS = {
    "rss.refresh": 3,
    "rss.mark": 4,
    "rss.download": 3,
    "rss.retry": 3,
    "agent.action_history": 12,
    "playback.instances": 12,
    "playback.test": 4,
    "playback.set_enabled": 4,
    "playback.restart": 3,
    "playback.failures": 8,
    "recognition.set_rule_enabled": 4,
    "settings.features": 12,
    "settings.policies": 12,
    "settings.set_policy": 4,
    "settings.test_notification": 3,
    "diagnostics.component": 12,
    "media.confirm_mapping": 4,
    "media.refresh": 3,
    "watchlist.add": 4,
    "watchlist.remove": 4,
    # 预检读取快照；过期后的重新预检也计一次。云端写入由持久化队列串行执行。
    "cloud.change": 6,
    "cloud.scrape": 3,
    "cloud.restore": 3,
    "cloud.clear_recycle": 2,
    "cloud.shares": 8,
    "cloud.share": 3,
    "cloud.revoke_share": 3,
    "cloud.set_organize_schedule": 4,
    "cloud.preview_organize": 4,
    "library.start_audit": 2,
    "library.run_patrol": 2,
    "library.patrol_status": 12,
    "subscriptions.check_updates": 4,
    "subscriptions.create": 4,
    "subscriptions.delete": 3,
    "subscriptions.update": 4,
    "preferences.update": 4,
    "preferences.clear": 4,
    "subscriptions.today": 8,
    "subscriptions.notification": 12,
    "subscriptions.set_notification": 4,
    "subscriptions.reset_notification": 4,
    "downloads.diagnose": 4,
    "downloads.pause": 3,
    "downloads.resume": 3,
    "downloads.remove": 3,
    "downloads.retry": 3,
    "cloud.transfer_share": 4,
    "cloud.inspect_share": 6,
    "downloads.submit": 4,
    "resources.download": 4,
    "diagnostics.pipeline": 4,
    "local.diagnose": 4,
    "local.sources": 8,
    "local.source": 8,
    "local.create_source": 4,
    "local.update_source": 4,
    "local.delete_source": 3,
    "local.scan_sources": 3,
    "local.tasks": 8,
    "local.task": 8,
    "local.preview": 8,
    "local.verify_library": 8,
    "local.retry": 4,
    "local.refresh_library": 4,
    "strm.failures": 4,
    "strm.retry_failures": 3,
    "strm.set_schedule": 4,
}


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Atomic permit snapshot, not a reservation for a future retry.

    Remaining units are measured after consumption (or at rejection). Memory
    deadlines have no epoch representation; shared deadlines use wall time.
    """

    allowed: bool
    limit: int
    cost: int
    remaining: int
    retry_after_seconds: float | None
    retry_at_epoch: float | None
    reason: str


class AgentRateLimiter:
    """线程安全的滑动窗口限流器，并对身份状态做有界回收。"""

    def __init__(
        self,
        *,
        max_keys: int = 4096,
        cleanup_interval: int = 128,
        clock: Callable[[], float] = time.monotonic,
        shared: bool = False,
        wall_clock: Callable[[], float] | None = None,
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
        self._wall_clock = wall_clock
        self._operations = 0
        self._shared = shared

    def allow(self, key: str, *, limit: int, window_seconds: int, cost: int = 1) -> bool:
        return self.consume(
            key, limit=limit, window_seconds=window_seconds, cost=cost
        ).allowed

    def consume(
        self, key: str, *, limit: int, window_seconds: int, cost: int = 1
    ) -> RateLimitDecision:
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

        normalized_key = key
        if self._shared:
            return self._consume_shared(
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
                    retry = min(self._expires_at.values()) - now
                    return RateLimitDecision(
                        False, limit, cost, limit, retry, None, "capacity"
                    )
                events = deque()
                self._events[normalized_key] = events

            if len(events) + cost > limit:
                gap = len(events) + cost - limit
                retry = events[gap - 1] + window_seconds - now
                return RateLimitDecision(
                    False, limit, cost, max(0, limit - len(events)),
                    retry, None, "quota",
                )
            events.extend([now] * cost)
            self._expires_at[normalized_key] = now + window_seconds
            return RateLimitDecision(
                True, limit, cost, limit - len(events), 0.0, None, "allowed"
            )

    def _wall_now(self) -> float:
        return time.time() if self._wall_clock is None else self._wall_clock()

    def tracked_keys(self) -> int:
        """返回清理过期状态后的当前身份数量，供诊断与测试使用。"""
        if self._shared:
            from app import database as db
            now_epoch = int(self._wall_now())
            with db.get_conn() as conn:
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
                conn.execute("DELETE FROM agent_rate_limit_buckets")
        with self._lock:
            self._events.clear()
            self._expires_at.clear()
            self._operations = 0

    def _consume_shared(
        self, key: str, *, limit: int, window_seconds: int, cost: int
    ) -> RateLimitDecision:
        """单行双窗口计数；只衰减上一个窗口原始计数，不重复结转历史权重。"""
        from app import database as db

        digest = hashlib.sha256(
            b"mediaflux-agent-rate:v1\0" + key.encode("utf-8", errors="replace")
        ).hexdigest()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            wall_now = self._wall_now()
            now_epoch = int(wall_now)
            window_start = (now_epoch // window_seconds) * window_seconds
            conn.execute(
                "DELETE FROM agent_rate_limit_buckets WHERE expires_at<=?",
                (now_epoch,),
            )
            row = conn.execute(
                "SELECT window_start,count,previous_count FROM agent_rate_limit_buckets "
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
                    expiry = conn.execute(
                        "SELECT MIN(expires_at) AS earliest FROM agent_rate_limit_buckets"
                    ).fetchone()["earliest"]
                    return RateLimitDecision(
                        False, limit, cost, limit,
                        float(expiry) - wall_now, float(expiry), "capacity",
                    )
            current_count = previous_count = 0
            if row is not None:
                previous_start = int(row["window_start"])
                if now_epoch < previous_start:
                    # A future bucket is not empty. Preserve its counters and
                    # do not promise an exact retry on wall-clock rollback.
                    return RateLimitDecision(
                        False, limit, cost,
                        max(0, limit - int(row["count"]) - int(row["previous_count"])),
                        None, None, "clock_anomaly",
                    )
                if previous_start == window_start:
                    current_count = int(row["count"])
                    previous_count = int(row["previous_count"])
                elif previous_start == window_start - window_seconds:
                    previous_count = int(row["count"])
            remaining = max(0, window_seconds - (now_epoch - window_start))
            carried = (previous_count * remaining + window_seconds - 1) // window_seconds
            new_count = current_count + cost
            if new_count + carried > limit:
                retry_at = self._shared_retry_at(
                    window_start, window_seconds, current_count, previous_count,
                    limit - cost,
                )
                return RateLimitDecision(
                    False, limit, cost, max(0, limit - current_count - carried),
                    retry_at - wall_now, float(retry_at), "quota",
                )
            conn.execute(
                "INSERT INTO agent_rate_limit_buckets"
                "(limiter_key,window_start,count,previous_count,expires_at,updated_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(limiter_key) DO UPDATE SET "
                "window_start=excluded.window_start,count=excluded.count,"
                "previous_count=excluded.previous_count,"
                "expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                (
                    digest,
                    window_start,
                    new_count,
                    previous_count,
                    window_start + 2 * window_seconds,
                    db.now(),
                ),
            )
            return RateLimitDecision(
                True, limit, cost, limit - new_count - carried,
                0.0, None, "allowed",
            )

    @staticmethod
    def _shared_retry_at(
        start: int, window: int, current: int, previous: int, available: int
    ) -> int:
        """Solve the unchanged ceil-weighted rule, without looping over time.

        ceil(previous * (W-offset) / W) <= allowance iff the numerator
        is <= allowance*W. If this window cannot fit, current becomes
        previous next window; its first feasible integer epoch is <=2W.
        """
        allowance = available - current
        if allowance >= 0 and previous:
            offset = max(0, window - allowance * window // previous)
            if offset < window:
                return start + offset
        offset = max(0, window - available * window // current) if current else 0
        return start + window + offset

    def _prune_expired_locked(self, now: float) -> None:
        expired = [
            key for key, expires_at in self._expires_at.items() if expires_at <= now
        ]
        for key in expired:
            self._events.pop(key, None)
            self._expires_at.pop(key, None)


def tool_rate_limit_policy(tool_name: str) -> tuple[str, int, int]:
    """返回直调与 LLM 回退共用的 ``scope / limit / cost``。"""
    name = (tool_name or "").strip()
    scope = _TOOL_RATE_LIMIT_SCOPES.get(name, f"tool:{name}")
    limit = _TOOL_RATE_LIMITS.get(name, 30)
    cost = 2 if name in {
        "subscriptions.check_updates",
    } else 1
    return scope, limit, cost


def consume_agent_tool(
    identity: str, tool_name: str, *, scope_suffix: str = ""
) -> RateLimitDecision:
    """按调用身份和真实工具执行预算限流。"""
    owner = (identity or "unknown").strip() or "unknown"
    scope, limit, cost = tool_rate_limit_policy(tool_name)
    if scope_suffix:
        scope = f"{scope}:{scope_suffix}"
    return agent_rate_limiter.consume(
        f"{owner}:{scope}", limit=limit, window_seconds=60, cost=cost
    )


agent_rate_limiter = AgentRateLimiter(shared=True)
