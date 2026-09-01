"""本地媒体来源的受控手动扫描动作。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import secrets
from typing import Any

from app import database as db
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.agent.result_projection import sanitize_public_text
from app.modules.local_media_scheduler import get_local_media_scheduler

_OWNER = "admin"
_MAX_SOURCES = 20
_MAX_SOURCE_NUMBER = 10_000


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def local_media_scan_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or not set(arguments).issubset({"source_numbers", "query"}):
        raise AgentToolError("local_media.scan_sources 只接受 source_numbers 和 query 参数")
    raw = arguments.get("source_numbers", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > _MAX_SOURCES:
        raise AgentToolError(f"source_numbers 必须是最多 {_MAX_SOURCES} 项的数组")
    source_numbers: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            raise AgentToolError("source_numbers 只能包含正整数")
        if not 1 <= value <= _MAX_SOURCE_NUMBER:
            raise AgentToolError(
                f"source_numbers 必须在 1 到 {_MAX_SOURCE_NUMBER} 之间"
            )
        if value not in source_numbers:
            source_numbers.append(value)
    query = sanitize_public_text(arguments.get("query"), limit=120)
    return {"source_numbers": source_numbers, "query": query}


def _selected_sources(source_numbers: list[int]) -> tuple[list[Any], list[int]]:
    rows = db.list_local_media_sources(owner=_OWNER)
    if not rows:
        raise AgentToolError("尚未配置本地媒体来源", code="precondition_failed")
    if not source_numbers:
        return rows, list(range(1, len(rows) + 1))
    selected: list[Any] = []
    for number in source_numbers:
        index = number - 1
        if index < 0 or index >= len(rows):
            raise AgentToolError(
                f"本地媒体来源 {number} 不存在", code="precondition_failed"
            )
        selected.append(rows[index])
    return selected, source_numbers


def _snapshot(source_numbers: list[int]) -> tuple[dict[str, Any], list[int], set[int]]:
    selected, public_numbers = _selected_sources(source_numbers)
    entries: list[dict[str, Any]] = []
    eligible_ids: set[int] = set()
    for number, source in zip(public_numbers, selected, strict=True):
        targets = db.list_local_library_targets(source.id, owner=_OWNER)
        eligible = source.mode != "preview_only" and bool(targets)
        if eligible:
            eligible_ids.add(int(source.id))
        entries.append({
            "source_number": number,
            "source_id": int(source.id),
            "name": str(source.name),
            "local_root": str(source.local_root),
            "mode": str(source.mode),
            "media_type": str(source.media_type),
            "updated_at": str(source.updated_at),
            "targets": [
                {
                    "id": int(target.id),
                    "category": str(target.category),
                    "path": str(target.path),
                    "provider": str(target.provider),
                    "library_id": str(target.library_id),
                    "server_path": str(target.server_path),
                    "updated_at": str(target.updated_at),
                }
                for target in targets
            ],
            "eligible": eligible,
        })
    return {"sources": entries}, public_numbers, eligible_ids


def _fingerprint(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def prepare_scan_local_media_sources(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    normalized = local_media_scan_arguments(arguments)
    snapshot, public_numbers, eligible_ids = _snapshot(normalized["source_numbers"])
    snapshot["query"] = normalized["query"]
    if not eligible_ids:
        raise AgentToolError(
            "所选本地媒体来源均处于仅预览模式或尚未配置归档目标",
            code="precondition_failed",
        )
    skipped = len(public_numbers) - len(eligible_ids)
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将扫描 {len(eligible_ids)} 个本地媒体来源",
        data={
            "source_numbers": public_numbers,
            "selected": len(public_numbers),
            "eligible": len(eligible_ids),
            "skipped": skipped,
            "query": normalized["query"],
            "effects": [
                "只扫描已经配置的本地媒体来源，不接受任意宿主机路径。",
                "提供媒体名称时，只会把名称匹配的候选加入队列。",
                "发现的媒体会进入本地整理队列；后续移动、覆盖与清理继续服从现有来源规则。",
                "仅预览来源和未配置归档目标的来源会被安全跳过。",
            ],
        },
        evidence=[Evidence(
            "local_media_configuration",
            "已冻结所选来源及其归档目标配置；路径和媒体库内部标识不会展示给模型或用户。",
            _now(),
        )],
    ), _fingerprint(snapshot)


def scan_local_media_sources_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    normalized = local_media_scan_arguments(arguments)
    snapshot, public_numbers, eligible_ids = _snapshot(normalized["source_numbers"])
    snapshot["query"] = normalized["query"]
    if not secrets.compare_digest(
        _fingerprint(snapshot), str(expected_context or "")
    ):
        raise AgentToolError(
            "本地媒体来源或归档目标已变化，请重新预检",
            code="confirmation_stale",
        )
    scheduler = get_local_media_scheduler()
    was_running = bool(scheduler.status().get("running"))
    result = scheduler.enqueue_manual_scan_candidates(
        silent=True,
        source_ids=eligible_ids,
        candidate_query=normalized["query"],
    )
    queued = int(result.get("queued_count") or 0)
    started = False
    if queued and not was_running:
        scheduler.start()
        started = True
    return ToolResult(
        ok=True,
        status="accepted" if queued else "completed",
        summary=(
            f"已扫描 {int(result.get('scanned_sources') or 0)} 个来源，"
            f"{queued} 个媒体候选已进入整理队列"
        ),
        data={
            "operation": "scan_sources",
            "source_numbers": public_numbers,
            "scanned_sources": int(result.get("scanned_sources") or 0),
            "candidates": int(result.get("candidate_count") or 0),
            "queued_tasks": queued,
            "runtime_started": started,
        },
        evidence=[Evidence(
            "local_media_scheduler",
            "已按确认快照扫描配置来源并唤醒本地媒体调度器；未返回路径、文件名或任务内部标识。",
            _now(),
        )],
        suggestions=(
            ["可继续查看本地媒体任务或待确认队列。"]
            if queued else ["当前所选来源没有发现可入队的媒体候选。"]
        ),
    )
