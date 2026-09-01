"""Jellyfin / Emby 刷新合并器。

所有自动整理入口只登记变化路径；单一后台消费者在 quiet window 后合并执行，
并将媒体服务器刷新失败留在持久队列中重试，而不会重新执行整理或 STRM 生成。
"""
from __future__ import annotations

import threading
import uuid
from typing import Any

from app import config
from app.logger import get_logger, redact_sensitive_text
from app.modules.media_server_path_mapping import configured_media_server_refresh_options
from app.modules.media_server_profiles import list_configured_profiles
from app.modules.process_lock import CrossProcessLock
from app.repositories.media_refresh_queue import (
    claim_due_media_refreshes,
    complete_media_refresh,
    defer_media_refresh,
    enqueue_media_refresh,
    fail_media_refresh,
    media_refresh_queue_status,
    next_media_refresh_due_in,
    recent_media_refresh_target_ids,
    recover_media_refresh_leases,
)

logger = get_logger(__name__)

_DEFAULT_DEBOUNCE_SECONDS = 20
_DEFAULT_RECENT_TTL_SECONDS = 90
_DEFAULT_RETRY_SECONDS = 30


def _debounce_seconds() -> int:
    return max(5, min(config.get_int(
        "MEDIA_REFRESH_DEBOUNCE_SECONDS", _DEFAULT_DEBOUNCE_SECONDS,
    ), 300))


def _recent_ttl_seconds() -> int:
    return max(10, min(config.get_int(
        "MEDIA_REFRESH_DEDUPE_SECONDS", _DEFAULT_RECENT_TTL_SECONDS,
    ), 1800))


def _retry_seconds(attempts: int) -> int:
    base = max(10, min(config.get_int(
        "MEDIA_REFRESH_RETRY_BASE_SECONDS", _DEFAULT_RETRY_SECONDS,
    ), 600))
    return min(1800, base * (2 ** min(max(0, int(attempts)), 5)))


def _configured_provider_names(*, allow_emby: bool = True) -> tuple[str, ...]:
    providers: list[str] = []
    if (
        config.get_bool("JELLYFIN_ENABLED")
        and config.get("JELLYFIN_URL")
        and config.get("JELLYFIN_API_KEY")
    ):
        providers.append("jellyfin")
    if (
        allow_emby
        and config.get_bool("EMBY_ENABLED")
        and config.get("EMBY_URL")
        and config.get("EMBY_TOKEN")
    ):
        providers.append("emby")
    return tuple(providers)


def enqueue_media_refresh_paths(
    paths: list[str] | tuple[str, ...] | set[str],
    *,
    providers: tuple[str, ...] | list[str] | None = None,
    allowed_library_ids: tuple[str, ...] = (),
    immediate: bool = False,
    allow_emby: bool = True,
) -> dict[str, str]:
    """把变化路径持久加入统一队列，返回逐 provider 的排队状态。"""
    selected = tuple(dict.fromkeys(
        str(item or "").strip().lower()
        for item in (
            providers if providers is not None
            else _configured_provider_names(allow_emby=allow_emby)
        )
        if str(item or "").strip()
    ))
    results: dict[str, str] = {}
    delay = 0 if immediate else _debounce_seconds()
    for provider in selected:
        label = {"jellyfin": "Jellyfin", "emby": "Emby"}.get(
            provider, provider or "unknown",
        )
        try:
            queued = enqueue_media_refresh(
                provider,
                paths,
                allowed_library_ids=allowed_library_ids,
                debounce_seconds=delay,
            )
        except Exception as exc:
            logger.warning(
                "%s 媒体库刷新入队失败 type=%s", label, type(exc).__name__,
            )
            results[label] = "failed"
            continue
        if queued.get("group_key"):
            results[label] = "queued"
        else:
            results[label] = "skipped"
    if any(value == "queued" for value in results.values()):
        get_media_refresh_coordinator().wake()
    return results


