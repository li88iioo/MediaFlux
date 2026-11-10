"""会话绑定的最近资源提交记录与安全下载状态投影。"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from app.repositories import download_requests as download_requests_repo
from app.repositories import agent_download_verification as agent_download_verification_repo
from app.agent.models import Evidence, ToolResult
from app.agent.session_context import AgentSessionContextRepository
from app.modules.media_identity import normalize_episode_verification

_ALLOWED_TARGETS = {"qb", "guangya", "both"}
_ALLOWED_DISPATCH_STATUSES = {"submitted", "partial", "failed", "duplicate", "manual_review"}
_ALLOWED_RESULT_STATUSES = {"accepted", "partial", "conflict", "unavailable", "outcome_unknown"}
_BACKEND_NAMES = {"qb", "guangya"}
_TERMINAL_BACKEND_STATUSES = {"completed", "failed", "manual_review", "cancelled"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecentDownloadVerification:
    """最近缺集提交的最小安全核验上下文。"""

    title: str
    tmdb_id: str
    season: int
    episode: int
    as_of: str
    library_name: str = ""


@dataclass(frozen=True)
class RecentDownloadSubmission:
    """仅供服务端会话续接使用；不得直接序列化到 Agent 响应。"""

    request_id: int | None
    target: str
    dispatch_status: str
    succeeded: tuple[str, ...]
    failed: tuple[str, ...]
    created: bool
    duplicate: bool
    result_status: str
    captured_at: str
    verification: RecentDownloadVerification | None = None


@dataclass(frozen=True)
class _StoredSubmission:
    record: RecentDownloadSubmission
    expires_at: float


class RecentDownloadSubmissionStore:
    """保存最近确认执行的资源提交，按 owner 隔离并自动过期。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 1800,
        max_owners: int = 256,
        max_items_per_owner: int = 8,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        repository: AgentSessionContextRepository | None = None,
    ) -> None:
        self.ttl_seconds = max(1, ttl_seconds)
        self.max_owners = max(1, max_owners)
        self.max_items_per_owner = max(1, max_items_per_owner)
        self._clock = clock
        self._wall_clock = wall_clock
        self._repository = repository
        self._lock = threading.RLock()
        self._owner_locks = tuple(threading.RLock() for _ in range(64))
        self._entries: OrderedDict[str, tuple[_StoredSubmission, ...]] = OrderedDict()

    def capture(
        self,
        *,
        owner: str,
        result: ToolResult,
        verification_context: dict[str, Any] | None = None,
    ) -> bool:
        owner_key = (owner or "").strip()
        captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
        record = _safe_submission(
            result,
            captured_at=captured_at,
            verification_context=verification_context,
        )
        if not owner_key or record is None:
            return False
        with self._owner_lock(owner_key):
            now = self._clock()
            entry = _StoredSubmission(record=record, expires_at=now + self.ttl_seconds)
            with self._lock:
                self._prune_locked(now)
                needs_restore = self._repository is not None and owner_key not in self._entries
            if needs_restore:
                self._restore(owner_key=owner_key, now=now)
            with self._lock:
                current = self._entries.pop(owner_key, ())
                records = (entry, *current)[: self.max_items_per_owner]
                self._entries[owner_key] = records
                while len(self._entries) > self.max_owners:
                    self._entries.popitem(last=False)
            if self._repository is not None:
                try:
                    self._repository.append_download(
                        owner=owner_key,
                        payload=_submission_payload(record),
                        expires_at=self._wall_clock() + self.ttl_seconds,
                        max_items=self.max_items_per_owner,
                    )
                except Exception as exc:
                    logger.warning("Agent 下载上下文持久化失败 type=%s", type(exc).__name__)
        return True

    def get(self, *, owner: str) -> tuple[RecentDownloadSubmission, ...]:
        owner_key = (owner or "").strip()
        if not owner_key:
            return ()
        with self._owner_lock(owner_key):
            now = self._clock()
            with self._lock:
                self._prune_locked(now)
                entries = self._entries.get(owner_key)
                if entries is not None:
                    self._entries.move_to_end(owner_key)
                    return tuple(item.record for item in entries)
            return self._restore(owner_key=owner_key, now=now)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def clear_owner(self, *, owner: str) -> bool:
        owner_key = (owner or "").strip()
        if not owner_key:
            return False
        removed = False
        with self._owner_lock(owner_key):
            with self._lock:
                removed = self._entries.pop(owner_key, None) is not None
            if self._repository is not None:
                try:
                    removed = bool(self._repository.delete_downloads(
                        owner=owner_key
                    )) or removed
                except Exception as exc:
                    logger.warning(
                        "Agent 下载上下文清理失败 type=%s",
                        type(exc).__name__,
                    )
        return removed

    def _owner_lock(self, owner_key: str) -> threading.RLock:
        return self._owner_locks[hash(owner_key) % len(self._owner_locks)]

    def _prune_locked(self, now: float) -> None:
        for owner, entries in tuple(self._entries.items()):
            active = tuple(item for item in entries if item.expires_at > now)
            if active:
                self._entries[owner] = active
            else:
                self._entries.pop(owner, None)

    def _restore(self, *, owner_key: str, now: float) -> tuple[RecentDownloadSubmission, ...]:
        if self._repository is None:
            return ()
        wall_now = self._wall_clock()
        try:
            persisted = self._repository.list_downloads(
                owner=owner_key,
                now=wall_now,
                limit=self.max_items_per_owner,
            )
        except Exception as exc:
            logger.warning("Agent 下载上下文恢复失败 type=%s", type(exc).__name__)
            return ()
        entries: list[_StoredSubmission] = []
        for item in persisted:
            record = _submission_from_payload(item.payload)
            remaining = item.expires_at - wall_now
            if record is None or remaining <= 0:
                continue
            entries.append(
                _StoredSubmission(
                    record=record,
                    expires_at=now + min(float(self.ttl_seconds), remaining),
                )
            )
        if not entries:
            return ()
        restored = tuple(entries[: self.max_items_per_owner])
        with self._lock:
            self._prune_locked(now)
            self._entries[owner_key] = restored
            self._entries.move_to_end(owner_key)
            while len(self._entries) > self.max_owners:
                self._entries.popitem(last=False)
        return tuple(item.record for item in restored)


