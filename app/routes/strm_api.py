"""STRM 调度 API：状态、历史、cron 校验、手动触发、启停。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body, Request

from app import config, database as db
from app.logger import get_logger
from app.modules.scheduler import STRMScheduler, get_scheduler
from app.modules.strm import retry_all_strm_failures, retry_strm_failures
from app.web import api_error, api_response, config_write_api_error, require_api_login

router = APIRouter(prefix="/api/strm")
logger = get_logger(__name__)


_MAX_DIAGNOSTIC_CLEANUP_IDS = 100
_CLEANUP_CONFIRMATION = "CLEAN TEST INDEX"


@router.get("/base-url-candidates")
def base_url_candidates(request: Request):
    """返回 STRM 播放地址候选，不探测外部目标也不修改配置。"""
    require_api_login(request)
    from app.modules.network_addresses import build_lan_url_candidates

    payload = build_lan_url_candidates(
        bind_host=config.get("WEB_HOST", "127.0.0.1"),
        port=config.flask_port(),
    )
    payload["configured"] = config.get("GY_STRM_BASE_URL", "").strip().rstrip("/")
    return api_response(payload)


@router.get("/index-diagnostics")
def index_diagnostics(request: Request):
    require_api_login(request)
    try:
        return api_response(
            db.list_strm_index_diagnostics(config.get("STRM_ROOT", ""))
        )
    except Exception:
        return api_error("STRM 索引诊断失败", 500)


@router.post("/index-diagnostics/cleanup")
def cleanup_index_diagnostics(request: Request, data: dict = Body(...)):
    require_api_login(request)
    if set(data) != {"confirm", "ids"}:
        return api_error("请求必须仅包含 confirm 和 ids", 400)
    if data.get("confirm") != _CLEANUP_CONFIRMATION:
        return api_error("确认文本无效", 400)
    ids = data.get("ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= _MAX_DIAGNOSTIC_CLEANUP_IDS:
        return api_error("ids 必须是包含 1-100 项的数组", 400)
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0
        for item in ids
    ):
        return api_error("ids 只能包含正整数", 400)
    normalized_ids = list(dict.fromkeys(ids))
    try:
        deleted = db.delete_confirmed_test_strm_indexes(normalized_ids)
        diagnostics = db.list_strm_index_diagnostics(config.get("STRM_ROOT", ""))
    except ValueError:
        return api_error("请求包含非确认测试索引，未执行清理", 409)
    except Exception:
        return api_error("STRM 测试索引清理失败", 500)
    return api_response({"deleted": deleted, "diagnostics": diagnostics})


@router.get("/failures")
def failures(
    request: Request,
    source_id: str = "",
    action: str = "",
    status: str = "open",
    page: int = 1,
    page_size: int = 120,
):
    require_api_login(request)
    if action not in {"", "generate", "metadata"}:
        return api_error("action 只能是 generate 或 metadata", 400)
    if status not in {"open", "resolved", "all"}:
        return api_error("status 只能是 open、resolved 或 all", 400)
    current_page = max(1, int(page or 1))
    size = max(1, min(int(page_size or 120), 500))
    offset = (current_page - 1) * size

    total = db.count_strm_failures(
        status=status, source_id=source_id.strip(), action=action
    )
    total_pages = max(1, (total + size - 1) // size) if total > 0 else 1

    rows = db.list_strm_failures(
        status=status,
        source_id=source_id.strip(),
        action=action,
        limit=size,
        offset=offset,
    )
    return api_response({
        "items": [dict(row) for row in rows],
        "summary": db.summarize_strm_failures(),
        "pagination": {
            "page": current_page,
            "page_size": size,
            "total": total,
            "total_pages": total_pages,
        },
    })


@router.post("/failures/retry")
def retry_failures(request: Request, data: dict = Body(...)):
    require_api_login(request)
    allowed = {"ids", "all", "source_id", "action"}
    if not set(data).issubset(allowed):
        return api_error("请求包含不支持的字段", 400)
    action = str(data.get("action") or "")
    source_id = str(data.get("source_id") or "").strip()
    if action not in {"", "generate", "metadata"}:
        return api_error("action 只能是 generate 或 metadata", 400)

    retry_all = data.get("all") is True
    if retry_all:
        if "ids" in data:
            return api_error("all 与 ids 不能同时提交", 400)
        result = retry_all_strm_failures(source_id, action, "web")
    else:
        ids = data.get("ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            return api_error("ids 必须是包含 1-100 项的数组", 400)
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in ids
        ):
            return api_error("ids 只能包含正整数", 400)
        result = retry_strm_failures(list(dict.fromkeys(ids)), "web")

    if not result.get("ok", True):
        error = str(result.get("error") or "STRM 重试失败")
        return api_error(error, 409 if "正在运行" in error else 400)
    result["remaining"] = db.count_strm_failures(
        status="open", source_id=source_id, action=action
    )
    result["failures"] = {
        "items": [dict(row) for row in db.list_strm_failures(status="open", limit=500)],
        "summary": db.summarize_strm_failures(),
    }
    return api_response(result)


@router.post("/failures/clear")
def clear_failures(request: Request, data: dict = Body(...)):
    require_api_login(request)
    allowed = {"ids", "all", "source_id", "action", "status"}
    if not set(data).issubset(allowed):
        return api_error("请求包含不支持的字段", 400)
    action = str(data.get("action") or "")
    source_id = str(data.get("source_id") or "").strip()
    status = str(data.get("status") or "")
    if action not in {"", "generate", "metadata"}:
        return api_error("action 只能是 generate 或 metadata", 400)
    if status not in {"", "open", "resolved", "all"}:
        return api_error("status 只能是 open、resolved 或 all", 400)

    clear_all = data.get("all") is True
    ids = data.get("ids")
    if clear_all:
        if ids is not None:
            return api_error("all 与 ids 不能同时提交", 400)
        deleted = db.delete_strm_failures(
            all_items=True, source_id=source_id, action=action, status=status
        )
    else:
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
            return api_error("ids 必须是包含 1-500 项的数组", 400)
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in ids
        ):
            return api_error("ids 只能包含正整数", 400)
        deleted = db.delete_strm_failures(ids=ids)

    return api_response({
        "ok": True,
        "deleted": deleted,
        "summary": db.summarize_strm_failures(),
    })


@router.get("/schedule")
def schedule_status(request: Request):
    require_api_login(request)
    return api_response(get_scheduler().status())


@router.put("/schedule")
def update_schedule(request: Request, data: dict = Body(...)):
    require_api_login(request)
    cron_expr = str(data.get("cron", "0 4 * * *")).strip()
    if not STRMScheduler.validate_cron(cron_expr):
        return api_error("cron 表达式无效，需使用 5 段格式：分 时 日 月 周", 400)
    updates = {
        "STRM_SCHEDULE_ENABLED": "1" if data.get("enabled") else "0",
        "STRM_SCHEDULE_CRON": cron_expr,
    }
    if "notify_enabled" in data:
        updates["STRM_NOTIFY_ENABLED"] = "1" if data["notify_enabled"] else "0"
    try:
        config.set_and_save(updates)
    except (config.AtomicPublishError, OSError) as exc:
        return config_write_api_error(
            exc,
            logger=logger,
            operation="save_strm_schedule",
        )
    get_scheduler().reload()
    return api_response({"success": True, "status": get_scheduler().status()})


@router.post("/schedule/validate")
def validate_schedule(request: Request, data: dict = Body(...)):
    require_api_login(request)
    expr = str(data.get("cron", "")).strip()
    valid = STRMScheduler.validate_cron(expr)
    next_run = ""
    if valid:
        next_run = STRMScheduler._calculate_next(expr).strftime("%Y-%m-%d %H:%M:%S")
    return api_response({"valid": valid, "next_run": next_run})


@router.post("/run")
def run_now(request: Request):
    require_api_login(request)
    config_error = get_scheduler().validate_config(auto_only=False)
    if config_error:
        return api_error(config_error, 400)
    # 保留历史 API 的完整同步语义，避免既有自动化升级后静默变成 no-op。
    result = get_scheduler().trigger("manual", sync_mode="full", force_full=True)
    return api_response(result, 202 if result.get("ok") else 409)


@router.post("/run/fast")
def run_fast_now(request: Request):
    require_api_login(request)
    config_error = get_scheduler().validate_config(auto_only=False)
    if config_error:
        return api_error(config_error, 400)
    result = get_scheduler().trigger("manual", sync_mode="fast")
    return api_response(result, 202 if result.get("ok") else 409)


@router.post("/run/full")
def run_full_now(request: Request):
    require_api_login(request)
    config_error = get_scheduler().validate_config(auto_only=False)
    if config_error:
        return api_error(config_error, 400)
    result = get_scheduler().trigger("manual", sync_mode="full", force_full=True)
    return api_response(result, 202 if result.get("ok") else 409)


@router.get("/runs")
def runs(request: Request):
    require_api_login(request)
    rows = db.list_task_runs("strm_sync", limit=30)
    result = []
    for row in rows:
        payload = {}
        if row["result"]:
            try:
                payload = json.loads(row["result"])
            except (ValueError, TypeError):
                payload = {"raw": row["result"]}
        result.append({
            "id": row["id"],
            "trigger_type": row["trigger_type"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "result": payload,
            "error": row["error"] or "",
        })
    return api_response(result)
