"""活动关联与任务关注：只认持久化请求/运行标识，不凭标题拼接跨域时间线。"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from app import database as db
from app.sensitive_data import redact_sensitive_text
from app.agent.public_safety import sanitize_public_text
from app.modules.media_automation_rules import notification_route_settings
from app.repositories import media_automation_rules as rules
from app.repositories.download_requests import download_display_title, download_request_public_summary
from app.repositories.agent_jobs import agent_job_owner_digest

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


def snapshot(kind: str, identifier: int) -> dict | None:
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
                    "SELECT id,task_name,status,started_at,finished_at,error FROM task_runs "
                    "WHERE id IN (?,?) ORDER BY id",
                    (row["organize_run_id"], row["strm_run_id"]),
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


def timeline_snapshot(kind: str, identifier: int) -> dict[str, Any]:
    """按持久活动身份返回安全阶段事实；不导出原始记录、来源或投递身份。"""
    if not isinstance(kind, str) or kind not in _TABLES:
        raise ValueError("活动类型无效")
    if type(identifier) is not int or identifier < 1:
        raise ValueError("活动编号必须是正整数")
    stored = snapshot(kind, identifier)
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
            gaps.append("未建立媒体库可见性复核记录；下载或 STRM 完成不等于已经入库。")
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
            **({"request": download_request_public_summary(row)} if kind == "download" else {}),
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
