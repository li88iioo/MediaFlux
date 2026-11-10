"""RSS API 路由：订阅项管理、刷新、条目预览、手动下载。"""
from __future__ import annotations

from typing import Annotated
from fastapi import APIRouter, Body, Query, Request

from app.repositories import rss as rss_repo
from app.logger import get_logger
from app.modules.rss import RSSEngine, rss_entry_delivery
from app.modules.rss_subscriptions import RSSSubscriptionError, apply_change
from app.web import api_error, api_response, require_api_login

logger = get_logger(__name__)
router = APIRouter(prefix="/api/rss")


# ===== 统计 =====
@router.get("/stats")
def rss_stats(request: Request):
    require_api_login(request)
    return api_response(rss_repo.get_rss_stats())


# ===== 订阅项 =====
@router.get("/subscriptions")
def list_subs(request: Request):
    require_api_login(request)
    rows = rss_repo.list_rss_subscriptions()
    return api_response([{
        "id": r["id"],
        "name": r["name"],
        "enabled": bool(r["enabled"]),
        "urls": r["urls"],
        "parser": r["parser"],
        "exclude_keywords": r["exclude_keywords"],
        "action": r["action"],
        "refresh_cron": r["refresh_cron"],
        "refresh_interval_minutes": int(r["refresh_interval_minutes"] or 0),
        "last_refreshed_at": r["last_refreshed_at"],
        "download_method": r["download_method"] or "",
        "qb_save_path": r["qb_save_path"] or "",
        "gy_target_dir": r["gy_target_dir"] or "",
        "gy_target_dir_name": r["gy_target_dir_name"] or "",
        "media_tmdb_id": r["media_tmdb_id"] or "",
        "media_default_season": int(r["media_default_season"] if r["media_default_season"] is not None else 1),
        "skip_existing_episodes": bool(r["skip_existing_episodes"]),
        "updated_at": r["updated_at"],
    } for r in rows])


def _subscription_response(result: dict):
    if not result["ok"]:
        return api_error(result.get("error") or result["summary"], 409 if result["status"] == "conflict" else 503)
    data = {"success": True}
    if result["data"]["operation"] == "create":
        data["id"] = result["data"]["subscription_id"]
    return api_response(data)


@router.post("/subscriptions")
def create_sub(request: Request, data: dict = Body(...)):
    require_api_login(request)
    try:
        result = apply_change("create", data)
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)
    return _subscription_response(result)


@router.put("/subscriptions/{sid}")
def update_sub(sid: int, request: Request, data: dict = Body(...)):
    require_api_login(request)
    current = rss_repo.get_rss_subscription(sid)
    if current is None:
        return api_error("订阅项不存在", 404)
    try:
        if "subscription_id" in data:
            raise ValueError("订阅配置包含未支持字段")
        result = apply_change("update", {**data, "subscription_id": sid})
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)
    return _subscription_response(result)


@router.delete("/subscriptions/{sid}")
def delete_sub(sid: int, request: Request):
    require_api_login(request)
    if rss_repo.get_rss_subscription(sid) is not None:
        try:
            return _subscription_response(apply_change("delete", {"subscription_id": sid}))
        except RSSSubscriptionError as exc:
            return api_error(str(exc), 409 if exc.code == "busy" else 400)
    return api_response({"success": True})


@router.post("/subscriptions/{sid}/refresh")
def refresh_sub(sid: int, request: Request):
    require_api_login(request)
    try:
        engine = RSSEngine()
        result = engine.refresh(sid)
        if result.get("busy"):
            return api_response(result, 409)
        if result.get("error"):
            return api_response(result, 400)
        return api_response({"success": True, "stats": result})
    except Exception as exc:
        logger.error("RSS 刷新失败 sub#%s type=%s", sid, type(exc).__name__)
        return api_error("RSS 刷新失败，请稍后重试", 500)


@router.post("/subscriptions/{sid}/auto")
def auto_download_sub(sid: int, request: Request):
    """刷新并自动下载所有 pending 条目。"""
    require_api_login(request)
    try:
        engine = RSSEngine()
        result = engine.auto_download(sid)
        if result.get("busy"):
            return api_response(result, 409)
        if result.get("error"):
            return api_response(result, 400)
        return api_response({"success": True, "result": result})
    except Exception as exc:
        logger.error("RSS 自动下载失败 sub#%s type=%s", sid, type(exc).__name__)
        return api_error("RSS 自动下载失败，请稍后重试", 500)


# ===== 条目 =====
@router.get("/entries")
def list_entries(
    request: Request,
    subscription_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str = Query(default=""),
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=300)] = 100,
):
    require_api_login(request)
    try:
        sid = int(subscription_id) if subscription_id else None
    except (TypeError, ValueError):
        return api_error("订阅 ID 无效", 400)
    status = status or None
    page = rss_repo.list_rss_entries(
        sub_id=sid,
        status=status,
        keyword=q.strip(),
        limit=limit,
        offset=offset,
        order="unprocessed_first",
    )
    return api_response({"items": [{
        "id": r["id"],
        "subscription_id": r["rss_item_id"],
        "sub_name": r["sub_name"],
        "title": r["title"],
        "status": r["status"],
        "processed": bool(r["processed"]),
        "delivery": rss_entry_delivery(r),
        "submitted_at": r["submitted_at"],
        "processed_at": r["processed_at"],
        "pub_date": r["pub_date"],
        "media_season": r["media_season"],
        "media_episode": r["media_episode"],
        "skip_reason": r["skip_reason"] or "",
        "created_at": r["created_at"],
    } for r in page.items], "total": page.total, "offset": page.offset, "limit": page.limit,
        "has_more": page.offset + len(page.items) < page.total})


@router.post("/entries/batch-download")
def batch_download_entries(request: Request, data: dict = Body(...)):
    require_api_login(request)
    raw_ids = data.get("entry_ids") or []
    if not isinstance(raw_ids, list):
        return api_error("entry_ids 必须是数组", 400)
    try:
        ids = list(dict.fromkeys(int(item) for item in raw_ids))
    except (TypeError, ValueError):
        return api_error("entry_ids 包含无效值", 400)
    if not ids or len(ids) > 20:
        return api_error("每次请选择 1 至 20 个条目", 400)
    result = RSSEngine().download_many(ids)
    return api_response({"success": True, "result": result})


@router.post("/entries/mark")
def mark_entries(request: Request, data: dict = Body(...)):
    require_api_login(request)
    raw_ids = data.get("entry_ids") or []
    if not isinstance(raw_ids, list):
        return api_error("entry_ids 必须是数组", 400)
    try:
        ids = list(dict.fromkeys(int(item) for item in raw_ids))
    except (TypeError, ValueError):
        return api_error("entry_ids 包含无效值", 400)
    if not ids or len(ids) > 300:
        return api_error("每次请选择 1 至 300 个条目", 400)
    processed = bool(data.get("processed"))
    updated = rss_repo.update_rss_entries_processed(ids, processed)
    return api_response({"success": True, "updated": updated, "processed": processed})


@router.post("/entries/{eid}/download")
def download_entry(eid: int, request: Request):
    require_api_login(request)
    try:
        engine = RSSEngine()
        result = engine.download(eid)
        if result.get("error"):
            return api_response(result, 400)
        return api_response({"success": True, "result": result})
    except Exception as e:
        logger.error("RSS 下载失败 entry=%s type=%s", eid, type(e).__name__)
        return api_error(str(e), 500)
