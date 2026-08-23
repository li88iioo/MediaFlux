"""STRM 运行历史与失败上下文的安全聚合。"""
from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from app import database as db
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.logger import get_logger

logger = get_logger(__name__)
_ALLOWED_STATUSES = {"all", "running", "success", "partial", "failed", "skipped"}
_SAFE_RUN_STATUSES = _ALLOWED_STATUSES - {"all"}
_SAFE_TRIGGER_TYPES = {"manual", "cron", "organize", "telegram", "config-retirement"}
_SAFE_MODES = {"full", "fast", "incremental", "fast_noop"}
_SAFE_CHANGE_QUEUE_STATES = ("queued", "running", "dirty", "completed", "failed")
_SAFE_METADATA_QUEUE_STATUSES = (
    "queued", "running", "retry_wait", "completed", "failed", "cancelled",
)
_SAFE_STAT_KEYS = (
    "total", "generated", "created", "updated", "skipped", "failed",
    "metadata_generated", "metadata_queued", "metadata_skipped", "metadata_failed",
    "cleaned", "metadata_cleaned", "empty_dirs_cleaned",
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 40:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.isoformat(timespec="seconds")


def strm_run_history_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) - {"limit", "status"}:
        raise AgentToolError("strm.run_history 只接受 limit 和 status")
    limit = arguments.get("limit", 8)
    status = str(arguments.get("status") or "all").strip().casefold()
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise AgentToolError("limit 必须是 1 到 20 的整数")
    if status not in _ALLOWED_STATUSES:
        raise AgentToolError("status 必须是 all、running、success、partial、failed 或 skipped")
    return {"limit": int(limit), "status": status}


def _safe_run(row: Any, ordinal: int) -> dict[str, Any]:
    try:
        payload = json.loads(str(row["result"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    stats_raw = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    stats = {key: _count(stats_raw.get(key)) for key in _SAFE_STAT_KEYS}
    mode = str(payload.get("mode") or "").strip().casefold()
    status = str(row["status"] or "").strip().casefold()
    trigger_type = str(row["trigger_type"] or "").strip().casefold()
    try:
        elapsed = max(0.0, min(float(payload.get("elapsed_seconds") or 0), 604800.0))
    except (TypeError, ValueError, OverflowError):
        elapsed = 0.0
    return {
        "run_number": ordinal,
        "status": status if status in _SAFE_RUN_STATUSES else "unknown",
        "trigger_type": trigger_type if trigger_type in _SAFE_TRIGGER_TYPES else "unknown",
        "started_at": _safe_timestamp(row["started_at"]),
        "finished_at": _safe_timestamp(row["finished_at"]),
        "elapsed_seconds": round(elapsed, 1),
        "mode": mode if mode in _SAFE_MODES else "unknown",
        "stats": stats,
    }


def _queue_summary() -> dict[str, Any]:
    with db.get_conn() as conn:
        change_rows = conn.execute(
            "SELECT state,COUNT(*) AS count FROM strm_change_queue GROUP BY state"
        ).fetchall()
        metadata_rows = conn.execute(
            "SELECT status,COUNT(*) AS count FROM strm_metadata_queue GROUP BY status"
        ).fetchall()
    change_counts = {state: 0 for state in _SAFE_CHANGE_QUEUE_STATES}
    for row in change_rows:
        state = str(row["state"] or "").strip().casefold()
        if state in change_counts:
            change_counts[state] = _count(row["count"])
    metadata_counts = {status: 0 for status in _SAFE_METADATA_QUEUE_STATUSES}
    for row in metadata_rows:
        status = str(row["status"] or "").strip().casefold()
        if status in metadata_counts:
            metadata_counts[status] = _count(row["count"])
    return {"change_queue": change_counts, "metadata_queue": metadata_counts}


def get_strm_run_history(arguments: dict[str, Any]) -> ToolResult:
    try:
        rows = db.list_task_runs("strm_sync", limit=100)
        if arguments["status"] != "all":
            rows = [
                row for row in rows
                if str(row["status"] or "").strip().casefold() == arguments["status"]
            ]
        rows = rows[: arguments["limit"]]
        runs = [_safe_run(row, index) for index, row in enumerate(rows, start=1)]
        failures = db.get_strm_failure_triage_summary()
        queues = _queue_summary()
    except Exception as exc:
        logger.warning("Agent STRM 运行历史不可用 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取 STRM 运行历史",
            error="STRM 安全运行摘要当前不可用。",
            evidence=[Evidence(
                "sqlite:strm_history",
                "只尝试读取本地聚合，不访问网络或媒体文件系统。",
                _now(),
            )],
        )
    active_failures = _count(failures.get("open")) + _count(failures.get("retrying"))
    status = "attention" if active_failures else ("completed" if runs else "empty")
    summary = (
        f"已读取最近 {len(runs)} 次 STRM 运行摘要，当前有 {active_failures} 条活跃失败记录"
        if runs else "当前没有 STRM 运行历史"
    )
    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data={
            "runs": runs,
            "run_count": len(runs),
            "failure_context": {
                "open": _count(failures.get("open")),
                "retrying": _count(failures.get("retrying")),
                "resolved": _count(failures.get("resolved")),
                "active_repeated": _count(failures.get("active_repeated")),
                "active_retried": _count(failures.get("active_retried")),
                "by_action": {
                    action: {
                        key: _count(
                            (
                                failures.get("by_action", {}).get(action, {})
                                if isinstance(failures.get("by_action"), dict)
                                and isinstance(failures.get("by_action", {}).get(action), dict)
                                else {}
                            ).get(key)
                        )
                        for key in ("total", "open", "retrying", "resolved")
                    }
                    for action in ("generate", "metadata")
                },
            },
            "queue_context": queues,
            "limits": {"max_runs": 20},
        },
        evidence=[Evidence(
            "sqlite:strm_history",
            "仅返回运行状态、固定统计和队列计数；未返回运行 ID、来源、路径、文件名、URL、对象标识或错误正文。",
            _now(),
        )],
        suggestions=(
            ["如需处理失败项，可先查看 STRM 失败分诊。"] if active_failures else []
        ),
    )
