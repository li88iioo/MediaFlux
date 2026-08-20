"""本地识别知识库管理 API。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Path, Query, Request

from app.modules import recognition_knowledge
from app.web import api_error, api_response, require_api_login

router = APIRouter(prefix="/api/tools/recognition-knowledge")


@router.get("")
def list_recognition_knowledge_api(
    request: Request,
    q: str = Query(default="", max_length=160),
    knowledge_type: str = Query(default="", max_length=32),
    limit: int = Query(default=300, ge=1, le=500),
):
    require_api_login(request)
    try:
        return api_response(
            recognition_knowledge.list_entries(
                keyword=q, knowledge_type=knowledge_type, limit=limit
            )
        )
    except ValueError as exc:
        return api_error(str(exc), 400)


@router.post("")
def create_recognition_knowledge_api(
    request: Request, data: dict | None = Body(default=None),
):
    require_api_login(request)
    try:
        payload = dict(data or {})
        payload["source"] = "user"
        payload.pop("knowledge_key", None)
        payload.pop("evidence", None)
        return api_response(recognition_knowledge.create_entry(payload), 201)
    except ValueError as exc:
        return api_error(str(exc), 400)


@router.put("/{entry_id}")
def update_recognition_knowledge_api(
    request: Request,
    entry_id: int = Path(..., ge=1),
    data: dict | None = Body(default=None),
):
    require_api_login(request)
    try:
        payload = dict(data or {})
        for protected in ("source", "knowledge_key", "evidence"):
            payload.pop(protected, None)
        return api_response(recognition_knowledge.update_entry(entry_id, payload))
    except ValueError as exc:
        return api_error(str(exc), 404 if "不存在" in str(exc) else 400)


@router.delete("/{entry_id}")
def delete_recognition_knowledge_api(
    request: Request, entry_id: int = Path(..., ge=1),
):
    require_api_login(request)
    try:
        if not recognition_knowledge.delete_entry(entry_id):
            return api_error("识别知识不存在", 404)
        return api_response({"success": True})
    except ValueError as exc:
        return api_error(str(exc), 400)