def _safe_submission(
    result: ToolResult,
    *,
    captured_at: str,
    verification_context: dict[str, Any] | None = None,
) -> RecentDownloadSubmission | None:
    if (result.status or "") not in _ALLOWED_RESULT_STATUSES:
        return None
    data = result.data if isinstance(result.data, dict) else {}
    target = str(data.get("target") or "").strip().lower()
    dispatch_status = str(data.get("status") or "").strip().lower()
    if target not in _ALLOWED_TARGETS or dispatch_status not in _ALLOWED_DISPATCH_STATUSES:
        return None
    duplicate = bool(data.get("duplicate"))
    request_id = _positive_int(data.get("request_id") or data.get("request_number"))
    if duplicate or dispatch_status == "duplicate" or result.status == "conflict":
        request_id = None
    return RecentDownloadSubmission(
        request_id=request_id,
        target=target,
        dispatch_status=dispatch_status,
        succeeded=_safe_backend_tuple(data.get("succeeded")),
        failed=_safe_backend_tuple(data.get("failed")),
        created=bool(data.get("created")),
        duplicate=duplicate,
        result_status=(result.status),
        captured_at=_safe_timestamp(captured_at),
        verification=_safe_verification(verification_context),
    )


def _submission_payload(record: RecentDownloadSubmission) -> dict[str, Any]:
    return {
        "request_id": record.request_id,
        "target": record.target,
        "dispatch_status": record.dispatch_status,
        "succeeded": list(record.succeeded),
        "failed": list(record.failed),
        "created": record.created,
        "duplicate": record.duplicate,
        "result_status": record.result_status,
        "captured_at": record.captured_at,
        "verification": _verification_payload(record.verification),
    }


