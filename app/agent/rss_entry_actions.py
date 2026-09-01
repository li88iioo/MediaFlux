"""RSS 条目的安全列表、确认标记与精确 qB 提交。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app import database as db
from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.agent.result_projection import sanitize_public_text
from app.logger import get_logger

logger = get_logger(__name__)
_ALLOWED_STATUSES = {"all", "pending", "submitting", "downloaded", "failed", "skipped"}
_SAFE_FAILURE_LABELS = {
    "": "",
    "unknown_failure": "未分类失败",
    "submission_outcome_unknown": "提交结果待确认",
    "qb_outcome_unknown": "qBittorrent 提交结果待确认",
    "guangya_outcome_unknown": "光鸭提交结果待确认",
    "download_failed": "下载失败",
    "invalid_payload": "条目数据无效",
    "qb_auth_failed": "qBittorrent 认证失败",
    "qb_rate_limited": "qBittorrent 请求受限",
    "qb_unavailable": "qBittorrent 暂不可用",
    "qb_submit_failed": "qBittorrent 提交失败",
    "guangya_submit_failed": "光鸭提交失败",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _positive_ids(value: Any, *, maximum: int) -> list[int]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise AgentToolError(f"entry_numbers 必须包含 1 到 {maximum} 个条目编号")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise AgentToolError("entry_numbers 只能包含正整数")
        if item in result:
            raise AgentToolError("entry_numbers 不能重复")
        result.append(int(item))
    return result


def rss_entry_summaries_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) - {"subscription_number", "status", "limit"}:
        raise AgentToolError("rss.entry_summaries 只接受 subscription_number、status 和 limit")
    status = str(arguments.get("status") or "pending").strip().casefold()
    limit = arguments.get("limit", 20)
    if status not in _ALLOWED_STATUSES:
        raise AgentToolError("status 无效")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise AgentToolError("limit 必须是 1 到 50 的整数")
    result: dict[str, Any] = {"status": status, "limit": int(limit)}
    if "subscription_number" in arguments:
        number = arguments["subscription_number"]
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise AgentToolError("subscription_number 必须是正整数")
        result["subscription_number"] = int(number)
    return result


def rss_mark_entries_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"entry_numbers", "processed"}:
        raise AgentToolError("必须且只能提供 entry_numbers 和 processed")
    if not isinstance(arguments["processed"], bool):
        raise AgentToolError("processed 必须是布尔值")
    return {
        "entry_numbers": _positive_ids(arguments["entry_numbers"], maximum=50),
        "processed": bool(arguments["processed"]),
    }


def rss_submit_entries_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"entry_numbers"}:
        raise AgentToolError("必须且只能提供 entry_numbers")
    return {"entry_numbers": _positive_ids(arguments["entry_numbers"], maximum=20)}


def _query_rows(entry_numbers: list[int]) -> list[Any]:
    placeholders = ",".join("?" for _ in entry_numbers)
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT e.id,e.rss_item_id,e.title,e.status,e.processed,e.submitted_at,"
            "e.processed_at,e.failure_code,e.failure_retryable,e.pub_date,e.payload,e.created_at,"
            "COALESCE(i.name,'') AS sub_name,COALESCE(i.download_method,'') AS download_method,"
            "COALESCE(i.qb_save_path,'') AS qb_save_path,m.season AS media_season,"
            "m.episode AS media_episode,COALESCE(m.skip_reason,'') AS skip_reason "
            "FROM rss_entries e LEFT JOIN rss_items i ON i.id=e.rss_item_id "
            "LEFT JOIN rss_entry_media m ON m.rss_entry_id=e.id "
            f"WHERE e.id IN ({placeholders}) ORDER BY e.id DESC",
            tuple(entry_numbers),
        ).fetchall()


def _stable_hash(value: Any, *, domain: str) -> str:
    return confirmation_context_fingerprint(value, domain=domain)


def list_rss_entry_summaries(arguments: dict[str, Any]) -> ToolResult:
    status = None if arguments["status"] == "all" else arguments["status"]
    rows = db.list_rss_entries(
        sub_id=arguments.get("subscription_number"),
        status=status,
        limit=arguments["limit"],
        order="received_desc",
    )
    entries: list[dict[str, Any]] = []
    for row in rows:
        failure_code = str(row["failure_code"] or "")
        entries.append({
            "entry_number": int(row["id"]),
            "subscription_number": int(row["rss_item_id"]),
            "subscription_name": sanitize_public_text(row["sub_name"], limit=80) or "RSS 订阅",
            "title": sanitize_public_text(row["title"], limit=180) or "未命名条目",
            "status": str(row["status"] or "unknown"),
            "processed": bool(row["processed"]),
            "published_at": str(row["pub_date"] or ""),
            "created_at": str(row["created_at"] or ""),
            "season": row["media_season"],
            "episode": row["media_episode"],
            "skip_reason": sanitize_public_text(row["skip_reason"], limit=120),
            "failure_code": _SAFE_FAILURE_LABELS.get(failure_code, "未分类失败") if failure_code else "",
            "failure_retryable": bool(row["failure_retryable"]),
        })
    return ToolResult(
        ok=True,
        status="completed" if entries else "empty",
        summary=f"找到 {len(entries)} 个符合条件的 RSS 条目" if entries else "没有符合条件的 RSS 条目",
        data={"entries": entries, "entry_count": len(entries), "limits": {"max_items": 50}},
        evidence=[Evidence(
            "sqlite:rss_entries",
            "仅返回公开条目编号、标题、状态和季集线索；未读取或返回 GUID、payload、下载 URL、路径或凭据。",
            _now(),
        )],
    )


def _mark_snapshot(arguments: dict[str, Any]) -> dict[str, Any]:
    rows = _query_rows(arguments["entry_numbers"])
    by_id = {int(row["id"]): row for row in rows}
    requested = arguments["entry_numbers"]
    eligible_statuses = {"pending", "failed", "skipped"} if arguments["processed"] else {"failed", "skipped"}
    snapshot = [{
        "id": item,
        "status": str(by_id[item]["status"] or "") if item in by_id else "missing",
        "processed": bool(by_id[item]["processed"]) if item in by_id else False,
        "created_at": str(by_id[item]["created_at"] or "") if item in by_id else "",
        "failure_code": str(by_id[item]["failure_code"] or "") if item in by_id else "",
        "failure_retryable": bool(by_id[item]["failure_retryable"]) if item in by_id else False,
    } for item in requested]
    eligible = all(item["status"] in eligible_statuses for item in snapshot)
    return {
        "snapshot": snapshot,
        "eligible": eligible and len(rows) == len(requested),
        "fingerprint": _stable_hash(
            {"processed": arguments["processed"], "rows": snapshot},
            domain="rss-mark-entries",
        ),
    }


def prepare_mark_rss_entries(arguments: dict[str, Any]) -> tuple[ToolResult, str]:
    state = _mark_snapshot(arguments)
    if not state["eligible"]:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="所选 RSS 条目当前不能统一执行该标记",
            error="条目可能不存在、正在提交、已下载或状态已变化。",
            suggestions=["请重新列出 RSS 条目后选择可标记的编号。"],
        ), state["fingerprint"]
    count = len(arguments["entry_numbers"])
    label = "已处理" if arguments["processed"] else "未处理"
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将把 {count} 个 RSS 条目标记为{label}",
        data={"selected_count": count, "processed": arguments["processed"], "effects": [
            "只更新本地 RSS 条目处理状态，不删除订阅、下载任务或文件。",
            "正在提交或已经下载的条目不会被此动作覆盖。",
        ]},
        evidence=[Evidence("sqlite:rss_entries", "已只读冻结所选条目的当前状态。", _now())],
    ), state["fingerprint"]


def mark_rss_entries_confirmed(arguments: dict[str, Any], expected_context: str) -> ToolResult:
    state = _mark_snapshot(arguments)
    if not state["eligible"] or state["fingerprint"] != str(expected_context or ""):
        raise AgentToolError("RSS 条目状态已变化，请重新预检", code="confirmation_stale")
    updated = db.update_rss_entries_processed_snapshot(
        state["snapshot"], arguments["processed"]
    )
    if int(updated) != len(arguments["entry_numbers"]):
        raise AgentToolError("RSS 条目标记发生并发冲突，请重新查看", code="confirmation_stale")
    label = "已处理" if arguments["processed"] else "未处理"
    return ToolResult(
        ok=True,
        status="completed",
        summary=f"已将 {updated} 个 RSS 条目标记为{label}",
        data={"affected": int(updated), "processed": arguments["processed"]},
        evidence=[Evidence("sqlite:rss_entries", "已按确认时冻结的条目集合更新状态。", _now())],
    )


def _submit_snapshot(arguments: dict[str, Any]) -> dict[str, Any]:
    from app.modules.rss import capture_rss_qb_runtime_config

    runtime, config_error = capture_rss_qb_runtime_config()
    rows = _query_rows(arguments["entry_numbers"])
    default_method = str(runtime.get("default_method") or "").strip().casefold()
    entries: list[dict[str, Any]] = []
    for row in rows:
        method = str(row["download_method"] or "").strip().casefold() or default_method
        if str(row["status"] or "") != "pending" or bool(row["processed"]) or method != "qb":
            continue
        entries.append({
            "id": int(row["id"]), "rss_item_id": int(row["rss_item_id"]),
            "title": str(row["title"] or ""), "payload": str(row["payload"] or ""),
            "created_at": str(row["created_at"] or ""),
            "download_method": str(row["download_method"] or ""),
            "qb_save_path": str(row["qb_save_path"] or ""),
        })
    complete = len(entries) == len(arguments["entry_numbers"])
    fingerprint = _stable_hash(
        {
            "requested": arguments["entry_numbers"],
            "entries": entries,
            "runtime": runtime,
            "config_error": str(config_error or ""),
        },
        domain="rss-submit-entries-to-qb",
    )
    return {
        "entries": entries, "runtime": runtime, "config_error": str(config_error or ""),
        "complete": complete, "fingerprint": fingerprint,
    }


def prepare_submit_rss_entries(arguments: dict[str, Any]) -> tuple[ToolResult, str]:
    state = _submit_snapshot(arguments)
    if not state["complete"]:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="所选 RSS 条目不是完整的待处理 qB 集合",
            error="条目可能不存在、已处理、正在提交或目标不是 qBittorrent。",
            suggestions=["请重新列出 pending RSS 条目后选择编号。"],
        ), state["fingerprint"]
    if state["config_error"] or not state["runtime"].get("url"):
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="qBittorrent 提交配置当前不可用",
            error="请检查 qBittorrent 配置后重新预检。",
        ), state["fingerprint"]
    count = len(state["entries"])
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将向 qBittorrent 提交指定的 {count} 个 RSS 条目",
        data={"selected_count": count, "target": "qbittorrent", "effects": [
            "所选条目将原子认领后提交到 qBittorrent。",
            "提交结果未知的条目会进入人工核对状态，不会自动重复提交。",
        ]},
        evidence=[Evidence("sqlite:rss_entries", "已只读冻结精确条目集合和当前 qB 配置。", _now())],
    ), state["fingerprint"]


def submit_rss_entries_confirmed(arguments: dict[str, Any], expected_context: str) -> ToolResult:
    state = _submit_snapshot(arguments)
    if (
        not state["complete"]
        or state["fingerprint"] != str(expected_context or "")
        or not state["runtime"].get("url")
    ):
        raise AgentToolError("RSS 条目或 qB 配置已变化，请重新预检", code="confirmation_stale")
    from app.modules.rss import RSSEngine

    raw = RSSEngine().submit_pending_qb_snapshot(state["entries"], state["runtime"])
    requested = max(0, int(raw.get("requested") or 0))
    claimed = max(0, int(raw.get("claimed") or 0))
    submitted = max(0, int(raw.get("submitted") or 0))
    failed = max(0, int(raw.get("failed") or 0))
    unknown = min(failed, max(0, int(raw.get("outcome_unknown") or 0)))
    if raw.get("conflict") or claimed != requested:
        raise AgentToolError("RSS 条目在提交前发生变化，请重新预检", code="confirmation_stale")
    if unknown:
        status, ok = ("partial", True) if submitted else ("review_required", False)
        summary = f"RSS 提交结果：成功 {submitted}，待核对 {unknown}，失败 {failed - unknown}"
    elif failed and submitted:
        status, ok, summary = "partial", True, f"RSS 条目部分提交完成：成功 {submitted}，失败 {failed}"
    elif failed:
        status, ok, summary = "failed", False, f"本次 {failed} 个 RSS 条目均未成功提交"
    else:
        status, ok, summary = "completed", True, f"已向 qBittorrent 提交 {submitted} 个 RSS 条目"
    logger.info("Agent 精确 RSS qB 提交 requested=%s submitted=%s failed=%s", requested, submitted, failed)
    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "target": "qbittorrent", "requested": requested, "claimed": claimed,
            "submitted": submitted, "failed": failed,
            **({"outcome_unknown": unknown} if unknown else {}),
        },
        evidence=[Evidence("rss_submission", "已按确认时冻结的精确集合执行一次提交。", _now())],
        suggestions=["请先在 qBittorrent 中核对待确认任务，勿直接重复提交。"] if unknown else [],
        error="部分提交结果未知。" if unknown else ("RSS 条目提交未全部成功。" if failed else ""),
    )
