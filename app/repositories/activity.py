"""活动关联与任务关注：只认持久化请求/运行标识，不凭标题拼接跨域时间线。"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from unicodedata import normalize

from app import database as db
from app.agent.public_safety import sanitize_public_text
from app.modules.media_automation_rules import notification_route_settings
from app.repositories import media_automation_rules as rules
from app.repositories.agent_jobs import agent_job_owner_digest
from app.repositories.download_requests import (
    download_display_title,
    download_request_public_summary,
)
from app.sensitive_data import contains_sensitive_credential, redact_sensitive_text

_TABLES = {
    "download": "download_requests",
    "organize": "organize_log",
    "local_media": "local_media_tasks",
}


def _follow_owner(owner: str) -> str:
    if not isinstance(owner, str) or not owner:
        raise ValueError("活动跟踪需要当前用户身份")
    return agent_job_owner_digest(owner)


def _public_follow(rule: dict[str, Any]) -> dict[str, Any]:
    settings = rule["settings"]
    result = {"rule_id": rule["id"], "enabled": rule["enabled"],
              "title": public_text(settings.get("title"), 120),
              "expires_at": settings.get("expires_at", ""), "next_check_at": rule["next_run_at"]}
    if rule.get("settings_error"):
        result["settings_error"] = rule["settings_error"]
    return result


def follows(*, owner: str) -> dict[str, Any]:
    items = [_public_follow(row) for row in rules.list_rules(_follow_owner(owner), kind="activity_follow")]
    return {"ok": True, "status": "completed", "summary": f"已读取 {len(items)} 条活动跟踪规则", "data": {"items": items}}


def prepare_follow(kind: str, identifier: int, *, owner: str, hours: int = 24,
                   delivery_route: Mapping[str, Any] | None = None) -> dict[str, Any]:
    digest = _follow_owner(owner)
    if type(hours) is not int or not 1 <= hours <= 168:
        raise ValueError("跟踪时长应为 1–168 小时")
    timeline = timeline_snapshot(kind, identifier)
    if not timeline["ok"]:
        raise ValueError("活动记录已不存在")
    target = {"kind": kind, "id": identifier}
    current = rules.find_activity_follow_rule(digest, target)
    snapshot = {"owner": digest, "target": target, "hours": hours,
                "rule_id": current["id"] if current else "", "revision": current["revision"] if current else 0,
                "route": notification_route_settings(owner, delivery_route=delivery_route)}
    data = timeline["data"]
    return {"snapshot": snapshot, "preview": {
        "ok": True, "status": "confirmation_required", "summary": "确认后跟踪这项活动的最终状态",
        "data": {"title": data["title"], "hours": hours, "current_status": data["explanation"],
                 "effects": ["约每 5 分钟检查一次已有任务记录；发现异常、记录的阶段结束或到期时通知一次并停止跟踪。",
                             "只观察，不启动整理、不自动重试，也不将下载完成视为媒体库入库。"],
                 "delivery": "Telegram 通知中心；受现有通知开关控制", "gaps": data.get("gaps", [])}}}


def follow(kind: str, identifier: int, *, owner: str, expected_snapshot: dict,
           hours: int = 24, delivery_route: Mapping[str, Any] | None = None) -> dict[str, Any]:
    prepared = prepare_follow(kind, identifier, owner=owner, hours=hours, delivery_route=delivery_route)
    snapshot = prepared["snapshot"]
    if snapshot != expected_snapshot:
        raise ValueError("活动跟踪配置或通知目标已改变，请重新确认")
    clock = datetime.now().astimezone()
    settings = {"target": snapshot["target"], "title": prepared["preview"]["data"]["title"],
                "expires_at": (clock + timedelta(hours=hours)).isoformat(timespec="seconds"), **snapshot["route"]}
    saved = rules.save_rule(snapshot["owner"], kind="activity_follow", settings=settings, enabled=True,
                            next_run_at=clock.isoformat(timespec="seconds"), rule_id=snapshot["rule_id"],
                            expected_revision=snapshot["revision"])
    if saved is None:
        raise ValueError("活动跟踪已被修改，请重新确认")
    return {"ok": True, "status": "completed", "summary": "活动跟踪已保存；这不表示原任务已完成", "data": _public_follow(saved)}


def prepare_unfollow(rule_id: str, *, owner: str) -> dict[str, Any]:
    digest = _follow_owner(owner)
    if not isinstance(rule_id, str) or not rule_id.startswith("auto_") or not 8 <= len(rule_id) <= 100:
        raise ValueError("跟踪规则引用无效")
    row = rules.get_rule(digest, rule_id)
    if row is None or row["kind"] != "activity_follow":
        raise ValueError("活动跟踪不存在或不属于当前用户")
    # 绑定配置而非租约/检查进度；后台正常推进不能撤销停止确认。
    snapshot = {key: row[key] for key in ("id", "owner_digest", "kind", "revision", "settings")}
    return {"snapshot": snapshot, "preview": {
        "ok": True, "status": "confirmation_required" if row["enabled"] else "no_changes",
        "summary": "确认后停止跟踪，不取消原任务" if row["enabled"] else "活动跟踪已停用，无需变更",
        "data": _public_follow(row)}}


def unfollow(rule_id: str, *, owner: str, expected_snapshot: dict) -> dict[str, Any]:
    prepared = prepare_unfollow(rule_id, owner=owner)
    row = prepared["snapshot"]
    if row != expected_snapshot:
        raise ValueError("跟踪状态已变化，请重新预检")
    saved = rules.save_rule(row["owner_digest"], kind=row["kind"], settings=row["settings"], enabled=False,
                            next_run_at=prepared["preview"]["data"]["next_check_at"],
                            rule_id=row["id"], expected_revision=row["revision"])
    if saved is None:
        raise ValueError("跟踪已被修改，请重新预检")
    return {"ok": True, "status": "completed", "summary": "活动跟踪已停用；原任务保持原样", "data": _public_follow(saved)}


def organize_logs(origin: str = "all", status: str = "all", keyword: str = "",
                  limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """返回管理员整理时间线，保留完整业务字段，凭据统一脱敏。"""
    try:
        page = db.organize_history_page(origin=origin, status=status, keyword=keyword,
                                        limit=limit, offset=offset)
    except sqlite3.Error as exc:
        raise RuntimeError("暂时无法读取整理记录") from exc
    page["items"] = [
        {**{key: redact_sensitive_text(value) if isinstance(value, str) else value
            for key, value in row.items()},
         "kind": "organize" if row["origin"] == "guangya" else "local_media"}
        for row in page["items"]
    ]
    return page


def search_page(query: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """按全域更新时间分页；页面与真实总数来自同一读快照，仅导出安全字段。"""
    if not isinstance(query, str):
        raise ValueError("活动标题查询必须是字符串")
    if type(limit) is not int or limit < 1:
        raise ValueError("limit 必须是正整数")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset 必须是非负整数")
    selections = []
    params: list[object] = []
    for rank, (kind, table) in enumerate(_TABLES.items()):
        title = "COALESCE(NULLIF(display_title,''),title)" if kind == "download" else "title"
        # 表名和类型只取本模块常量；instr 保留 %/_ 的文字包含语义。
        selections.append(
            f"SELECT '{kind}' AS kind,{rank} AS kind_rank,"
            f"id,{title} AS title,status,created_at,updated_at FROM {table} "
            "WHERE (?='' OR instr(lower(title),lower(?))>0"
            + (" OR instr(lower(display_title),lower(?))>0)" if kind == "download" else ")")
        )
        params.extend((query, query, query) if kind == "download" else (query, query))
    params.extend((limit, offset))
    order = "COALESCE(NULLIF(updated_at,''),created_at) DESC,id DESC,kind_rank ASC"
    with db.get_conn() as conn:
        rows = conn.execute(
            "WITH matched AS ("
            + " UNION ALL ".join(selections)
            + ") SELECT page.kind,page.id,page.title,page.status,"
            "page.created_at,page.updated_at,totals.total "
            "FROM (SELECT COUNT(*) AS total FROM matched) AS totals "
            "LEFT JOIN (SELECT kind,id,kind_rank,title,status,created_at,updated_at "
            f"FROM matched ORDER BY {order} LIMIT ? OFFSET ?) AS page ON 1=1 "
            f"ORDER BY {order}",
            params,
        ).fetchall()
    # COUNT 始终保留一行，即使无匹配或 offset 越界也不丢失真实 total。
    total = int(rows[0]["total"])
    items = [
        {
            "kind": row["kind"],
            "id": row["id"],
            "title": public_text(row["title"], 120),
            "status": _status(row["status"]),
            "created_at": _stamp(row["created_at"]),
            "updated_at": _stamp(row["updated_at"] or row["created_at"]),
        }
        for row in rows
        if row["kind"] is not None
    ]
    returned = len(items)
    has_more = offset + returned < total
    return {
        "items": items,
        "total": total,
        "returned": returned,
        "has_more": has_more,
        "next_offset": offset + returned if has_more else None,
    }


def snapshot(kind: str, identifier: int, *, include_download_evidence: bool = False) -> dict | None:
    table = _TABLES.get(kind)
    if table is None:
        raise ValueError("活动类型无效")
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id=?", (identifier,)
        ).fetchone()
        if row is None:
            return None
        result: dict = {"kind": kind, "record": dict(row)}
        if kind == "download":
            result["logs"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT source,status,progress,error,created_at,updated_at,completed_at "
                    "FROM download_log WHERE request_id=? ORDER BY id DESC LIMIT 40",
                    (identifier,),
                )
            ]
            result["runs"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT id,task_name,status,started_at,finished_at,error,"
                    "CASE WHEN id=? AND task_name='guangya_organize' THEN result END AS result FROM task_runs "
                    "WHERE id IN (?,?) ORDER BY id",
                    (row["organize_run_id"], row["organize_run_id"], row["strm_run_id"]),
                )
            ]
            verification = conn.execute(
                "SELECT status,result,attempts,updated_at,targets_json FROM agent_download_verifications WHERE request_id=?",
                (identifier,),
            ).fetchone()
            result["verification"] = dict(verification) if verification else None
            # 本地任务归属按 tracker 写入的 qB 身份确定，绝不使用媒体标题猜测。
            result["local_tasks"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT id,title,status,error,updated_at,completed_at FROM local_media_tasks "
                    "WHERE qb_hash=? AND ?!='' ORDER BY id DESC LIMIT 20",
                    (row["qb_task_id"], row["qb_task_id"] or ""),
                )
            ]
            # 小列表已完整；达到展示上限才额外汇总，避免常见单任务路径多查一次。
            result["local_task_status_counts"] = (
                {
                    str(item["status"] or ""): int(item["total"])
                    for item in conn.execute(
                        "SELECT status,COUNT(*) AS total FROM local_media_tasks "
                        "WHERE qb_hash=? AND ?!='' GROUP BY status",
                        (row["qb_task_id"], row["qb_task_id"] or ""),
                    )
                }
                if len(result["local_tasks"]) == 20
                else {}
            )
            if include_download_evidence:
                result["request"] = download_request_public_summary(result["record"])
                result["destination_evidence"] = _download_destination_evidence(conn, result)
                result["organize_evidence"] = _download_organize_evidence(conn, result)

        elif kind == "organize":
            result["steps"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM organize_operation_steps WHERE log_id=? ORDER BY id DESC LIMIT 100",
                    (identifier,),
                )
            ]
        else:
            result["item_status_counts"] = {
                str(item["status"] or ""): int(item["total"])
                for item in conn.execute(
                    "SELECT status,COUNT(*) AS total FROM local_media_task_items "
                    "WHERE task_id=? GROUP BY status",
                    (identifier,),
                )
            }
    return result


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
    "resubmitted",
    "skipped",
    "not_required",
    "disabled",
    "deleted",
}


def public_text(value: Any, limit: int = 240) -> str:
    return sanitize_public_text(str(value or ""), limit=limit)


def _stamp(value: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return ""


def _status(value: Any) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"[a-z_]{1,40}", text) else "unknown"


def _stage(
    name: str, status: Any, *, stamp: Any = "", error: Any = "", **extra: Any
) -> dict:
    normalized = _status(status)
    return {
        "stage": name,
        "status": normalized,
        "updated_at": _stamp(stamp),
        "needs_attention": normalized in _FAILED,
        "reason": public_text(error),
        **extra,
    }


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


_DOWNLOAD_EVIDENCE_LIMIT = 50
_ACCEPTED_DOWNLOAD_STATES = {"submitted", "downloading", "completed"}
_UNCONFIRMED_DELETE_STATES = {"pending", "failed", "interrupted"}


def _cloud_id(value: object) -> str:
    return value if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", value)
                     and not contains_sensitive_credential(value)) else ""


def _destination_name(value: object) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 255:
        return ""
    checked = normalize("NFKC", value)
    if checked.strip() in {".", ".."} or re.search(r"[/\\\x00-\x1f\x7f-\x9f\ud800-\udfff]|%[0-9a-fA-F]{2}", checked):
        return ""
    # 安全检查可解码和归一化，但展示保留原始文件名，例如全角括号与书名号。
    return value if public_text(value, 255) else ""


def _organize_receipt_item(item: dict, fields: tuple[str, ...]) -> dict:
    result: dict[str, Any] = {}
    for key in fields:
        if key not in item:
            continue
        value = item[key]
        if not isinstance(value, str):
            if value is None or type(value) is int:
                result[key] = value
        elif key in {"file_id", "parent_id"}:
            if _cloud_id(value):
                result[key] = value
        elif key == "rel_dir":
            checked = normalize("NFKC", value).lstrip()
            if (len(value) <= 1024 and not re.match(r"^[a-z][a-z0-9+.-]*:", checked, re.IGNORECASE)
                    and (not value or all(_destination_name(part) for part in value.split("/")))):
                result[key] = value
        elif not value or _destination_name(value) or (
            key == "media_type" and len(value) <= 40 and set(value.lower().split("/")) <= {"movie", "tv", "series", "episode"}
        ):
            result[key] = value
    return result


def _download_organize_evidence(conn: sqlite3.Connection, stored: dict) -> dict:
    """只投影绑定运行中独占该请求的来源，不能按标题或整个批次推断归属。"""
    from app.modules.organize_results import read_organize_result

    row = stored["record"]
    evidence: dict[str, Any] = {
        "status": "unavailable", "freshness": "persisted_snapshot",
        "message": "未保存可独立归属此请求的整理明细；不能按标题猜测关联。",
        "media": [], "files": [],
    }
    run = next((run for run in stored["runs"]
                if run["id"] == row.get("organize_run_id")
                and run["task_name"] == "guangya_organize"), None)
    source_id = _cloud_id(row.get("gy_target_dir"))
    if (run is None or not row.get("organize_task_id") or row.get("gy_isolated") != 1
            or source_id in {"", "0"}):
        return evidence
    try:
        result = read_organize_result(json.loads(run["result"] or "{}"))
    except (ValueError, TypeError):
        return evidence
    if result["task_id"] != row["organize_task_id"]:
        return evidence
    sources = result.get("sources")
    if not isinstance(sources, list):
        return evidence
    selected = [source for source in sources if isinstance(source, dict) and source.get("id") == source_id]
    if len(selected) != 1:
        return evidence
    stats = selected[0].get("stats")
    if not isinstance(stats, dict):
        return evidence
    context = stats.get("notification_context")
    if not isinstance(context, dict) or context.get("download_request_ids") != [row["id"]]:
        return evidence

    receipts = [stats]
    # 同一批次可能有多个来源，只读取准确绑定本请求及来源的确认执行回执。
    confirmed = conn.execute(
        """
        SELECT c.result_json FROM organize_confirmations c
        WHERE c.organize_task_id=? AND c.status='completed'
          AND CASE WHEN json_valid(c.payload_json) THEN
              COALESCE(json_extract(c.payload_json,'$.kind'),'guangya')='guangya'
              AND json_extract(c.payload_json,'$.source_dir_id')=?
              AND json_type(c.payload_json,'$.download_request_ids')='array' AND EXISTS (
                  SELECT 1 FROM json_each(c.payload_json,'$.download_request_ids')
                  WHERE type='integer' AND value=?
              )
          ELSE 0 END
        ORDER BY c.id DESC LIMIT ?
        """, (row["organize_task_id"], source_id, row["id"], _DOWNLOAD_EVIDENCE_LIMIT + 1),
    ).fetchall()
    for card in reversed(confirmed[:_DOWNLOAD_EVIDENCE_LIMIT]):
        try:
            completed = json.loads(card["result_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(completed, dict):
            receipts.append(completed)

    def project(key: str, fields: tuple[str, ...]) -> tuple[list[dict], int]:
        items = [item for receipt in receipts if isinstance(receipt.get(key), list)
                 for item in receipt[key] if isinstance(item, dict)]
        return [_organize_receipt_item(item, fields) for item in items[:_DOWNLOAD_EVIDENCE_LIMIT]], len(items)

    media, media_count = project("media_items", (
        "title", "year", "media_type", "tmdb_id", "season", "episode", "filename",
    ))
    files, file_count = project("strm_changes", ("file_id", "parent_id", "name", "rel_dir", "kind", "action"))
    return {
        **evidence, "status": "recorded", "run_id": run["id"],
        "source_dir_id": source_id, "recorded_at": _stamp(run["finished_at"]),
        "historical": "resubmitted" in (row.get("gy_status"), row.get("organize_status")),
        "message": "这是该请求执行整理时的持久回执；当前位置和媒体库可见性需要另行核验。",
        "scan_complete": stats.get("scan_complete") is True,
        "media": media, "media_count": media_count, "files": files, "file_count": file_count,
        "truncated": media_count > len(media) or file_count > len(files) or len(confirmed) > _DOWNLOAD_EVIDENCE_LIMIT,
    }


def _guangya_receiving(conn: sqlite3.Connection, row: dict) -> dict | None:
    directory = _cloud_id(row["gy_target_dir"])
    status = row["gy_status"]
    isolated = row["gy_isolated"] == 1
    if not directory or (isolated and directory == "0") or not (isolated or status in _ACCEPTED_DOWNLOAD_STATES):
        return None
    cleanup = row["gy_staging_cleanup_status"]
    parent = _cloud_id(row["gy_staging_parent_dir"])
    finalized = not isolated and status == "completed" and cleanup == "completed" and bool(parent and row["gy_staging_name"])
    kind = "staging" if isolated else ("finalized" if finalized else "direct")
    name = _destination_name(row["gy_staging_name"] if isolated else row["gy_target_name"])
    # 单项提升后 tracker 的显示名可能来自当前默认配置，目录 ID 才是该次落点。
    if finalized and directory == parent:
        name = "根目录" if directory == "0" else ""
    audit = conn.execute(
        "SELECT status FROM organize_delete_audit WHERE provider='guangya' AND file_id=? "
        "AND created_at>=? AND status<>'blocked' ORDER BY id DESC LIMIT 1",
        (directory, row["created_at"]),
    ).fetchone()
    delete_status = audit["status"] if audit else ""
    state = "recorded"
    if delete_status == "success":
        state = "recycled"
    elif delete_status in _UNCONFIRMED_DELETE_STATES or (isolated and cleanup == "failed"):
        state = "unverified"
    elif isolated and cleanup == "completed":
        state = "closed"
    elif isolated and cleanup in {"retained", "skipped"}:
        state = "retained"
    result = {"kind": kind, "directory_id": directory, "state": state}
    if name:
        result["directory_name"] = name
    elif state in {"closed", "recycled"}:
        result["historical_path_note"] = "原接收目录编号已记录，原目录名未留存，无法从这份回执还原当时的完整路径。"
    if parent and (isolated or finalized) and parent != directory:
        result["parent_id"] = parent
        if isolated and name:
            # 投递时保存的是「接收目录名 / 隔离目录名」。它能定位这两级，完整祖先路径仍需核验。
            parent_label, _, leaf = (row["gy_target_name"] or "").rpartition(" / ")
            if leaf == name and (parent_name := _destination_name(parent_label)):
                result["parent_name"] = parent_name
    return result


def _download_destination_evidence(conn: sqlite3.Connection, stored: dict) -> dict:
    row, request = stored["record"], stored["request"]
    evidence: dict[str, Any] = {"freshness": "persisted_snapshot"}
    if "qb" in request["targets"]:
        status, save_path = request["qb_status"], row["qb_save_path"]
        qb = {"status": status, "historical": status in {"cancelled", "resubmitted"},
              "task_bound": bool(row["qb_task_id"]), "local_import_status": request["local_import_status"],
              "save_path_policy": "unknown" if save_path is None else ("configured" if save_path else "qb_default")}
        if re.fullmatch(r"[0-9a-fA-F]{40}", row["qb_task_id"] or ""):
            qb["task_hash"] = row["qb_task_id"]
        content_path = row["qb_content_path"] or ""
        if (status == "completed" and re.match(r"^(?:[a-zA-Z]:[\\/]|/(?!/))", content_path)
                and not contains_sensitive_credential(content_path)):
            name = _destination_name(content_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1])
            if name:
                qb["observed_content_name"] = name
        evidence["qb"] = qb
    if "guangya" not in request["targets"]:
        return evidence

    receiving = _guangya_receiving(conn, row)
    historical = "resubmitted" in (row["gy_status"], row["organize_status"]) or row["gy_status"] == "cancelled"
    check_directory_ids = []
    if (not historical and receiving and receiving["state"] in {"recorded", "retained"}
            and receiving["directory_id"] != "0"):
        check_directory_ids.append(receiving["directory_id"])
    evidence["guangya"] = {
        "status": request["guangya_status"], "organize_status": _status(row["organize_status"]),
        "historical": historical, "receiving": receiving,
        "staging_cleanup_status": _status(row["gy_staging_cleanup_status"]),
        "check_directory_ids": check_directory_ids,
    }
    if requested := _cloud_id(row["gy_requested_target_dir"]):
        evidence["guangya"]["requested_directory_id"] = requested
    return evidence


def timeline_snapshot(kind: str, identifier: int) -> dict[str, Any]:
    """按持久活动身份返回安全阶段事实；不导出原始记录、来源或投递身份。"""
    if not isinstance(kind, str) or kind not in _TABLES:
        raise ValueError("活动类型无效")
    if type(identifier) is not int or identifier < 1:
        raise ValueError("活动编号必须是正整数")
    stored = snapshot(kind, identifier, include_download_evidence=True)
    if stored is None:
        return {
            "ok": False,
            "status": "not_found",
            "message": "活动记录已被清理",
            "data": {},
            "error": "请重新查询活动，不能据旧引用推断成功。",
        }
    row = stored["record"]
    stages: list[dict] = []
    links: list[dict] = []
    gaps: list[str] = []
    if kind == "download":
        stages.append(
            _stage(
                "下载请求",
                row["status"],
                stamp=row["updated_at"],
                error=row.get("error"),
            )
        )
        latest = {}
        for log in stored["logs"]:
            latest.setdefault(log["source"], log)
        targets = ("qb", "guangya") if row["targets"] == "both" else (row["targets"],)
        for backend, field in (("qb", "qb_status"), ("guangya", "gy_status")):
            if backend not in targets:
                continue
            log = latest.get(backend) or {}
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
                    task_bound=bool(row.get("qb_task_id" if backend == "qb" else "gy_task_id")),
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
        local_import_historical = row.get("local_import_status") == "resubmitted"
        for task in stored["local_tasks"]:
            stages.append(
                _stage(
                    "关联本地任务",
                    task["status"],
                    stamp=task["updated_at"],
                    error=task.get("error"),
                    historical=local_import_historical,
                )
            )
            links.append({"kind": "local_media", "id": task["id"]})
        counts = stored["local_task_status_counts"]
        total_count = sum(counts.values())
        if total_count > len(stored["local_tasks"]):
            attention_count = sum(
                count for status, count in counts.items() if status in _FAILED
            )
            pending_count = sum(
                count
                for status, count in counts.items()
                if status not in _DONE | _FAILED
            )
            stages.append(
                _stage(
                    "关联本地任务汇总",
                    "partial_failed"
                    if attention_count
                    else ("running" if pending_count else "observed"),
                    total_count=total_count,
                    attention_count=attention_count,
                    pending_count=pending_count,
                    truncated=True,
                    historical=local_import_historical,
                )
            )
            gaps.append(
                "仅展示最近20个关联本地任务；状态汇总覆盖全部同qB任务身份记录。"
            )
        verification = stored["verification"]
        if verification:
            stages.append(
                _stage(
                    "媒体库可见性复核",
                    verification["status"],
                    stamp=verification["updated_at"],
                    result=_status(verification["result"]),
                    attempts=verification["attempts"],
                    targets=[{
                        "tmdb_id": target["tmdb_id"], "season": target["season"], "episode": target["episode"],
                        "library_name": public_text(target["library_name"], 120),
                        "result": _status(target["result"]),
                    } for target in json.loads(verification["targets_json"])],
                )
            )
        else:
            gaps.append("未建立媒体库可见性复核记录；是否已入库未知，当前记录既不能证明已入库，也不能证明未入库。")
        for run in stored["runs"]:
            stages.append(
                _stage(
                    "关联运行 · " + public_text(run["task_name"], 40),
                    run["status"],
                    stamp=run.get("finished_at") or run["started_at"],
                    error=run.get("error"),
                    historical=any(
                        row.get(prefix + "_status") == "resubmitted"
                        and row.get(prefix + "_run_id") == run["id"]
                        for prefix in ("organize", "strm")
                    ),
                )
            )
    elif kind == "organize":
        stages.append(
            _stage(
                "整理",
                row["status"],
                stamp=row.get("updated_at"),
                error=row.get("error"),
            )
        )
        for step in reversed(stored["steps"]):
            stages.append(
                _stage(
                    "文件操作 · " + public_text(step.get("action"), 40),
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
        counts = stored["item_status_counts"]
        total_count = sum(counts.values())
        attention_count = sum(
            count for status, count in counts.items() if status in _FAILED
        )
        stages.append(
            _stage(
                "成员处理",
                "partial_failed" if attention_count else "observed",
                count=min(total_count, 100),
                truncated=total_count > 100,
                total_count=total_count,
                attention_count=attention_count,
            )
        )
        gaps.append(
            "成员状态按该任务全部持久记录汇总，展示数量上限100；不代表已实时检查媒体服务器。"
        )
    # 当前日志/任务状态是权威；历史失败继续可见，但不再次发已修复的告警。
    current_stages = [stage for stage in stages if not stage.get("historical")]
    attention = [stage for stage in current_stages if stage["needs_attention"]]
    pending = [
        stage
        for stage in current_stages
        if stage["status"] not in _DONE | _FAILED | {"observed"}
        or stage.get("pending_count", 0) > 0
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
    if any(stage["status"] == "resubmitted" for stage in current_stages):
        explanation = "重提阶段已移交；请按重提回执继续核对后继请求，移交不代表实际下载或入库完成。" + explanation
    return {
        "ok": True,
        "status": overall,
        "message": explanation,
        "error": "",
        "data": {
            "title": public_text(download_display_title(row) if kind == "download" else row.get("title"), 120),
            **({key: stored[key] for key in ("request", "organize_evidence", "destination_evidence")}
               if kind == "download" else {}),
            "kind": kind,
            "stages": stages,
            "explanation": explanation,
            "gaps": gaps,
            "terminal": not pending,
            "needs_attention": bool(attention),
            "freshness": "persisted_snapshot",
            "observed_at": _stamp(_now()),
            "activities": [{"kind": kind, "id": identifier}, *links],
        },
    }