def _submission_from_payload(value: Any) -> RecentDownloadSubmission | None:
    required_keys = {
        "request_id", "target", "dispatch_status", "succeeded", "failed", "created",
        "duplicate", "result_status", "captured_at", "verification",
    }
    if not isinstance(value, dict) or set(value) != required_keys:
        return None
    target = str(value.get("target") or "").strip().lower()
    dispatch_status = str(value.get("dispatch_status") or "").strip().lower()
    result_status = str(value.get("result_status") or "").strip().lower()
    created = value.get("created")
    duplicate = value.get("duplicate")
    request_id_value = value.get("request_id")
    request_id = None if request_id_value is None else _positive_int(request_id_value)
    captured_at = _safe_timestamp(value.get("captured_at"))
    succeeded = _safe_backend_tuple(value.get("succeeded"))
    failed = _safe_backend_tuple(value.get("failed"))
    verification_value = value.get("verification")
    verification = _safe_verification(verification_value)
    if (
        target not in _ALLOWED_TARGETS
        or dispatch_status not in _ALLOWED_DISPATCH_STATUSES
        or result_status not in _ALLOWED_RESULT_STATUSES
        or not isinstance(created, bool)
        or not isinstance(duplicate, bool)
        or (request_id_value is not None and request_id is None)
        or not captured_at
        or len(succeeded) != len(value.get("succeeded") or ())
        or len(failed) != len(value.get("failed") or ())
        or (verification_value is not None and verification is None)
    ):
        return None
    if duplicate or dispatch_status == "duplicate" or result_status == "conflict":
        if request_id is not None:
            return None
    return RecentDownloadSubmission(
        request_id=request_id,
        target=target,
        dispatch_status=dispatch_status,
        succeeded=succeeded,
        failed=failed,
        created=created,
        duplicate=duplicate,
        result_status=result_status,
        captured_at=captured_at,
        verification=verification,
    )


def _safe_verification(value: Any) -> RecentDownloadVerification | None:
    # 工作流票据保留随机引用；最近记录只消费公共最小目标投影。
    source = value.get("verification", value) if isinstance(value, dict) else value
    normalized = normalize_episode_verification(source)
    return RecentDownloadVerification(**normalized) if normalized is not None else None


def parse_recent_download_verification_context(
    value: Any,
) -> RecentDownloadVerification | None:
    """公开复用严格核验上下文解析器；失败时安全返回 ``None``。"""
    return _safe_verification(value)


def _verification_payload(value: RecentDownloadVerification | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "title": value.title,
        "tmdb_id": value.tmdb_id,
        "season": value.season,
        "episode": value.episode,
        "as_of": value.as_of,
        "library_name": value.library_name,
    }


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if 0 < parsed <= 2_147_483_647 else None


def _safe_backend_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        normalized = str(item or "").strip().lower()
        if normalized in _BACKEND_NAMES and normalized not in result:
            result.append(normalized)
    return tuple(result)


def sanitize_submission_confirmation_result(result: ToolResult) -> ToolResult:
    """把资源提交确认结果收敛为固定白名单，拒绝透传后端原始字段。"""
    data = result.data if isinstance(result.data, dict) else {}
    target = str(data.get("target") or "").strip().lower()
    dispatch_status = str(data.get("status") or "").strip().lower()
    duplicate = bool(data.get("duplicate"))
    result_status = (result.status or "").strip().lower()

    public_data: dict[str, Any] = {
        "target": target if target in _ALLOWED_TARGETS else "unknown",
        "status": (
            dispatch_status
            if dispatch_status in _ALLOWED_DISPATCH_STATUSES
            else "failed"
        ),
        "created": bool(data.get("created")),
        "duplicate": duplicate,
        "succeeded": list(_safe_backend_tuple(data.get("succeeded"))),
        "failed": list(_safe_backend_tuple(data.get("failed"))),
    }
    if duplicate or dispatch_status == "duplicate" or result_status == "conflict":
        return ToolResult(
            False,
            "conflict",
            "该资源已经提交或正在处理中",
            data=public_data,
            suggestions=["可前往下载任务页核对现有任务。"],
            error="请勿重复提交。",
        )
    if result_status != "accepted" or dispatch_status == "failed":
        return ToolResult(
            False,
            "unavailable",
            "下载任务提交失败",
            data=public_data,
            suggestions=["请检查下载后端配置后重新搜索并提交。"],
            error="所选下载后端未接受任务。",
        )
    summary = "下载任务已部分提交" if dispatch_status == "partial" else "下载任务已提交"
    return ToolResult(
        True,
        "accepted",
        summary,
        data=public_data,
        evidence=[_evidence("服务器已受理资源提交；响应仅保留安全状态摘要。")],
        suggestions=["可询问：刚才下载到哪了。"],
    )


