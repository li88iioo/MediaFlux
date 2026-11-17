"""Jellyfin / Emby 刷新合并器。

所有自动整理入口只登记变化路径；单一后台消费者在 quiet window 后合并执行，
失败记录原因后结束本次刷新，由用户在媒体服务器手动处理；不重新执行整理或 STRM 生成。
"""
from __future__ import annotations

import threading
import uuid
from typing import Any

from app import config
from app.clients.base import close_media_server_client
from app.logger import get_logger, redact_sensitive_text
from app.modules.media_server_profiles import MediaRefreshIntent, configured_media_refresh_targets
from app.modules.process_lock import CrossProcessLock
from app.repositories.media_refresh_queue import (
    claim_due_media_refreshes,
    settle_media_refresh,
    enqueue_media_refresh,
    media_refresh_queue_status,
    next_media_refresh_due_in,
    recent_media_refresh_target_ids,
    recover_media_refresh_leases,
    register_media_refresh_handoff,
)

logger = get_logger(__name__)

_DEFAULT_DEBOUNCE_SECONDS = 20
_DEFAULT_RECENT_TTL_SECONDS = 90


def _debounce_seconds() -> int:
    return max(5, min(config.get_int(
        "MEDIA_REFRESH_DEBOUNCE_SECONDS", _DEFAULT_DEBOUNCE_SECONDS,
    ), 300))


def _recent_ttl_seconds() -> int:
    return max(10, min(config.get_int(
        "MEDIA_REFRESH_DEDUPE_SECONDS", _DEFAULT_RECENT_TTL_SECONDS,
    ), 1800))


