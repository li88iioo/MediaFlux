"""日志 API：整理日志、下载日志查询与整理回退。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import StreamingResponse

from app import database as db
from app.logger import get_logger
from app.modules.organize_correction import OrganizeCorrectionService
from app.modules.organize_tasks import get_organize_manager
from app.modules.runtime_log import clear_logs, log_snapshot, read_stream_chunk
from app.web import api_error, require_api_login

logger = get_logger(__name__)
router = APIRouter(prefix="/api/logs")


@router.get("/runtime")
def runtime_logs(request: Request, lines: int = Query(default=200)):
    require_api_login(request)
    limit = max(1, min(lines, 1000))
    chunk = read_stream_chunk(0, max_bytes=4 * 1024 * 1024)
    mtime_ns, _size = log_snapshot()
    return {
        "lines": [event.line for event in chunk.events[-limit:]],
        "source": "app.log",
        "mtime_ns": mtime_ns,
        # 使用最后一条完整物理行的游标，避免初次加载时跳过尚未写完的日志行。
        "offset": chunk.offset,
        "stream_id": chunk.stream_id,
        "checkpoint": chunk.checkpoint,
        "generation": chunk.generation,
    }


@router.delete("/runtime")
def clear_runtime_logs(request: Request):
    """清空真实运行日志并返回清空后的流游标基线。"""
    require_api_login(request)
    try:
        generation, offset, stream_id, checkpoint = clear_logs()
        return {
            "success": True,
            "message": "实时日志已清空",
            "offset": offset,
            "stream_id": stream_id,
            "checkpoint": checkpoint,
            "generation": generation,
        }
    except Exception as exc:
        logger.error(f"清空实时日志失败: {exc}")
        return api_error("清空实时日志失败", 500)


@router.get("/runtime/stream")
async def runtime_log_stream(
    request: Request,
    offset: int = Query(default=0),
    stream_id: str = Query(default="", max_length=128),
    checkpoint: str = Query(default="", max_length=128),
    generation: int | None = Query(default=None, ge=0),
):
    require_api_login(request)
    start_offset = max(0, int(offset or 0))
    start_stream_id = str(stream_id or "").strip()
    start_checkpoint = str(checkpoint or "").strip()
    start_generation = generation

    async def events():
        current = start_offset
        current_stream_id = start_stream_id
        current_checkpoint = start_checkpoint
        current_generation = start_generation
        last_heartbeat = time.monotonic()
        while not await request.is_disconnected():
            try:
                generation_before_read = current_generation
                chunk = await asyncio.to_thread(
                    read_stream_chunk,
                    current,
                    expected_stream_id=current_stream_id,
                    expected_checkpoint=current_checkpoint,
                    expected_generation=current_generation,
                )
                if (
                    generation_before_read is not None
                    and chunk.generation != generation_before_read
                ):
                    current = chunk.offset
                    current_stream_id = chunk.stream_id
                    current_checkpoint = chunk.checkpoint
                    current_generation = chunk.generation
                last_delivered_offset = chunk.reset_offset
                if chunk.reset_reason:
                    reset_payload = {
                        "reason": chunk.reset_reason,
                        "offset": chunk.reset_offset,
                        "stream_id": chunk.stream_id,
                        "checkpoint": chunk.reset_checkpoint,
                        "generation": chunk.generation,
                    }
                    if chunk.reset_reason == "line_truncated" and chunk.events:
                        reset_payload["notice"] = chunk.events[0].line
                    payload = json.dumps(reset_payload, ensure_ascii=False)
                    yield f"event: reset\ndata: {payload}\n\n"
                for item in chunk.events:
                    if (
                        chunk.reset_reason == "line_truncated"
                        and item.offset == chunk.reset_offset
                    ):
                        continue
                    payload = json.dumps(
                        {
                            "line": item.line,
                            "offset": item.offset,
                            "stream_id": chunk.stream_id,
                            "checkpoint": item.checkpoint,
                            "generation": chunk.generation,
                        },
                        ensure_ascii=False,
                    )
                    yield f"event: log\ndata: {payload}\n\n"
                    last_delivered_offset = item.offset
                if chunk.offset != last_delivered_offset:
                    payload = json.dumps(
                        {
                            "offset": chunk.offset,
                            "stream_id": chunk.stream_id,
                            "checkpoint": chunk.checkpoint,
                            "generation": chunk.generation,
                        },
                        ensure_ascii=False,
                    )
                    yield f"event: cursor\ndata: {payload}\n\n"
                current = chunk.offset
                current_stream_id = chunk.stream_id
                current_checkpoint = chunk.checkpoint
                current_generation = chunk.generation
                now = time.monotonic()
                if now - last_heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now
            except Exception as exc:
                logger.error(f"实时日志流读取失败: {exc}")
                payload = json.dumps({"message": "实时日志读取暂时失败"}, ensure_ascii=False)
                yield f"event: error\ndata: {payload}\n\n"
            await asyncio.sleep(0.75)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/overview")
def overview(request: Request):
    require_api_login(request)
    try:
        return {
            "organize": db.count_logs_by_status("organize_log"),
            "timeline": db.count_organize_timeline_by_status(owner="admin"),
        }
    except Exception as exc:
        logger.error(f"日志概览失败: {exc}")
        return api_error(str(exc), 500)


@router.get("/organize")
def organize_logs(
    request: Request,
    status: str | None = Query(default=None),
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    require_api_login(request)
    keyword = q.strip()
    normalized_status = status or None
    total = db.count_organize_logs(status=normalized_status, keyword=keyword)
    pages = (total + page_size - 1) // page_size if total else 0
    rows = db.list_organize_logs(
        status=normalized_status,
        keyword=keyword,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    items = [{
        "id": row["id"],
        "source": row["source"],
        "original_path": row["original_path"],
        "original_name": row["original_name"] or "",
        "new_path": row["new_path"],
        "current_name": row["current_name"] or "",
        "file_id": row["file_id"] or "",
        "status": row["status"],
        "tmdb_id": row["tmdb_id"] or "",
        "provider": row["provider"] or "",
        "external_id": row["external_id"] or "",
        "media_type": row["media_type"] or "",
        "legacy_incomplete": bool(row["legacy_incomplete"]),
        "version": int(row["version"] or 1),
        "error": row["error"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"] or row["created_at"],
    } for row in rows]
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
    }


@router.get("/organize/timeline")
def organize_timeline(
    request: Request,
    origin: str = Query(default="all"),
    status: str = Query(default=""),
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """汇总光鸭与本地整理记录；写操作仍严格保留在各自执行链路。"""
    require_api_login(request)
    try:
        total = db.count_organize_timeline(
            owner="admin", origin=origin, status=status, keyword=q.strip(),
        )
        pages = (total + page_size - 1) // page_size if total else 0
        rows = db.list_organize_timeline(
            owner="admin", origin=origin, status=status, keyword=q.strip(),
            limit=page_size, offset=(page - 1) * page_size,
        )
    except ValueError as exc:
        return api_error(str(exc), 400)

    items = []
    for row in rows:
        row_origin = row["origin"]
        original_path = row["original_path"] or ""
        original_name = row["original_name"] or ""
        if row_origin == "local" and not original_name:
            original_name = Path(original_path).name
        items.append({
            "record_key": f"{row_origin}:{row['id']}",
            "id": row["id"],
            "origin": row_origin,
            "origin_label": "光鸭" if row_origin == "guangya" else "本地",
            "source_label": row["source_label"] or ("光鸭云盘" if row_origin == "guangya" else "本地媒体"),
            "raw_status": row["raw_status"],
            "status": row["status"],
            "original_path": original_path,
            "original_name": original_name,
            "new_path": row["new_path"] or "",
            "current_name": row["current_name"] or "",
            "title": row["title"] or "",
            "tmdb_id": row["tmdb_id"] or "",
            "provider": row["provider"] or "",
            "external_id": row["external_id"] or "",
            "media_type": row["media_type"] or "",
            "trigger": row["trigger"] or "",
            "error": row["error"] or "",
            "warning": row["warning"] or "",
            "legacy_incomplete": bool(row["legacy_incomplete"]),
            "version": int(row["version"] or 1),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"] or row["created_at"],
            "completed_at": row["completed_at"] or "",
            "actions": {
                "detail": row_origin == "guangya",
                # 待确认项尚未产生可回退的云端写操作，只允许查看详情。
                "batch": row_origin == "guangya" and row["status"] != "manual",
            },
        })
    return {
        "items": items, "page": page, "page_size": page_size,
        "total": total, "pages": pages,
    }


@router.delete("/organize")
def clear_organize_log_records(request: Request, data: dict | None = Body(default=None)):
    """清理光鸭与本地整理记录，不执行任何媒体文件删除。"""
    require_api_login(request)
    data = data or {}
    if str(data.get("confirm") or "") != "CLEAR":
        return api_error("请输入 CLEAR 确认清理整理记录", 400)
    try:
        result = db.clear_organize_logs()
        return {
            **result,
            "success": True,
            "message": "光鸭与本地整理记录已清理；未删除任何云端或本地媒体文件",
        }
    except Exception as exc:
        logger.error(f"清理整理日志失败: {exc}")
        return api_error("清理整理日志失败", 500)


@router.get("/organize/{log_id}")
def organize_log_detail(log_id: int, request: Request):
    require_api_login(request)
    try:
        return OrganizeCorrectionService().detail(log_id)
    except LookupError as exc:
        return api_error(str(exc), 404)
    except Exception as exc:
        logger.error(f"整理日志详情失败 log={log_id}: {exc}")
        return api_error("整理日志详情读取失败", 500)


@router.post("/organize/{log_id}/tmdb/search")
def organize_tmdb_search(log_id: int, request: Request,
                         data: dict | None = Body(default=None)):
    require_api_login(request)
    data = data or {}
    media_type = str(data.get("media_type") or "").strip()
    if media_type and media_type not in {"movie", "tv"}:
        return api_error("media_type 仅支持 movie 或 tv", 400)
    try:
        return {
            "candidates": OrganizeCorrectionService().search_tmdb(
                log_id, str(data.get("query") or "").strip(),
                str(data.get("year") or "").strip(), media_type,
            )
        }
    except LookupError as exc:
        return api_error(str(exc), 404)
    except ValueError as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        logger.error(f"整理日志 TMDB 搜索失败 log={log_id}: {exc}")
        return api_error(str(exc), 502)


def _optional_position(data: dict, key: str, *, minimum: int) -> int | None:
    raw = data.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    label = "季号" if key == "season" else "集号"
    if isinstance(raw, bool):
        raise ValueError(f"{label}必须是整数")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是整数")
    if value < minimum or value > 999:
        raise ValueError(f"{label}范围必须为 {minimum}-999")
    return value


@router.post("/organize/{log_id}/reorganize/preview")
def preview_reorganize(log_id: int, request: Request,
                       data: dict | None = Body(default=None)):
    require_api_login(request)
    data = data or {}
    tmdb_id = str(data.get("tmdb_id") or "").strip()
    media_type = str(data.get("media_type") or "").strip()
    if not tmdb_id or media_type not in {"movie", "tv"}:
        return api_error("请选择有效的 TMDB 候选", 400)
    try:
        return OrganizeCorrectionService().preview_reorganize(
            log_id, tmdb_id, media_type,
            str(data.get("title") or "").strip(), str(data.get("year") or "").strip(),
            _optional_position(data, "season", minimum=0),
            _optional_position(data, "episode", minimum=1),
        )
    except LookupError as exc:
        return api_error(str(exc), 404)
    except ValueError as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        logger.error(f"整理纠偏预览失败 log={log_id}: {exc}")
        return api_error(str(exc), 502)


def _operation_payload(data: dict) -> tuple[str, int]:
    token = str(data.get("operation_token") or "").strip()
    if not token:
        raise ValueError("缺少操作令牌")
    try:
        version = int(data.get("expected_version"))
    except (TypeError, ValueError):
        raise ValueError("缺少有效的日志版本")
    return token, version


def _batch_operation_payload(data: dict) -> tuple[str, list[dict]]:
    action = str(data.get("action") or "").strip()
    if action not in {"reorganize", "revert", "delete"}:
        raise ValueError("批量操作仅支持 reorganize、revert 或 delete")
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("缺少批量日志列表")
    entries: list[dict] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError("批量日志参数格式错误")
        token, version = _operation_payload(raw)
        try:
            log_id = int(raw.get("log_id"))
        except (TypeError, ValueError):
            raise ValueError("批量日志 ID 无效")
        entries.append({
            "log_id": log_id,
            "expected_version": version,
            "operation_token": token,
        })
    return action, entries


@router.post("/organize/batch")
def run_organize_batch(request: Request, data: dict | None = Body(default=None)):
    """对已选择的剧集日志执行批量改名、回退或删除。"""
    require_api_login(request)
    data = data or {}
    try:
        action, entries = _batch_operation_payload(data)
        confirm_text = str(data.get("confirm") or "")
        if action == "delete" and confirm_text != "DELETE":
            return api_error("请输入 DELETE 确认批量移入光鸭回收站", 400)
        service = OrganizeCorrectionService()
        service.validate_batch([entry["log_id"] for entry in entries], action)
        if not service.client.logged_in:
            return api_error("光鸭未登录，无法执行剧集批量操作", 503)
        labels = {
            "reorganize": "剧集批量改名",
            "revert": "剧集批量回退",
            "delete": "剧集批量移入光鸭回收站",
        }
        result = get_organize_manager().start_operation(
            labels[action], f"{len(entries)} 条剧集日志",
            lambda: service.run_batch(action, entries, confirm_text),
        )
        if not result["ok"]:
            return api_error(result["error"], 409)
        return {**result, "action": action, "count": len(entries)}
    except LookupError as exc:
        return api_error(str(exc), 404)
    except ValueError as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        logger.error(f"剧集批量操作提交失败: {exc}")
        return api_error(str(exc), 409)


@router.post("/organize/{log_id}/reorganize")
def run_reorganize(log_id: int, request: Request,
                   data: dict | None = Body(default=None)):
    require_api_login(request)
    data = data or {}
    try:
        token, version = _operation_payload(data)
        service = OrganizeCorrectionService()
        tmdb_id = str(data.get("tmdb_id") or "").strip()
        media_type = str(data.get("media_type") or "").strip()
        if not tmdb_id or media_type not in {"movie", "tv"}:
            return api_error("请选择有效的 TMDB 候选", 400)
        season = _optional_position(data, "season", minimum=0)
        episode = _optional_position(data, "episode", minimum=1)
        service.preview_reorganize(
            log_id, tmdb_id, media_type,
            str(data.get("title") or "").strip(), str(data.get("year") or "").strip(),
            season, episode,
        )
        if not service.client.logged_in:
            return api_error("光鸭未登录，无法重新整理", 503)
        result = get_organize_manager().start_operation(
            "重新整理", f"日志 {log_id}",
            lambda: service.reorganize(
                log_id, token, version, tmdb_id, media_type,
                str(data.get("title") or "").strip(), str(data.get("year") or "").strip(),
                season, episode,
            ),
        )
        if not result["ok"]:
            return api_error(result["error"], 409)
        return {**result, "operation_token": token}
    except LookupError as exc:
        return api_error(str(exc), 404)
    except ValueError as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        logger.error(f"重新整理提交失败 log={log_id}: {exc}")
        return api_error(str(exc), 409)


@router.post("/organize/{log_id}/return-to-source")
def return_to_source(log_id: int, request: Request,
                     data: dict | None = Body(default=None)):
    require_api_login(request)
    data = data or {}
    try:
        token, version = _operation_payload(data)
        service = OrganizeCorrectionService()
        detail = service.detail(log_id)
        if not detail["allowed_actions"]["return_to_source"]:
            return api_error(detail.get("safety_notice") or "当前状态不能送回源目录", 400)
        if not service.client.logged_in:
            return api_error("光鸭未登录，无法送回源目录", 503)
        result = get_organize_manager().start_operation(
            "送回源目录", f"日志 {log_id}",
            lambda: service.return_to_source(log_id, token, version),
        )
        if not result["ok"]:
            return api_error(result["error"], 409)
        return {**result, "operation_token": token}
    except LookupError as exc:
        return api_error(str(exc), 404)
    except ValueError as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        logger.error(f"送回源目录提交失败 log={log_id}: {exc}")
        return api_error(str(exc), 409)


@router.post("/organize/{log_id}/revert")
def revert_organize(log_id: int, request: Request,
                    data: dict | None = Body(default=None)):
    """按持久化操作步骤回退最近一次重整；不再从路径猜测原文件名。"""
    require_api_login(request)
    data = data or {}
    try:
        token, version = _operation_payload(data)
        service = OrganizeCorrectionService()
        detail = service.detail(log_id)
        if not detail["allowed_actions"]["revert"]:
            return api_error("没有可安全回退的最近操作", 400)
        if not service.client.logged_in:
            return api_error("光鸭未登录，无法回退", 503)
        result = get_organize_manager().start_operation(
            "回退最近操作", f"日志 {log_id}",
            lambda: service.revert_latest(log_id, token, version),
        )
        if not result["ok"]:
            return api_error(result["error"], 409)
        return {**result, "operation_token": token}
    except LookupError as exc:
        return api_error(str(exc), 404)
    except ValueError as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        logger.error(f"回退整理提交失败 log={log_id}: {exc}")
        return api_error(str(exc), 409)


@router.delete("/organize/{log_id}")
def delete_organize_group(log_id: int, request: Request,
                          data: dict | None = Body(default=None)):
    require_api_login(request)
    data = data or {}
    try:
        token, version = _operation_payload(data)
        service = OrganizeCorrectionService()
        if str(data.get("confirm") or "") != "DELETE":
            return api_error("请输入 DELETE 确认移入光鸭回收站", 400)
        detail = service.detail(log_id)
        if not detail["allowed_actions"]["delete"]:
            return api_error(detail.get("safety_notice") or "当前状态不能删除", 400)
        if not service.client.logged_in:
            return api_error("光鸭未登录，无法删除", 503)
        result = get_organize_manager().start_operation(
            "删除媒体组", f"日志 {log_id}",
            lambda: service.delete_group(
                log_id, token, version, str(data.get("confirm") or "")
            ),
        )
        if not result["ok"]:
            return api_error(result["error"], 409)
        return {**result, "operation_token": token}
    except LookupError as exc:
        return api_error(str(exc), 404)
    except ValueError as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        logger.error(f"删除媒体组提交失败 log={log_id}: {exc}")
        return api_error(str(exc), 409)