def build_recent_download_status(
    record: RecentDownloadSubmission,
    *,
    position: int,
) -> ToolResult:
    """读取本地持久化快照并生成固定白名单状态，不访问下载后端。"""
    if record.request_id is None:
        return _immediate_result(record, position=position)

    row, logs = download_requests_repo.get_download_request_status_snapshot(record.request_id)
    if row is None:
        return ToolResult(
            False,
            "unavailable",
            "最近提交任务的状态记录已不可用",
            data={
                "position": position,
                "target": record.target,
                "phase": "unknown",
                "tracking_freshness": "unavailable",
                "captured_at": record.captured_at,
            },
            evidence=[_evidence("未找到对应的本地持久化下载状态记录。")],
            suggestions=["可前往下载任务页按名称核对，或重新搜索资源后提交。"],
            error="任务记录可能已清理，或本机状态已变化。",
        )

    latest_logs: dict[str, Any] = {}
    for log in logs:
        source = str(log["source"] or "").strip().lower()
        if source in _BACKEND_NAMES and source not in latest_logs:
            latest_logs[source] = log

    backend_names = ("qb", "guangya") if record.target == "both" else (record.target,)
    backends = [
        _backend_projection(name, row=row, log=latest_logs.get(name))
        for name in backend_names
    ]
    phase, needs_attention = _root_phase(row, backends, target=record.target)
    terminal = phase in {"completed", "failed", "partial_failed", "manual_review"}
    data: dict[str, Any] = {
        "position": position,
        "target": record.target,
        "phase": phase,
        "terminal": terminal,
        "needs_attention": needs_attention,
        "tracking_freshness": "persisted_snapshot",
        "captured_at": record.captured_at,
        "dispatch": {
            "status": record.dispatch_status,
            "created": record.created,
            "duplicate": record.duplicate,
            "succeeded": list(record.succeeded),
            "failed": list(record.failed),
        },
        "backends": backends,
    }
    if record.verification is not None:
        data.update({
            "title": record.verification.title,
            "tmdb_id": record.verification.tmdb_id,
            "media_type": "tv",
            "season": record.verification.season,
            "episode": record.verification.episode,
        })

    for key in ("updated_at", "completed_at"):
        value = _safe_timestamp(row[key])
        if value:
            data[key] = value

    local_processing = _local_processing_projection(row)
    if local_processing:
        data["local_processing"] = local_processing

    automatic_verification = _automatic_verification_projection(record.request_id)
    if automatic_verification:
        data["library_verification"] = automatic_verification

    status = "attention" if needs_attention else ("completed" if terminal else "in_progress")
    return ToolResult(
        True,
        status,
        _phase_summary(phase),
        data=data,
        evidence=[_evidence("读取本地持久化下载请求与 tracker 快照；未访问下载后端或触发写操作。")],
        suggestions=_phase_suggestions(phase),
    )


def _automatic_verification_projection(request_id: int) -> dict[str, Any]:
    row = agent_download_verification_repo.get_agent_download_verification(request_id)
    if row is None:
        return {}
    status = str(row["status"] or "")
    result = str(row["result"] or "")
    if status not in {"pending", "running", "retry_wait", "visible", "attention"}:
        return {}
    data: dict[str, Any] = {
        "status": status,
        "attempts": _bounded_nonnegative_int(row["attempts"], maximum=100) or 0,
        "targets": [{
            "tmdb_id": str(target["tmdb_id"])[:10],
            "season": _bounded_nonnegative_int(target["season"], maximum=100) or 0,
            "episode": _bounded_nonnegative_int(target["episode"], maximum=1000) or 0,
            "as_of": str(target["as_of"] or "")[:10],
            "library_name": str(target["library_name"] or "")[:80],
            "result": target["result"] if target["result"] in {"", "visible", "missing", "inconclusive"} else "inconclusive",
        } for target in row["targets"]],
    }
    if result in {"visible", "missing", "inconclusive"}:
        data["result"] = result
    for key in ("next_check_at", "last_checked_at"):
        value = _safe_timestamp(row[key])
        if value:
            data[key] = value
    return data


