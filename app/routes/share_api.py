"""光鸭分享链接解析预览与选择转存 API。"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from app import database as db
from app.clients.guangya import GuangYaClient
from app.logger import get_logger
from app.modules.share_transfer import (
    create_share_request,
    get_share_transfer_store,
    inspect_share_for_transfer,
)
from app.web import require_api_login


router = APIRouter(prefix="/api/share")
logger = get_logger(__name__)


def _web_owner(request: Request) -> str:
    owner_id = str(request.session.get("share_transfer_owner") or "").strip()
    if not owner_id:
        owner_id = secrets.token_urlsafe(18)
        request.session["share_transfer_owner"] = owner_id
    return f"web:{owner_id}"


def _remember_share_request(request: Request, request_id: object) -> None:
    try:
        normalized = int(request_id)
    except (TypeError, ValueError):
        return
    remembered = [
        int(item) for item in list(request.session.get("share_transfer_requests") or [])
        if str(item).isdigit()
    ]
    request.session["share_transfer_requests"] = list(dict.fromkeys([*remembered, normalized]))[-20:]


def _preview_error(exc: Exception) -> JSONResponse:
    message = str(exc or "")
    status = 410 if "过期" in message or "无效" in message or "已使用" in message else 400
    if status == 410:
        message = "预览已过期或无效，请重新解析分享链接"
    return JSONResponse({"error": message or "分享预览无效"}, status_code=status)


@router.post("/inspect")
def inspect_share(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    data = data or {}
    share_url = str(data.get("url", "")).strip()
    if not share_url:
        return JSONResponse({"error": "请输入光鸭分享链接"}, status_code=400)
    try:
        owner = _web_owner(request)
        preview = inspect_share_for_transfer(
            share_url,
            owner,
            client=GuangYaClient(),
            store=get_share_transfer_store(),
        )
        return {
            "preview_id": preview["preview_id"],
            "share_id": preview["share_id"],
            "expires_in": preview["expires_in"],
            "count": len(preview["files"]),
            "files": preview["files"],
        }
    except ValueError:
        return JSONResponse({"error": "分享链接无效，请检查链接或提取码"}, status_code=400)
    except Exception as exc:
        # 不记录原始 URL/异常正文，避免提取码、token 或 signed URL 进入日志。
        logger.warning("分享链接解析失败 (%s)", type(exc).__name__)
        return JSONResponse({"error": "分享链接解析失败，请检查链接或提取码"}, status_code=400)


@router.post("/restore")
def restore_share(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    data = data or {}
    preview_id = str(data.get("preview_id", "")).strip()
    requested = data.get("file_ids") or []
    if not preview_id or not isinstance(requested, list):
        return JSONResponse({"error": "缺少有效预览或文件列表"}, status_code=400)
    selected = list(dict.fromkeys(str(item).strip() for item in requested if str(item).strip()))
    if not selected:
        return JSONResponse({"error": "至少选择一个文件"}, status_code=400)
    target_dir_id = str(data.get("target_dir_id", "0") or "0").strip()
    target_dir_name = str(data.get("target_dir_name", "根目录") or "根目录").strip()
    try:
        result = create_share_request(
            preview_id,
            selected,
            target_dir_id,
            _web_owner(request),
            target_name=target_dir_name,
            origin="web",
            tracker_chat_id="",
            client=GuangYaClient(),
            store=get_share_transfer_store(),
        )
    except ValueError as exc:
        return _preview_error(exc)
    except Exception as exc:
        logger.warning("分享转存请求失败 (%s)", type(exc).__name__)
        return JSONResponse({"error": "分享转存失败，请稍后查看任务状态"}, status_code=502)
    _remember_share_request(request, result.get("request_id"))
    if result.get("duplicate") and result.get("accepted"):
        return JSONResponse({
            "success": False,
            "accepted": True,
            "duplicate": True,
            "request_id": result.get("request_id"),
            "status": result.get("status"),
        }, status_code=202)
    if not result.get("success"):
        status = 503 if "未登录" in str(result.get("error") or "") else 502
        return JSONResponse({
            "error": result.get("error") or "转存失败",
            "request_id": result.get("request_id"),
            "duplicate": bool(result.get("duplicate")),
        }, status_code=status)
    return {
        "success": True,
        "count": result.get("count", 0),
        "target_dir_name": result.get("target_dir_name", target_dir_name),
        "request_id": result.get("request_id"),
        "duplicate": bool(result.get("duplicate")),
    }


@router.get("/requests/{request_id}")
def share_request_status(request_id: int, request: Request):
    require_api_login(request)
    remembered = {
        int(item) for item in list(request.session.get("share_transfer_requests") or [])
        if str(item).isdigit()
    }
    if int(request_id) not in remembered:
        return JSONResponse({"error": "转存请求不存在"}, status_code=404)
    row = db.get_download_request(int(request_id))
    if row is None or str(row["kind"] or "") != "guangya_share" or str(row["origin"] or "") != "web":
        return JSONResponse({"error": "转存请求不存在"}, status_code=404)
    status = str(row["status"] or "pending")
    success = status == "completed" and str(row["gy_status"] or "") == "completed"
    terminal = status in {"completed", "failed", "manual_review", "stopped"}
    return {
        "request_id": int(row["id"]),
        "status": status,
        "success": success,
        "terminal": terminal,
        "target_dir_name": str(row["gy_target_name"] or "根目录"),
        "error": str(row["error"] or "") if terminal and not success else "",
    }
