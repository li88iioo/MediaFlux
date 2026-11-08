"""本地手动扫描回执：复用 task_runs 保存批次成员与通知时的结果。

扫描回执的 success 只表示扫描/入队结束，不代表其中的媒体已经归档。
文件执行状态仍由本地媒体任务及其文件记录决定，不另建调度器或执行状态机。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app import database as db

UNRECORDED_LOCAL_SCAN = "LM-UNRECORDED"
_TASK_NAME = "local_media_scan"
_SCAN_REF = re.compile(r"LM([1-9][0-9]{0,17})\Z")
_SCAN_COUNTS = (
    "source_count", "scanned_sources", "candidate_count", "queued_count",
    "failed_sources", "skipped_sources", "enqueue_failures",
)
_RESULT_COUNTS = (
    "completed", "requires_manual", "failed", "moved_items", "skipped_items",
)
SCAN_FAILURE_MESSAGES = {
    "source_changed": "来源或归档目标在扫描前变化，或扫描已取消，请重新核对后预检。",
    "source_unavailable": "来源目录不可访问或配置无效，请检查目录与挂载。",
    "scan_incomplete": "部分来源目录未能完整扫描，请检查读取权限后重试。",
    "enqueue_failed": "部分媒体候选未能加入整理队列，请检查服务状态后重试。",
}


def local_media_scan_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """扫描阶段的唯一安全回执字段；不包含路径、来源名称或数据库标识。"""
    result: dict[str, Any] = {
        key: int(summary[key] or 0) for key in _SCAN_COUNTS if key in summary
    }
    status = summary.get("scan_status")
    if status in {"completed", "partial", "failed"}:
        result["scan_status"] = status
    errors = summary.get("scan_errors", [])
    result["scan_errors"] = [
        {"code": code, "message": message}
        for code, message in SCAN_FAILURE_MESSAGES.items()
        if any(isinstance(item, dict) and item.get("code") == code for item in errors)
    ]
    return result


def _payload(row: Any, *, owner: str) -> dict[str, Any] | None:
    if row is None or str(row["task_name"]) != _TASK_NAME:
        return None
    try:
        payload = json.loads(str(row["result"] or "{}"))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("owner") != owner:
        return None
    task_ids = payload.get("task_ids")
    if not isinstance(task_ids, list) or any(type(v) is not int or v < 1 for v in task_ids):
        return None
    return {
        "id": int(row["id"]),
        "scan_ref": f"LM{row['id']}",
        "owner": owner,
        "task_ids": list(dict.fromkeys(task_ids)),
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
        "summary": payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
    }


def record_local_media_scan(summary: dict[str, Any], *, owner: str = "admin") -> str:
    """在唤醒任务 Worker 之前记录扫描成员，后续复用旧任务也不会丢失批次归属。"""
    owner = owner.strip()
    if not owner:
        raise ValueError("owner 不能为空")
    task_ids = list(dict.fromkeys(int(v) for v in summary.get("task_ids", [])))
    if any(v < 1 for v in task_ids):
        raise ValueError("扫描任务标识无效")
    scan_summary = local_media_scan_summary(summary)
    encoded = json.dumps(
        {"version": 1, "owner": owner, "task_ids": task_ids, "summary": scan_summary},
        ensure_ascii=False,
    )
    timestamp = db.now()
    # 这里是扫描回执，不占用长期 running 状态；不与任务 Worker 抢执行状态。
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO task_runs(task_name,trigger_type,status,started_at,finished_at,result) "
            "VALUES(?,'manual',?,?,?,?)",
            (
                _TASK_NAME,
                {"failed": "failed", "partial": "partial"}.get(str(scan_summary.get("scan_status") or ""), "success"),
                str(summary.get("scan_started_at") or timestamp), timestamp, encoded,
            ),
        )
        return f"LM{cursor.lastrowid}"


def resolve_local_media_scan(scan_ref: str = "", *, owner: str = "admin") -> dict[str, Any]:
    """只解析明确扫描或本工作区最近扫描；旧版本没有回执时绝不用全历史冒充。"""
    scan_ref = (scan_ref or "").strip().upper()
    if scan_ref == UNRECORDED_LOCAL_SCAN:
        raise LookupError("本次扫描回执保存失败，不能使用以前的扫描替代")
    if scan_ref:
        match = _SCAN_REF.fullmatch(scan_ref)
        if not match:
            raise ValueError("scan_ref 必须是通知中的本地扫描编号（如 LM123）")
        result = _payload(db.get_task_run(int(match.group(1))), owner=owner)
    else:
        result = next(
            (item for row in db.list_task_runs(_TASK_NAME, limit=1000)
             if (item := _payload(row, owner=owner)) is not None),
            None,
        )
    if result is None:
        raise LookupError("没有找到本地扫描回执；旧版本通知无法还原准确批次，请勿把全部历史当成本次结果")
    return result


def finish_local_media_scan_report(
    scan_ref: str, summary: dict[str, Any], *, owner: str = "admin"
) -> None:
    """冻结通知对应的文件结果，之后重试任务不会改写本条通知的事实。"""
    run = resolve_local_media_scan(scan_ref, owner=owner)
    allowed = set(run["task_ids"])
    outcomes = [
        dict(item) for item in summary.get("task_outcomes", [])
        if isinstance(item, dict) and item.get("task_id") in allowed
    ]
    with db.get_conn() as conn:
        # 同一批次只冻结第一次终态报告，避免后续任务变化覆盖已送达通知。
        row = conn.execute("SELECT * FROM task_runs WHERE id=?", (run["id"],)).fetchone()
        current = _payload(row, owner=owner)
        if current is None:
            raise LookupError("本地扫描回执已移除")
        if current["summary"].get("reported_at"):
            return
        payload = {
            "version": 1, "owner": owner, "task_ids": run["task_ids"],
            "summary": {
                **current["summary"],
                **{key: int(summary[key] or 0) for key in _RESULT_COUNTS if key in summary},
                "task_outcomes": outcomes,
                "reported_at": db.now(),
            },
        }
        conn.execute(
            "UPDATE task_runs SET result=? WHERE id=? AND task_name=?",
            (json.dumps(payload, ensure_ascii=False), run["id"], _TASK_NAME),
        )