def explain_recent_download_status(
    record: RecentDownloadSubmission,
    *,
    position: int,
) -> ToolResult:
    """基于安全状态投影解释最近任务；不读取原始错误或访问后端。"""
    status_result = build_recent_download_status(record, position=position)
    data: dict[str, Any] = dict(status_result.data) if isinstance(status_result.data, dict) else {
        "position": position,
        "target": record.target,
        "phase": "unknown",
        "tracking_freshness": "unavailable",
    }
    explanation = _explanation_projection(data)
    data["explanation"] = explanation
    phase = str(data.get("phase") or "unknown")
    if phase in {"failed", "partial_in_progress", "partial_failed", "manual_review", "unknown"}:
        result_status = "attention"
    elif phase == "completed":
        result_status = "completed"
    elif phase == "already_submitted":
        result_status = "conflict"
    else:
        result_status = "in_progress"
    return ToolResult(
        status_result.ok,
        "unavailable" if not status_result.ok else result_status,
        explanation["headline"],
        data=data,
        evidence=[_evidence("基于本地结构化状态快照生成；未读取错误原文或访问下载后端。")],
        suggestions=list(explanation["next_steps"]),
        error=(
            "当前安全状态不足以确认具体原因。"
            if not status_result.ok or explanation["certainty"] == "limited"
            else None
        ),
    )


def _explanation_projection(data: dict[str, Any]) -> dict[str, Any]:
    phase = str(data.get("phase") or "unknown")
    tracking = str(data.get("tracking_freshness") or "")
    definitions: dict[str, tuple[str, str, tuple[str, ...], tuple[str, ...]]] = {
        "failed": (
            "all_targets_failed",
            "最近任务未能在所选下载目标完成",
            ("安全状态显示所选目标已结束，但没有目标完成。",),
            ("检查对应下载后端是否在线且配置有效。", "重新搜索资源并人工确认后再提交。"),
        ),
        "partial_in_progress": (
            "partial_failure_in_progress",
            "部分下载目标失败，其余目标仍在处理",
            ("当前不是整体失败；仍有目标处于等待、提交或下载阶段。",),
            ("先等待仍在处理的目标结束。", "如需排查失败目标，请检查对应后端配置。"),
        ),
        "partial_failed": (
            "partial_failure",
            "部分下载目标完成，部分目标失败",
            ("至少一个目标已经完成，另一个目标没有完成。",),
            ("保留已完成结果。", "仅检查失败目标的连接与配置后再决定是否重试。"),
        ),
        "manual_review": (
            "manual_review_required",
            "任务已进入需要人工核验的阶段",
            ("安全状态指向本地导入或云盘整理环节，需要人工确认。",),
            ("检查本地媒体待确认项或云盘整理记录。", "确认目录映射和写入目标后再人工处理。"),
        ),
        "post_processing": (
            "post_processing",
            "当前未检测到下载失败，任务仍在后处理",
            ("下载目标已经完成，系统正在执行本地导入或云盘整理。",),
            ("等待后处理完成后再次查询状态。",),
        ),
        "completed": (
            "completed",
            "最近任务已完成，当前未检测到异常",
            ("所选下载目标与已记录的后处理状态均未显示失败。",),
            (),
        ),
        "already_submitted": (
            "already_submitted",
            "本次没有创建新任务，因为资源已存在或正在处理",
            ("当前记录不能关联到一个新的下载任务。",),
            ("前往下载任务页检查已有队列。",),
        ),
        "accepted": (
            "accepted_without_snapshot",
            "提交已受理，但暂时没有可关联的状态快照",
            ("现有安全记录不足以判断任务卡在哪个阶段。",),
            ("稍后再次查询状态。", "必要时前往下载任务页核对。"),
        ),
        "pending": (
            "still_in_progress",
            "任务仍在等待下载目标处理",
            ("当前未检测到失败状态。",),
            ("稍后再次查询状态。",),
        ),
        "submitting": (
            "still_in_progress",
            "任务正在提交到下载目标",
            ("当前未检测到失败状态。",),
            ("稍后再次查询状态。",),
        ),
        "submitted": (
            "still_in_progress",
            "任务已提交，正在等待下载目标确认",
            ("当前未检测到失败状态。",),
            ("稍后再次查询状态。",),
        ),
        "downloading": (
            "still_in_progress",
            "任务正在下载，并非失败状态",
            ("当前安全快照仍显示下载进行中。",),
            ("等待下载完成后再次查询状态。",),
        ),
    }
    if phase == "failed" and tracking == "confirmed_result":
        classification = "submission_rejected"
        headline = "资源提交未被下载后端接受"
        details = ("现有安全记录无法判断是连接、认证还是资源格式问题。",)
        next_steps = ("检查对应下载后端配置和可用性。", "重新搜索资源并人工确认后再提交。")
        certainty = "limited"
    elif phase == "unknown" and tracking == "unavailable":
        classification = "tracking_unavailable"
        headline = "最近任务的本地追踪记录已不可用"
        details = ("这不等同于下载失败；现有安全记录无法确认最终状态。",)
        next_steps = ("前往下载任务页按名称核对。", "必要时重新搜索资源并提交。")
        certainty = "limited"
    elif phase == "unknown":
        classification = "state_indeterminate"
        headline = "最近任务状态暂时无法判断"
        details = ("安全快照中的状态不足以确定失败阶段。",)
        next_steps = ("前往下载任务页核对状态。", "检查对应后端是否在线。")
        certainty = "limited"
    else:
        classification, headline, details, next_steps = definitions.get(
            phase,
            (
                "state_indeterminate",
                "最近任务状态暂时无法判断",
                ("安全快照中的状态不足以确定失败阶段。",),
                ("前往下载任务页核对状态。",),
            ),
        )
        certainty = "limited" if phase in {"already_submitted", "accepted"} else "confirmed"

    backend_details = _backend_explanation_details(data.get("backends"))
    return {
        "classification": classification,
        "certainty": certainty,
        "headline": headline,
        "details": [*details, *backend_details],
        "next_steps": list(next_steps),
        "automatic_retry": False,
    }


