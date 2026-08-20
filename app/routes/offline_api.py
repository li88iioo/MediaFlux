"""离线转存配置 API：规则状态与安全预览。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from app.clients.guangya import GuangYaClient
from app.modules.offline import (
    OfflinePreviewStore,
    OfflineRules,
    preview_offline_selection,
    rules_summary,
    submit_offline_selection,
    validate_preview_indexes,
)
from app.web import require_api_login


router = APIRouter(prefix="/api/offline")
_offline_preview_store = OfflinePreviewStore()


@router.get("/rules")
def get_rules(request: Request):
    require_api_login(request)
    return rules_summary()


@router.post("/preview")
def preview(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    data = data or {}
    rules_data = data.get("rules") if isinstance(data.get("rules"), dict) else None
    rules = OfflineRules.from_mapping(rules_data) if rules_data is not None else OfflineRules.from_config()
    result = preview_offline_selection(
        str(data.get("url", "")),
        title=str(data.get("title", "")),
        rules=rules,
        client=GuangYaClient(),
    )
    if result.get("allowed") and not result.get("ok"):
        return JSONResponse(result, status_code=400)
    if result.get("ok"):
        files = result.get("files") if isinstance(result.get("files"), list) else []
        preview_id = _offline_preview_store.create(
            url=str(data.get("url", "")),
            title=str(data.get("title", "")),
            rules=rules,
            target_dir_id=str(result.get("target_dir_id", "0")),
            target_dir_name=str(result.get("target_dir_name", "")),
            file_indexes=[int(item["index"]) for item in files],
            locked_indexes=[int(item["index"]) for item in files if item.get("locked")],
        )
        result["preview_id"] = preview_id
        result["preview_expires_in"] = _offline_preview_store.ttl_seconds
    return result


@router.post("/submit")
def submit(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    data = data or {}
    rules_data = data.get("rules") if isinstance(data.get("rules"), dict) else None
    rules = OfflineRules.from_mapping(rules_data) if rules_data is not None else OfflineRules.from_config()
    indexes = data.get("file_indexes", data.get("fileIndexes", []))
    if not isinstance(indexes, list):
        return JSONResponse({"error": "file_indexes 必须是数组"}, status_code=400)
    preview_id = str(data.get("preview_id", "")).strip()
    if not preview_id:
        return JSONResponse({"error": "缺少有效 preview_id，请重新解析"}, status_code=400)
    try:
        snapshot = _offline_preview_store.claim(
            preview_id,
            url=str(data.get("url", "")),
            title=str(data.get("title", "")),
            rules=rules,
        )
        indexes = validate_preview_indexes(snapshot, indexes)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    result = submit_offline_selection(
        snapshot.url,
        selected_indexes=indexes,
        title=snapshot.title,
        rules=snapshot.rules,
        client=GuangYaClient(),
        expected_target_dir_id=snapshot.target_dir_id,
        expected_target_dir_name=snapshot.target_dir_name,
    )
    if result.get("decision"):
        decision = result["decision"]
        if (
            str(decision.get("target_dir_id", "")) != snapshot.target_dir_id
            or str(decision.get("target_dir_name", "")) != snapshot.target_dir_name
        ):
            return JSONResponse({"error": "预览目标已变化，请重新解析"}, status_code=409)
    if result.get("partial_success"):
        return JSONResponse(result, status_code=207)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result
