"""活动查询和跨阶段原因说明；公开内容仅来自有证据的领域状态。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolContext, ToolReference, ToolResult
from app.agent.public_safety import sanitize_public_text
from app.repositories import activity

_KIND_LABELS = {"download": "下载", "organize": "光鸭整理", "local_media": "本地整理"}
_FAILED = {
    "failed",
    "partial_failed",
    "revert_failed",
    "attention",
    "manual_review",
    "requires_manual",
    "manual",
    "interrupted",
    "rollback_failed",
}
_DONE = {
    "completed",
    "success",
    "visible",
    "reverted",
    "returned",
    "cancelled",
    "skipped",
    "not_required",
    "disabled",
    "deleted",
}


def search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) - {"query", "limit"}:
        raise AgentToolError("只接受标题 query 与 limit")
    query = arguments.get("query", "")
    limit = arguments.get("limit", 10)
    if not isinstance(query, str) or len(query.strip()) > 120:
        raise AgentToolError("标题应不超过 120 字")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise AgentToolError("limit 应为 1–20")
    return {"query": query.strip(), "limit": limit}


def selection_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) - {
        "activity_selection_ref",
        "position",
    }:
        raise AgentToolError("只接受活动引用与序号")
    ref = arguments.get("activity_selection_ref")
    if not isinstance(ref, str) or not re.fullmatch(r"ref_[A-Za-z0-9_-]{16,100}", ref):
        raise AgentToolError("请先查询活动，使用返回的活动引用")
    position = arguments.get("position", 1)
    if (
        isinstance(position, bool)
        or not isinstance(position, int)
        or not 1 <= position <= 20
    ):
        raise AgentToolError("活动序号应为 1–20")
    return {"activity_selection_ref": ref, "position": position}


def select_activity(arguments: dict[str, Any]) -> dict:
    payload = arguments.get("activity_selection")
    items = payload.get("items") if isinstance(payload, dict) else None
    position = arguments.get("position", 1)
    if not isinstance(items, list) or not 1 <= position <= len(items):
        raise AgentToolError("活动引用无效，请重新查询", code="precondition_failed")
    item = items[position - 1]
    if (
        not isinstance(item, dict)
        or item.get("kind") not in _KIND_LABELS
        or type(item.get("id")) is not int
        or item["id"] < 1
    ):
        raise AgentToolError("活动引用类型无效", code="precondition_failed")
    return dict(item)


def _text(value: Any, limit: int = 240) -> str:
    return sanitize_public_text(str(value or ""), limit=limit)


def _stamp(value: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return ""


def _status(value: Any) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"[a-z_]{1,40}", text) else "unknown"


def _evidence(description: str) -> list[Evidence]:
    return [
        Evidence(
            "persisted_activity",
            description,
            datetime.now().astimezone().isoformat(timespec="seconds"),
        )
    ]


def search_activities(arguments: dict, _context: ToolContext) -> ToolResult:
    rows = activity.search(**arguments)
    limited = len(rows) > arguments["limit"]
    rows = rows[: arguments["limit"]]
    references = (
        [
            ToolReference(
                "activity_selection",
                {"items": [{"kind": row["kind"], "id": row["id"]} for row in rows]},
                ttl_seconds=86400,
            )
        ]
        if rows
        else []
    )
    return ToolResult(
        True,
        "success" if rows else "empty",
        f"找到 {len(rows)} 条活动记录",
        data={
            "items": [
                {
                    "position": i,
                    "kind": row["kind"],
                    "label": _KIND_LABELS[row["kind"]],
                    "title": _text(row["title"], 120),
                    "status": _status(row["status"]),
                    "updated_at": _stamp(row.get("updated_at") or row["created_at"]),
                }
                for i, row in enumerate(rows, 1)
            ],
            "has_more": limited,
            "scope": "标题匹配仅用于选对象，不表示这些记录属于同一任务",
        },
        references=references,
        evidence=_evidence(
            "来自项目持久化记录；序号绑定到会话引用，未读取外部下载器实时数据。"
        ),
    )


def _stage(
    name: str, status: Any, *, stamp: Any = "", error: Any = "", **extra: Any
) -> dict:
    normalized = _status(status)
    return {
        "stage": name,
        "status": normalized,
        "updated_at": _stamp(stamp),
        "needs_attention": normalized in _FAILED,
        "reason": _text(error),
        **extra,
    }


def timeline_snapshot(target: dict) -> ToolResult:
    snapshot = activity.snapshot(target["kind"], target["id"])
    if snapshot is None:
        return ToolResult(
            False,
            "not_found",
            "活动记录已被清理",
            error="请重新查询活动，不能据旧引用推断成功。",
        )
    row = snapshot["record"]
    stages: list[dict] = []
    links: list[dict] = []
    gaps: list[str] = []
    if target["kind"] == "download":
        stages.append(
            _stage(
                "下载请求",
                row["status"],
                stamp=row["updated_at"],
                error=row.get("error"),
            )
        )
        latest = {}
        for log in snapshot["logs"]:
            latest.setdefault(log["source"], log)
        targets = ("qb", "guangya") if row["targets"] == "both" else (row["targets"],)
        for backend, field in (("qb", "qb_status"), ("guangya", "gy_status")):
            if backend not in targets:
                continue
            log = latest.get(backend, {})
            progress = log.get("progress")
            stages.append(
                _stage(
                    "qB 下载" if backend == "qb" else "光鸭下载",
                    row[field],
                    stamp=log.get("updated_at"),
                    error=log.get("error"),
                    progress=max(0.0, min(100.0, float(progress)))
                    if isinstance(progress, (int, float))
                    else None,
                )
            )
        for name, prefix in (("光鸭整理", "organize"), ("STRM", "strm")):
            status = row.get(prefix + "_status")
            if status or row.get(prefix + "_run_id"):
                stages.append(
                    _stage(
                        name,
                        status,
                        stamp=row.get(prefix + "_finished_at"),
                        error=row.get(prefix + "_error"),
                    )
                )
        if row.get("local_import_status"):
            stages.append(
                _stage(
                    "本地入库",
                    row["local_import_status"],
                    stamp=row.get("local_import_completed_at"),
                    error=row.get("local_import_error"),
                )
            )
        for task in snapshot["local_tasks"]:
            stages.append(
                _stage(
                    "关联本地任务",
                    task["status"],
                    stamp=task["updated_at"],
                    error=task.get("error"),
                )
            )
            links.append({"kind": "local_media", "id": task["id"]})
        verification = snapshot["verification"]
        if verification:
            stages.append(
                _stage(
                    "媒体库可见性复核",
                    verification["status"],
                    stamp=verification["updated_at"],
                    result=_status(verification["result"]),
                    attempts=verification["attempts"],
                )
            )
        else:
            gaps.append("未建立媒体库可见性复核记录；下载或 STRM 完成不等于已经入库。")
        for run in snapshot["runs"]:
            stages.append(
                _stage(
                    "关联运行 · " + _text(run["task_name"], 40),
                    run["status"],
                    stamp=run.get("finished_at") or run["started_at"],
                    error=run.get("error"),
                )
            )
    elif target["kind"] == "organize":
        stages.append(
            _stage(
                "整理",
                row["status"],
                stamp=row.get("updated_at"),
                error=row.get("error"),
            )
        )
        for step in reversed(snapshot["steps"]):
            stages.append(
                _stage(
                    "文件操作 · " + _text(step.get("action"), 40),
                    step["status"],
                    stamp=step.get("finished_at") or step.get("started_at"),
                    error=step.get("error"),
                    historical=True,
                )
            )
        gaps.append("仅展示本整理日志记录的操作；未按标题猜测下载、STRM 或媒体库关联。")
    else:
        stages.append(
            _stage(
                "本地整理",
                row["status"],
                stamp=row["updated_at"],
                error=row.get("error"),
            )
        )
        items = snapshot["items"]
        stages.append(
            _stage(
                "成员处理",
                "partial_failed"
                if any(item["status"] in _FAILED for item in items)
                else "observed",
                count=min(len(items), 100),
                truncated=len(items) > 100,
            )
        )
        gaps.append("成员处理为有界持久化快照，不代表已实时检查媒体服务器。")
    # 当前日志/任务状态是权威；历史失败继续可见，但不再次发已修复的告警。
    current_stages = [stage for stage in stages if not stage.get("historical")]
    attention = [stage for stage in current_stages if stage["needs_attention"]]
    pending = [
        stage
        for stage in current_stages
        if stage["status"] not in _DONE | _FAILED | {"observed"}
    ]
    overall = "attention" if attention else ("in_progress" if pending else "completed")
    explanation = "；".join(
        f"{item['stage']}：{item['reason'] or item['status']}" for item in attention[:3]
    )
    if not explanation:
        explanation = (
            "仍在处理或阶段状态尚不明确，请等待后续记录。"
            if pending
            else "已记录的处理阶段已结束；没有记录的阶段不能推断完成。"
        )
    return ToolResult(
        True,
        overall,
        explanation,
        data={
            "title": _text(row.get("title"), 120),
            "kind": target["kind"],
            "stages": stages,
            "explanation": explanation,
            "gaps": gaps,
            "terminal": not pending,
            "needs_attention": bool(attention),
            "freshness": "persisted_snapshot",
            "observed_at": _stamp(db_now()),
        },
        evidence=_evidence(
            "阶段按请求标识、后台运行标识或 qB 任务身份关联；时间线是已有日志的事实投影，不是实时模型过程。"
        ),
        references=[
            ToolReference(
                "activity_selection", {"items": [target, *links]}, ttl_seconds=86400
            )
        ],
    )


def db_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_activity_timeline(arguments: dict, _context: ToolContext) -> ToolResult:
    return timeline_snapshot(select_activity(arguments))


def attach_activity_reference(result: ToolResult, tool_name: str) -> None:
    """领域完成钩子：给真实返回的请求建立安全续句引用，不依赖 LLM 提取 ID。"""
    if tool_name not in {"ingest.submit", "downloads.retry_submission"}:
        return
    data = result.data if isinstance(result.data, dict) else {}
    payloads = data.get("items") if isinstance(data.get("items"), list) else [data]
    identifiers = list(
        dict.fromkeys(
            item["request_id"]
            for item in payloads
            if isinstance(item, dict)
            and type(item.get("request_id")) is int
            and item["request_id"] > 0
        )
    )[:20]
    if identifiers:
        result.references.append(
            ToolReference(
                "activity_selection",
                {
                    "items": [
                        {"kind": "download", "id": identifier}
                        for identifier in identifiers
                    ]
                },
                ttl_seconds=86400,
            )
        )