def _backend_explanation_details(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    backend_labels = {"qb": "qBittorrent", "guangya": "光鸭"}
    status_labels = {
        "pending": "等待处理",
        "submitting": "正在提交",
        "submitted": "已提交",
        "downloading": "下载中",
        "completed": "已完成",
        "failed": "失败",
        "manual_review": "需要人工核验",
        "cancelled": "已取消",
        "unknown": "状态未知",
    }
    details: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        status = str(item.get("status") or "")
        label = backend_labels.get(name)
        state = status_labels.get(status)
        if label and state:
            details.append(f"{label}：{state}。")
    return details


def build_recent_download_library_verification(
    record: RecentDownloadSubmission,
    audit: ToolResult,
    *,
    position: int,
) -> ToolResult:
    """把剧集审计收敛为指定季集的安全核验结论。"""
    verification = record.verification
    if verification is None:
        return ToolResult(
            False,
            "precondition_failed",
            "最近任务没有可用于入库核验的缺集上下文",
            suggestions=["请按明确片名、季和集重新执行媒体库缺集检查。"],
            error="该任务不是从已核验缺集的资源推荐链路提交，或来自旧版会话记录。",
        )
    audit_data = audit.data if isinstance(audit.data, dict) else {}
    data: dict[str, Any] = {
        "position": position,
        "title": verification.title,
        "tmdb_id": verification.tmdb_id,
        "season": verification.season,
        "episode": verification.episode,
        "as_of": verification.as_of,
        "audit_status": (audit.status or "")[:40],
    }
    if verification.library_name:
        data["library_name"] = verification.library_name
    missing_count = _bounded_nonnegative_int(audit_data.get("missing_count"), maximum=100_000)
    if missing_count is not None:
        data["missing_count"] = missing_count
    outcome = "inconclusive"
    if audit.ok and audit.status in {"up_to_date", "updates_available"}:
        facts = next((item for item in audit_data.get("episodes", []) if isinstance(item, dict)
                      and item.get("season") == verification.season and item.get("episode") == verification.episode), {})
        aired, local = facts.get("aired"), facts.get("local")
        exact_target = (
            any(key in audit_data for key in ("target_aired", "target_local", "target_missing"))
            and audit_data.get("season", verification.season) == verification.season
            and audit_data.get("target_episode", verification.episode) == verification.episode
        )
        if exact_target:
            aired, local = audit_data.get("target_aired"), audit_data.get("target_local")
        sample_missing = any(
            isinstance(item, dict) and item.get("season") == verification.season
            and item.get("episode") == verification.episode
            for item in audit_data.get("missing_sample", [])
        )
        if local is True:
            outcome = "visible"
        elif ((exact_target and audit_data.get("target_missing") is True)
                or (aired is True and local is False) or sample_missing):
            outcome = "missing"
    data["verification"] = outcome
    if outcome == "missing":
        return ToolResult(
            True, "updates_available",
            f"{verification.title} S{verification.season:02d}E{verification.episode:02d} 仍未在媒体库中可见",
            data=data, evidence=list(audit.evidence),
            suggestions=["下载完成不等于媒体服务器已扫描入库；可稍后再次核验或检查入库链路。"],
        )
    if outcome == "visible":
        has_other_missing = audit.status == "updates_available"
        return ToolResult(
            True, "up_to_date",
            (f"目标集 S{verification.season:02d}E{verification.episode:02d} 已在媒体库中可见，但同季仍有其他缺集"
             if has_other_missing else f"{verification.title} S{verification.season:02d}E{verification.episode:02d} 已在媒体库中可见"),
            data=data, evidence=list(audit.evidence),
            suggestions=(["可继续查看同季其他缺集。"] if has_other_missing else ["如媒体客户端尚未显示，可刷新客户端缓存后再查看。"]),
        )
    return ToolResult(
        False, "inconclusive", "媒体库审计暂时无法可靠判断目标集是否已入库",
        data=data, evidence=list(audit.evidence),
        suggestions=list(audit.suggestions) or ["请稍后重试，或按明确片名与季集重新检查。"],
        error=audit.error or "审计结果不完整或缺少目标集的本地存在证据。",
    )


def _bounded_nonnegative_int(value: Any, *, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if 0 <= parsed <= maximum else None


def _immediate_result(record: RecentDownloadSubmission, *, position: int) -> ToolResult:
    data = {
        "position": position,
        "target": record.target,
        "phase": "unknown",
        "tracking_freshness": "confirmed_result",
        "captured_at": record.captured_at,
        "dispatch": {
            "status": record.dispatch_status,
            "created": record.created,
            "duplicate": record.duplicate,
            "succeeded": list(record.succeeded),
            "failed": list(record.failed),
        },
        "backends": [],
    }
    if record.duplicate or record.dispatch_status == "duplicate" or record.result_status == "conflict":
        data["phase"] = "already_submitted"
        return ToolResult(
            True,
            "conflict",
            "上次提交未创建新任务，因为该资源已存在或正在处理",
            data=data,
            evidence=[_evidence("确认结果未提供可关联的本地任务状态记录。")],
            suggestions=["可前往下载任务页核对现有任务。"],
        )
    if record.dispatch_status == "failed" or record.result_status == "unavailable":
        data["phase"] = "failed"
        return ToolResult(
            True,
            "attention",
            "上次资源提交未被下载后端接受",
            data=data,
            evidence=[_evidence("读取最近一次确认执行的安全结果；未访问下载后端。")],
            suggestions=["请检查下载后端配置后重新搜索并提交。"],
        )
    data["phase"] = "accepted"
    return ToolResult(
        True,
        "in_progress",
        "上次资源提交已受理，但暂时没有可关联的状态快照",
        data=data,
        evidence=[_evidence("读取最近一次确认执行的安全结果；未访问下载后端。")],
        suggestions=["可稍后再问，或前往下载任务页查看。"],
    )


def _backend_projection(name: str, *, row: Any, log: Any | None) -> dict[str, Any]:
    column = "qb_status" if name == "qb" else "gy_status"
    raw = str(row[column] or "").strip().lower()
    status = raw if raw in {
        "pending", "submitting", "submitted", "downloading", "outcome_unknown", "completed", "failed",
        "manual_review", "cancelled",
    } else "unknown"
    progress = _safe_progress(log["progress"] if log is not None else None)
    if status == "completed":
        progress = 100
    observed_at = _safe_timestamp(
        (log["completed_at"] or log["updated_at"]) if log is not None else row["updated_at"]
    )
    result: dict[str, Any] = {
        "name": name,
        "status": status,
        "terminal": status in _TERMINAL_BACKEND_STATUSES,
    }
    if progress is not None:
        result["progress_percent"] = progress
    if observed_at:
        result["observed_at"] = observed_at
    return result


def _safe_progress(value: Any) -> int | None:
    try:
        progress = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(progress):
        return None
    return (round(max(0.0, min(progress, 1.0)) * 100))


def _safe_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 40:
        return ""
    allowed = set("0123456789-:TtZz+. ")
    return text if all(char in allowed for char in text) else ""


def _root_phase(
    row: Any,
    backends: list[dict[str, Any]],
    *,
    target: str,
) -> tuple[str, bool]:
    statuses = [str(item["status"]) for item in backends]
    failure_statuses = {"failed", "manual_review", "cancelled"}
    active_statuses = {"pending", "submitting", "submitted", "downloading"}
    completed = any(status == "completed" for status in statuses)
    failed = any(status in failure_statuses for status in statuses)
    active = any(status in active_statuses for status in statuses)

    if int(row["organize_started"] or 0) < 0:
        return "manual_review", True
    local_status = str(row["local_import_status"] or "").strip().lower()
    if local_status in {"requires_manual", "failed"}:
        return "manual_review", True
    if statuses and all(status in failure_statuses for status in statuses):
        return "failed", True
    if failed and active:
        return "partial_in_progress", True
    if completed and failed:
        return "partial_failed", True
    if active:
        for phase in ("downloading", "submitted", "submitting", "pending"):
            if phase in statuses:
                return phase, False

    root = str(row["status"] or "").strip().lower()
    if "unknown" in statuses:
        if statuses and all(status == "unknown" for status in statuses) and root in active_statuses:
            return root, False
        return "unknown", True

    organize_started = int(row["organize_started"] or 0)
    guangya_pending_organize = (
        target in {"guangya", "both"}
        and any(item["name"] == "guangya" and item["status"] == "completed" for item in backends)
        and organize_started == 0
    )
    if statuses and all(status == "completed" for status in statuses):
        if (
            local_status in {"pending", "planned"}
            or organize_started > 0
            or guangya_pending_organize
        ):
            return "post_processing", False
        return "completed", False

    if root in active_statuses:
        return root, False
    if root == "manual_review":
        return "manual_review", True
    if root in {"failed", "cancelled"}:
        return "failed", True
    return "unknown", True


def _local_processing_projection(row: Any) -> dict[str, Any] | None:
    local_status = str(row["local_import_status"] or "").strip().lower()
    organize_started = int(row["organize_started"] or 0)
    if not local_status and organize_started == 0:
        return None
    if local_status in {"pending", "planned"}:
        local_phase = "in_progress"
    elif local_status == "completed":
        local_phase = "completed"
    elif local_status == "skipped":
        local_phase = "skipped"
    elif local_status in {"requires_manual", "failed"}:
        local_phase = "attention"
    else:
        local_phase = "unknown"
    if organize_started > 0:
        organize_phase = "started"
    elif organize_started < 0:
        organize_phase = "attention"
    else:
        organize_phase = "not_started"
    return {"local_import": local_phase, "guangya_organize": organize_phase}


def _phase_summary(phase: str) -> str:
    return {
        "pending": "最近任务正在等待下载目标",
        "submitting": "最近任务正在提交到下载后端",
        "submitted": "最近任务已提交，正在等待后端确认",
        "downloading": "最近任务正在下载",
        "post_processing": "下载已完成，正在执行本地导入或云盘整理",
        "completed": "最近任务已完成",
        "partial_in_progress": "最近任务部分目标失败，其余目标仍在处理",
        "partial_failed": "最近任务部分目标已完成，部分目标失败",
        "failed": "最近任务执行失败",
        "manual_review": "最近任务需要人工核验",
        "unknown": "最近任务状态暂时无法判断",
    }.get(phase, "最近任务状态已读取")


def _phase_suggestions(phase: str) -> list[str]:
    if phase in {"failed", "partial_in_progress", "partial_failed", "manual_review", "unknown"}:
        return ["可前往下载任务页查看安全摘要，并检查对应后端配置。"]
    if phase in {"submitted", "downloading", "post_processing"}:
        return ["可稍后再次询问：刚才下载到哪了。"]
    return []


def _evidence(description: str) -> Evidence:
    return Evidence(
        "download_tracker_snapshot",
        description,
        datetime.now().astimezone().isoformat(timespec="seconds"),
    )