class MediaRefreshCoordinator:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._state_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._worker_lock = CrossProcessLock("media-refresh-coordinator")
        self._owner = f"media-refresh-{uuid.uuid4().hex[:12]}"
        self._consumer_active = False
        self._last_error_type = ""
        self._completed_session = 0
        self._failed_session = 0

    def start(self) -> None:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="media-refresh-coordinator",
                daemon=True,
            )
            self._thread.start()
        self._wake_event.set()
        logger.info("媒体库刷新合并器已启动")

    def stop(self, timeout: float = 30.0) -> bool:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, float(timeout or 0.1)))
        stopped = not thread or not thread.is_alive()
        if stopped:
            with self._state_lock:
                if self._thread is thread:
                    self._thread = None
        else:
            logger.warning("媒体库刷新合并器未能在关闭超时内结束；持久队列将在重启后恢复")
        return stopped

    def wake(self) -> None:
        self._wake_event.set()

    def status(self) -> dict[str, Any]:
        try:
            queue = media_refresh_queue_status()
        except Exception:
            queue = {"queued": 0, "running": 0, "retry_wait": 0, "paths": 0, "recent": 0}
        with self._state_lock:
            return {
                **queue,
                "worker_running": bool(self._thread and self._thread.is_alive()),
                "consumer_active": bool(self._consumer_active),
                "last_error_type": self._last_error_type,
                "completed_session": int(self._completed_session),
                "failed_session": int(self._failed_session),
            }

    def _loop(self) -> None:
        owns_lock = False
        try:
            while not self._stop_event.is_set():
                if self._worker_lock.acquire(blocking=False):
                    owns_lock = True
                    break
                self._stop_event.wait(1.0)
            if not owns_lock:
                return
            recovered = recover_media_refresh_leases()
            if recovered:
                logger.warning("已恢复中断的媒体库刷新请求 count=%s", recovered)
            with self._state_lock:
                self._consumer_active = True
            while not self._stop_event.is_set():
                worked = False
                try:
                    claimed = claim_due_media_refreshes(
                        owner=self._owner,
                        lease_seconds=300,
                        limit=8,
                    )
                    for group in claimed:
                        worked = True
                        self._process_group(group)
                        if self._stop_event.is_set():
                            break
                except Exception as exc:
                    with self._state_lock:
                        self._last_error_type = type(exc).__name__
                    logger.exception("媒体库刷新合并器轮询异常")
                if worked:
                    continue
                try:
                    due_in = next_media_refresh_due_in()
                except Exception:
                    due_in = None
                wait_seconds = 5.0 if due_in is None else max(0.1, min(due_in, 5.0))
                self._wake_event.wait(wait_seconds)
                self._wake_event.clear()
        finally:
            with self._state_lock:
                self._consumer_active = False
            if owns_lock:
                try:
                    self._worker_lock.release()
                except Exception:
                    logger.exception("释放媒体库刷新合并器跨进程锁失败")

    @staticmethod
    def _client_for(provider: str):
        profiles = {
            item.server_type: item for item in list_configured_profiles()
            if item.enabled and item.configured
        }
        profile = profiles.get(provider)
        if profile is None:
            return None
        if provider == "jellyfin":
            from app.clients.jellyfin import JellyfinClient

            return JellyfinClient(
                profile.url,
                profile.credential,
                **configured_media_server_refresh_options("jellyfin"),
            )
        if provider == "emby":
            from app.clients.emby import EmbyClient

            return EmbyClient(
                profile.url,
                profile.credential,
                **configured_media_server_refresh_options("emby"),
            )
        return None

    def _process_group(self, group: dict[str, Any]) -> None:
        provider = str(group.get("provider") or "").strip().lower()
        group_key = str(group.get("group_key") or "")
        generation = int(group.get("lease_generation") or 0)
        attempts = int(group.get("attempts") or 0)
        client = None
        try:
            client = self._client_for(provider)
            if client is None:
                logger.warning(
                    "媒体库刷新 provider 已停用或配置不完整，将保留任务等待配置恢复 provider=%s",
                    provider,
                )
                fail_media_refresh(
                    group_key,
                    owner=self._owner,
                    lease_generation=generation,
                    error="媒体服务器已停用或配置不完整",
                    retry_seconds=_retry_seconds(attempts),
                    recent_ttl_seconds=_recent_ttl_seconds(),
                )
                with self._state_lock:
                    self._failed_session += 1
                    self._last_error_type = "MediaServerUnavailable"
                return
            recent_ids = recent_media_refresh_target_ids(provider)
            outcome = client.refresh_for_paths(
                list(group.get("paths") or []),
                allowed_library_ids=tuple(group.get("allowed_library_ids") or ()),
                allow_global_fallback=False,
                skip_item_ids=recent_ids,
            )
            scope = str(outcome.get("scope") or "unknown")
            logger.info(
                "%s 合并刷新完成 scope=%s ok=%s requested=%s items=%s folders=%s "
                "libraries=%s deduplicated=%s retryable=%s reason=%s",
                client.display_name,
                scope,
                bool(outcome.get("ok")),
                int(outcome.get("requested") or 0),
                len(outcome.get("items") or []),
                len(outcome.get("folders") or []),
                len(outcome.get("libraries") or []),
                int(outcome.get("deduplicated") or 0),
                bool(outcome.get("retryable")),
                outcome.get("fallback") or "-",
            )
            if outcome.get("retryable"):
                fail_media_refresh(
                    group_key,
                    owner=self._owner,
                    lease_generation=generation,
                    error=outcome.get("fallback") or "媒体服务器刷新失败",
                    retry_seconds=_retry_seconds(attempts),
                    refreshed_target_ids=outcome.get("succeeded_target_ids") or (),
                    recent_ttl_seconds=_recent_ttl_seconds(),
                )
                with self._state_lock:
                    self._failed_session += 1
                    self._last_error_type = "MediaRefreshRetryable"
                return
            deduplicated = int(outcome.get("deduplicated") or 0)
            if deduplicated:
                # 同一媒体目标刚刷新过时，新的变化路径仍可能包含随后入库的剧集。
                # 等去重窗口结束后再校准一次，不能把这批路径直接确认丢弃。
                defer_media_refresh(
                    group_key,
                    owner=self._owner,
                    lease_generation=generation,
                    delay_seconds=_recent_ttl_seconds(),
                    reason=f"等待媒体库刷新去重窗口结束（{deduplicated} 项）",
                    refreshed_target_ids=outcome.get("succeeded_target_ids") or (),
                    recent_ttl_seconds=_recent_ttl_seconds(),
                )
                return
            complete_media_refresh(
                group_key,
                owner=self._owner,
                lease_generation=generation,
                refreshed_target_ids=outcome.get("succeeded_target_ids") or (),
                recent_ttl_seconds=_recent_ttl_seconds(),
            )
            with self._state_lock:
                self._completed_session += 1
                self._last_error_type = ""
            if outcome.get("succeeded_target_ids"):
                from app.services import clear_dashboard_cache

                clear_dashboard_cache()
        except Exception as exc:
            safe_error = redact_sensitive_text(str(exc))[:300]
            try:
                fail_media_refresh(
                    group_key,
                    owner=self._owner,
                    lease_generation=generation,
                    error=safe_error or type(exc).__name__,
                    retry_seconds=_retry_seconds(attempts),
                )
            except Exception:
                logger.exception("媒体库刷新失败后更新重试状态异常")
            with self._state_lock:
                self._failed_session += 1
                self._last_error_type = type(exc).__name__
            logger.warning(
                "媒体库合并刷新失败 provider=%s type=%s，将仅重试刷新",
                provider,
                type(exc).__name__,
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


_coordinator = MediaRefreshCoordinator()


def get_media_refresh_coordinator() -> MediaRefreshCoordinator:
    return _coordinator