def enqueue_media_refresh_paths(
    paths: list[str] | tuple[str, ...] | set[str],
    *,
    providers: tuple[str, ...] | list[str] | None = None,
    allowed_library_ids: tuple[str, ...] = (),
    library_binding: dict[str, str] | None = None,
    immediate: bool = False,
    allow_emby: bool = True,
    source_entries: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """持久登记刷新；无可用自动目标时明确返回全局 skipped，不创建待办。"""
    if source_entries is None:
        targets = configured_media_refresh_targets()
        selected = dict.fromkeys(item.strip().lower() for item in providers if item.strip()) if providers is not None else (
            provider for provider, target in targets.items()
            if target.profile.enabled and target.profile.configured and (allow_emby or provider != "emby")
        )
        batches = [
            (provider, targets[provider].intent() if provider in targets else None, paths, None)
            for provider in selected
        ]
    else:
        register_media_refresh_handoff(source_entries)
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for entry in source_entries:
            for provider, intent in (entry.get("refresh_targets") or {}).items():
                grouped.setdefault((provider, intent["upstream"], intent["mappings"]), []).append(entry)
        batches = [
            (provider, {"upstream": upstream, "mappings": mappings}, [entry["path"] for entry in batch], batch)
            for (provider, upstream, mappings), batch in grouped.items()
        ]
    results: dict[str, str] = {}
    delay = 0 if immediate else _debounce_seconds()
    for provider, intent, batch_paths, batch in batches:
        label = {"jellyfin": "Jellyfin", "emby": "Emby"}.get(
            provider, provider or "unknown",
        )
        try:
            queued = enqueue_media_refresh(
                provider,
                batch_paths,
                allowed_library_ids=allowed_library_ids,
                library_binding=library_binding,
                debounce_seconds=delay,
                source_entries=batch,
                intent=intent,
            )
        except Exception as exc:
            logger.warning(
                "%s 媒体库刷新入队失败 type=%s", label, type(exc).__name__,
            )
            results[label] = "failed"
            continue
        if results.get(label) != "failed":
            results[label] = "queued" if queued.get("group_key") else "skipped"
    if any(value == "queued" for value in results.values()):
        get_media_refresh_coordinator().wake()
    return results or {"媒体库": "skipped"}


class MediaRefreshCoordinator:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._state_lock = threading.RLock()
        # 启停串行；join 不持有 worker 更新状态时使用的状态锁。
        self._lifecycle_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._worker_lock = CrossProcessLock("media-refresh-coordinator")
        self._owner = f"media-refresh-{uuid.uuid4().hex[:12]}"
        self._consumer_active = False
        self._last_error_type = ""
        self._completed_session = 0
        self._failed_session = 0

    def start(self) -> None:
        with self._lifecycle_lock:
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
        with self._lifecycle_lock:
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
            queue = {"queued": 0, "running": 0, "paths": 0, "recent": 0}
        with self._state_lock:
            return {
                **queue,
                "worker_running": bool(self._thread and self._thread.is_alive()),
                "consumer_active": self._consumer_active,
                "last_error_type": self._last_error_type,
                "completed_session": self._completed_session,
                "failed_session": self._failed_session,
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
            recovery_pending = True
            while not self._stop_event.is_set():
                worked = False
                try:
                    if recovery_pending:
                        recovered = recover_media_refresh_leases()
                        if recovered:
                            logger.warning("已恢复中断的媒体库刷新请求 count=%s", recovered)
                        recovery_pending = False
                        with self._state_lock:
                            self._consumer_active = True
                            self._last_error_type = ""
                    claimed = claim_due_media_refreshes(
                        owner=self._owner,
                        lease_seconds=300,
                        limit=8,
                    )
                    self._reconcile_results()
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
    def _reconcile_results() -> None:
        from app.modules.scheduler import reconcile_strm_media_refresh_results
        from app.repositories.media_refresh_queue import collect_media_refresh_results

        reconcile_strm_media_refresh_results()
        collect_media_refresh_results()

    @staticmethod
    def _client_for(provider: str, intent: MediaRefreshIntent):
        target = configured_media_refresh_targets().get(provider)
        if target is None or not target.profile.enabled or not target.profile.configured:
            return None
        if target.intent() != intent:
            raise ValueError("媒体服务器或路径映射已变更，请核对原刷新目标")
        return target.profile.create_client(path_mappings=target.path_mappings)

    @staticmethod
    def _resolve_library_binding(client, group: dict[str, Any]) -> tuple[str, ...]:
        """消费时校验历史绑定；失配或读取失败即结束本次刷新，绝不全库兜底。"""
        allowed_ids = tuple(group.get("allowed_library_ids") or ())
        binding = group.get("library_binding") or {}
        if not binding:
            return allowed_ids
        library_id = str(binding.get("id") or "").strip()
        library_name = str(binding.get("name") or "").strip()
        if not library_id and not library_name:
            raise ValueError("媒体库绑定为空，请重新绑定")
        folders = client.list_virtual_folders()
        if library_id:
            matches = [
                item for item in folders
                if str(item.get("id") or "").strip() == library_id
            ]
            if len(matches) != 1:
                raise ValueError("媒体库绑定已失效，请核对原媒体库")
            actual_name = str(matches[0].get("name") or "").strip()
            if library_name and actual_name.casefold() != library_name.casefold():
                raise ValueError("媒体库名称与绑定 ID 不一致，请核对原媒体库")
        else:
            matches = [
                item for item in folders
                if str(item.get("name") or "").strip().casefold() == library_name.casefold()
            ]
            if len(matches) != 1:
                reason = "不存在" if not matches else "存在同名媒体库"
                raise ValueError(f"{reason}，请核对原媒体库: {library_name}")
        resolved_id = str(matches[0].get("id") or "").strip()
        if not resolved_id or (allowed_ids and resolved_id not in allowed_ids):
            raise ValueError("媒体库绑定与允许范围不一致，请核对原媒体库")
        return (resolved_id,)

    def _process_group(self, group: dict[str, Any]) -> None:
        provider = str(group.get("provider") or "").strip().lower()
        paths = list(group.get("paths") or [])
        label = {"jellyfin": "Jellyfin", "emby": "Emby"}.get(provider, provider)
        # 本地存储故障不算一次上游刷新尝试，保留租约供消费者恢复。
        intent = group.get("intent")
        recent_ids = recent_media_refresh_target_ids(provider, upstream=intent["upstream"]) if intent else ()
        client = None
        outcome: dict[str, Any]
        try:
            if not intent:
                raise ValueError("历史刷新未记录原媒体服务器，请手动刷新")
            client = self._client_for(provider, intent)
            if client is None:
                raise ValueError("媒体服务器已停用或配置不完整")
            allowed_ids = self._resolve_library_binding(client, group)
            outcome = client.refresh_for_paths(
                paths, allowed_library_ids=allowed_ids, skip_item_ids=recent_ids,
            )
        except Exception as exc:
            outcome = {"ok": False, "fallback": redact_sensitive_text(str(exc)) or type(exc).__name__}
        finally:
            close_media_server_client(client)

        deferred = outcome.get("deferred_targets") or {}
        outcome["fallback"] = redact_sensitive_text(str(outcome.get("fallback") or ""))[:500]
        # 数据库结算失败必须交回消费者恢复；不能当成媒体服务器失败再清空队列。
        if not settle_media_refresh(
            str(group.get("group_key") or ""), owner=self._owner,
            lease_generation=int(group.get("lease_generation") or 0),
            deferred_targets=deferred,
            refreshed_target_ids=outcome.get("succeeded_target_ids") or (),
            recent_ttl_seconds=_recent_ttl_seconds(),
            outcome=outcome,
        ):
            return
        failed = bool(outcome.get("retryable") or outcome.get("unmatched_paths") or (
            not outcome.get("ok") and not deferred
        ))
        with self._state_lock:
            self._failed_session += int(failed)
            self._completed_session += int(not failed and not deferred)
            self._last_error_type = "MediaRefreshFailed" if failed else ""
        if failed:
            logger.warning(
                "%s 媒体库刷新未完成，失败路径已结束自动处理；已完成的整理与 STRM 不受影响，"
                "请在 %s 中手动刷新媒体库。原因=%s 路径数=%s 样例=%s",
                label, label, redact_sensitive_text(outcome.get("fallback") or "路径未匹配媒体库或刷新失败")[:300],
                len(paths), "；".join(paths[:3]),
            )
        else:
            logger.info(
                "%s 合并刷新完成 scope=%s requested=%s items=%s folders=%s libraries=%s deferred=%s",
                label, outcome.get("scope") or "unknown", len(paths),
                len(outcome.get("items") or []), len(outcome.get("folders") or []),
                len(outcome.get("libraries") or []), len(deferred),
            )
        if outcome.get("succeeded_target_ids"):
            from app.services import clear_dashboard_cache

            clear_dashboard_cache(keep_stale=True)
        self._reconcile_results()


_coordinator = MediaRefreshCoordinator()


def get_media_refresh_coordinator() -> MediaRefreshCoordinator:
    return _coordinator
