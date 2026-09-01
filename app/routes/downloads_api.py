"""下载管理 API：qBittorrent 实时状态与统一下载日志。"""
from __future__ import annotations

import logging
import re
import time
from fastapi import APIRouter, Body, Query, Request

from app import config
from app import database as db
from app.clients.qbittorrent import (
    QBConnectionTestError,
    QBittorrentClient,
    close_qbittorrent_client,
)
from app.logger import get_logger, log_throttled, redact_sensitive_text
from app.modules.download_dispatcher import (
    SUPPORTED_TARGETS,
    download_resubmit_capabilities,
    resubmit_download_request,
)
from app.modules.qb_control import (
    QBControlConflict,
    QBControlSafetyUnavailable,
    assert_qb_control_allowed,
    qb_control_write_lease,
)
from app.web import api_error, api_response, require_api_login

logger = get_logger(__name__)
router = APIRouter(prefix="/api/downloads")

_QB_HASH_RE = re.compile(r"^[A-Fa-f0-9]{40,64}$")
_QB_BATCH_LIMIT = 200
_DOWNLOAD_BATCH_LIMIT = 100
_QB_ACTIONS = {"pause", "resume", "delete"}


def _normalize_qb_hashes(value) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("任务 hashes 必须是数组")
    raw_hashes = value
    if len(raw_hashes) > _QB_BATCH_LIMIT:
        raise ValueError(f"单次最多操作 {_QB_BATCH_LIMIT} 个任务")
    hashes: list[str] = []
    seen: set[str] = set()
    for raw in raw_hashes:
        if not isinstance(raw, str):
            raise ValueError("任务 hash 必须是字符串")
        value = raw.strip().lower()
        if not value:
            continue
        if not _QB_HASH_RE.fullmatch(value):
            raise ValueError("任务 hash 格式无效")
        if value not in seen:
            hashes.append(value)
            seen.add(value)
    if not hashes:
        raise ValueError("至少选择一个任务")
    return hashes


def _normalize_record_ids(value, *, field: str) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是数组")
    if len(value) > _DOWNLOAD_BATCH_LIMIT:
        raise ValueError(f"单次最多操作 {_DOWNLOAD_BATCH_LIMIT} 条记录")
    record_ids: list[int] = []
    seen: set[int] = set()
    for raw in value:
        if isinstance(raw, bool):
            raise ValueError("记录 ID 格式无效")
        if isinstance(raw, int):
            record_id = raw
        elif isinstance(raw, str) and raw.strip().isdigit():
            record_id = int(raw.strip())
        else:
            raise ValueError("记录 ID 格式无效")
        if record_id <= 0:
            raise ValueError("记录 ID 格式无效")
        if record_id not in seen:
            record_ids.append(record_id)
            seen.add(record_id)
    if not record_ids:
        raise ValueError("至少选择一条记录")
    return record_ids


def _qb() -> QBittorrentClient | None:
    url = config.get("QB_URL", "").strip()
    if not url:
        return None
    return QBittorrentClient(
        url=url,
        username=config.get("QB_USERNAME"),
        password=config.get("QB_PASSWORD"),
        api_key=config.get("QB_API_KEY"),
    )


def _task_json(task) -> dict:
    return {
        "hash": task.hash,
        "name": task.name,
        "progress": task.progress,
        "state": task.state,
        "save_path": task.save_path,
        "size": task.size,
        "downloaded": task.downloaded,
        "dlspeed": task.dlspeed,
        "upspeed": task.upspeed,
        "eta": task.eta,
        "ratio": task.ratio,
        "category": task.category,
        "added_on": task.added_on,
    }


def _transfer_json(info) -> dict:
    return {
        "connection_status": info.connection_status,
        "dl_info_speed": info.dl_info_speed,
        "dl_info_data": info.dl_info_data,
        "up_info_speed": info.up_info_speed,
        "up_info_data": info.up_info_data,
        "dl_rate_limit": info.dl_rate_limit,
        "up_rate_limit": info.up_rate_limit,
        "dht_nodes": info.dht_nodes,
    }


def _attention_stages(row) -> list[dict[str, str]]:
    """把下载请求中的异常字段归一为前端可直接展示的处理阶段。"""
    stages: list[dict[str, str]] = []
    request_error = str(row["error"] or "")

    def backend_error(key: str) -> str:
        """从汇总错误中提取指定后端的原因，避免重复显示 ``guangya:``。"""
        prefix = f"{key}:"
        for part in request_error.split(";"):
            value = part.strip()
            if value.lower().startswith(prefix):
                return value[len(prefix):].strip()
        return request_error

    def append(key: str, label: str, status: object, error: object = "") -> None:
        stages.append({
            "key": key,
            "label": label,
            "status": str(status or "failed"),
            "error": str(error or "")[:500],
        })

    qb_status = str(row["qb_status"] or "")
    gy_status = str(row["gy_status"] or "")
    status = str(row["status"] or "")
    # 顶层失败通常只是具体下载后端失败的汇总；已有后端阶段时不重复展示同一错误。
    if status in {"failed", "manual_review"} and not (
        qb_status == "failed" or gy_status in {"failed", "manual_review"}
    ):
        append("request", "下载请求", status, request_error)
    if qb_status in {"failed", "manual_review"}:
        append("qb", "qB", qb_status, backend_error("qb"))
    if gy_status in {"failed", "manual_review"}:
        append("guangya", "光鸭", gy_status, backend_error("guangya"))
    local_status = str(row["local_import_status"] or "")
    if local_status == "failed":
        append("local_import", "本地入库", local_status, row["local_import_error"])
    organize_status = str(row["organize_status"] or "")
    if int(row["organize_started"] or 0) < 0 or organize_status == "failed":
        append("organize", "自动整理", organize_status or "failed", row["organize_error"])
    strm_status = str(row["strm_status"] or "")
    if strm_status == "failed":
        append("strm", "STRM 同步", strm_status, row["strm_error"])
    cleanup_status = str(row["gy_staging_cleanup_status"] or "")
    if cleanup_status in {"retained", "failed"}:
        append(
            "staging_cleanup", "暂存清理", cleanup_status,
            row["gy_staging_cleanup_error"],
        )
    return stages


def _attention_json(row) -> dict:
    return {
        "id": int(row["id"]),
        "title": str(row["title"] or "未命名下载请求"),
        "origin": str(row["origin"] or ""),
        "kind": str(row["kind"] or ""),
        "targets": str(row["targets"] or ""),
        "status": str(row["status"] or ""),
        "stages": _attention_stages(row),
        "retry_targets": download_resubmit_capabilities(row),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or row["created_at"] or ""),
    }


def _public_resubmit_result(result: dict, *, source_request_id: int, target: str) -> dict:
    succeeded = set(result.get("succeeded") or [])
    failed = set(result.get("failed") or [])
    return {
        "ok": bool(result.get("ok")),
        "duplicate": bool(result.get("duplicate")),
        "source_request_id": int(source_request_id),
        "request_id": int(result.get("request_id") or 0),
        "target": target,
        "status": str(result.get("status") or "failed"),
        "succeeded": [item for item in ("qb", "guangya") if item in succeeded],
        "failed": [item for item in ("qb", "guangya") if item in failed],
        "error": redact_sensitive_text(str(result.get("error") or ""))[:500],
    }


def _qb_test_secret(value: object, *, key: str, url: str, saved_url: str) -> str:
    raw = str(value or "").strip()
    if raw != "********":
        return raw
    if url != saved_url:
        raise ValueError("修改 qB 地址后，请重新输入密码或 API Key")
    return config.get(key, "").strip()


_QB_TEST_ERRORS = {
    "authentication": "qBittorrent 已响应，但认证失败",
    "not_qb_api": "地址可达，但不是有效的 qBittorrent WebUI",
    "rate_limited": "qBittorrent 暂时限制了请求，请稍后重试",
    "server_error": "qBittorrent 服务端暂时不可用",
    "redirect": "qBittorrent 返回了重定向，请检查 WebUI 地址",
    "invalid_response": "qBittorrent 返回了无法识别的响应",
    "timeout": "连接 qBittorrent 超时，请检查地址和网络",
    "connection": "无法连接 qBittorrent，请检查地址、端口和网络",
}


@router.post("/qb/test")
def test_qb_connection(request: Request, data: object = Body(default=None)):
    """使用当前草稿测试 qB 网络与认证，不保存配置。"""
    require_api_login(request)
    if not isinstance(data, dict):
        return api_error("测试参数必须是 JSON 对象", 400)
    allowed = {"url", "username", "password", "api_key"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        return api_error(f"包含不允许的测试参数: {', '.join(unknown[:3])}", 400)

    from app.modules.media_proxy import validate_upstream_url

    try:
        url = validate_upstream_url(str(data.get("url") or ""))
    except (TypeError, ValueError):
        return api_error("请输入安全有效的 qBittorrent HTTP(S) 地址", 400)
    try:
        saved_url = validate_upstream_url(config.get("QB_URL", ""))
    except (TypeError, ValueError):
        saved_url = ""
    try:
        password = _qb_test_secret(
            data.get("password"), key="QB_PASSWORD", url=url, saved_url=saved_url,
        )
        api_key = _qb_test_secret(
            data.get("api_key"), key="QB_API_KEY", url=url, saved_url=saved_url,
        )
    except ValueError as exc:
        return api_error(str(exc), 400)
    username = str(data.get("username") or "").strip()
    if not api_key and (not username or not password):
        return api_error("请填写 API Key，或同时填写用户名和密码", 400)

    client = QBittorrentClient(
        url=url, username=username, password=password, api_key=api_key, timeout=8,
    )
    started = time.perf_counter()
    try:
        versions = client.test_connection()
    except QBConnectionTestError as exc:
        return api_error(_QB_TEST_ERRORS.get(exc.code, _QB_TEST_ERRORS["connection"]), 502)
    finally:
        close_qbittorrent_client(client)
    latency_ms = max(1, round((time.perf_counter() - started) * 1000))
    return api_response({
        "success": True,
        "auth_mode": versions["auth_mode"],
        "app_version": versions["app"],
        "webapi_version": versions["webapi"],
        "latency_ms": latency_ms,
    })


@router.get("/overview")
def overview(request: Request):
    require_api_login(request)
    result = {
        "qb": {
            "configured": False,
            "online": False,
            "tasks": [],
            "transfer": None,
            "error_code": "not_configured",
            "error": "未连接到 qBittorrent",
        },
    }
    client = None
    try:
        client = _qb()
        if client is not None:
            result["qb"] = {
                "configured": True,
                "online": True,
                "tasks": [_task_json(t) for t in client.list_torrents()],
                "transfer": _transfer_json(client.get_transfer_info()),
                "version": client.get_version(),
                "error_code": "",
                "error": "",
            }
    except Exception as exc:
        log_throttled(
            logger, logging.WARNING, f"downloads-page-qb:{type(exc).__name__}",
            "下载页 qB 数据读取失败 type=%s", type(exc).__name__,
        )
        result["qb"].update({
            "configured": True,
            "error_code": "connection_failed",
            "error": "连接失败，请检查地址、认证信息和网络",
        })
    finally:
        close_qbittorrent_client(client)
    return api_response(result)


@router.get("/logs")
def logs(
    request: Request,
    source: str | None = Query(default=None),
    status: str | None = Query(default=None),
    keyword: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    require_api_login(request)
    return api_response(_logs(
        source=source or None,
        status=status or None,
        keyword=keyword.strip(),
        page=page,
        page_size=page_size,
    ))


@router.post("/logs/batch/clear")
def clear_logs_batch(request: Request, data: dict | None = Body(default=None)):
    """删除选中的下载日志；不会停止下载任务或删除实际文件。"""
    require_api_login(request)
    if not isinstance(data, dict):
        return api_error("请求数据必须是 JSON 对象", 400)
    try:
        log_ids = _normalize_record_ids(data.get("log_ids"), field="log_ids")
    except ValueError as exc:
        return api_error(str(exc), 400)
    deleted_ids = db.delete_download_logs(log_ids)
    return api_response({
        "ok": True,
        "requested": len(log_ids),
        "deleted": len(deleted_ids),
        "missing": len(log_ids) - len(deleted_ids),
    })


@router.get("/issues")
def issues(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """返回与看板“下载待处理”相同口径的异常请求。"""
    require_api_login(request)
    total = db.count_download_requests_requiring_attention()
    pages = (total + page_size - 1) // page_size if total else 0
    effective_page = min(page, pages) if pages else 1
    rows = db.list_download_requests_requiring_attention(
        limit=page_size,
        offset=(effective_page - 1) * page_size,
    )
    return api_response({
        "items": [_attention_json(row) for row in rows],
        "page": effective_page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
    })


@router.post("/issues/batch/resubmit")
def resubmit_issues_batch(request: Request, data: dict | None = Body(default=None)):
    """逐条重新提交选中的待处理请求，并返回可审计的批量结果。"""
    require_api_login(request)
    if not isinstance(data, dict):
        return api_error("请求数据必须是 JSON 对象", 400)
    target = str(data.get("target") or "").strip().lower()
    if target not in SUPPORTED_TARGETS:
        return api_error("下载目标无效", 400)
    try:
        request_ids = _normalize_record_ids(data.get("request_ids"), field="request_ids")
    except ValueError as exc:
        return api_error(str(exc), 400)

    items: list[dict] = []
    counts = {"succeeded": 0, "partial": 0, "failed": 0, "skipped": 0}
    for request_id in request_ids:
        row = db.get_download_request(request_id)
        if not row:
            counts["skipped"] += 1
            items.append({
                "source_request_id": request_id,
                "outcome": "skipped",
                "error": "待处理请求不存在",
            })
            continue
        if not _attention_stages(row):
            counts["skipped"] += 1
            items.append({
                "source_request_id": request_id,
                "outcome": "skipped",
                "error": "该请求已经处理，无需重复提交",
            })
            continue
        capability = download_resubmit_capabilities(row).get(target) or {}
        if not capability.get("enabled"):
            counts["skipped"] += 1
            items.append({
                "source_request_id": request_id,
                "outcome": "skipped",
                "error": redact_sensitive_text(
                    str(capability.get("reason") or "当前目标不可重新提交")
                )[:500],
            })
            continue
        try:
            result = resubmit_download_request(request_id, target)
        except Exception as exc:
            log_throttled(
                logger,
                logging.WARNING,
                f"download-batch-resubmit:{target}:{type(exc).__name__}",
                "批量重新提交下载请求存在失败 target=%s type=%s",
                target,
                type(exc).__name__,
            )
            counts["failed"] += 1
            items.append({
                "source_request_id": request_id,
                "outcome": "failed",
                "error": "重新提交失败，请核对下载服务状态",
            })
            continue

        public_result = _public_resubmit_result(
            result,
            source_request_id=request_id,
            target=target,
        )
        if public_result["duplicate"]:
            outcome = "skipped"
        elif not public_result["ok"]:
            outcome = "failed"
        elif public_result["failed"]:
            outcome = "partial"
        else:
            outcome = "succeeded"
        counts[outcome] += 1
        items.append({**public_result, "outcome": outcome})

    completed = counts["succeeded"] + counts["partial"]
    return api_response({
        "ok": completed > 0 and counts["failed"] == 0,
        "target": target,
        "requested": len(request_ids),
        **counts,
        "items": items,
    })


@router.post("/issues/batch/clear")
def clear_issues_batch(request: Request, data: dict | None = Body(default=None)):
    """批量确认并隐藏待处理告警，保留请求、任务、文件与日志。"""
    require_api_login(request)
    if not isinstance(data, dict):
        return api_error("请求数据必须是 JSON 对象", 400)
    try:
        request_ids = _normalize_record_ids(data.get("request_ids"), field="request_ids")
    except ValueError as exc:
        return api_error(str(exc), 400)
    result = db.clear_download_request_attentions(request_ids)
    skipped = len(result["not_attention"]) + len(result["not_found"])
    return api_response({
        "ok": True,
        "requested": len(request_ids),
        "cleared": len(result["cleared"]),
        "already_cleared": len(result["already_cleared"]),
        "skipped": skipped,
    })


@router.post("/issues/{request_id}/resubmit")
def resubmit_issue(
    request_id: int,
    request: Request,
    data: dict | None = Body(default=None),
):
    """使用旧请求保留的资源重新提交到 qB、光鸭或两者。"""
    require_api_login(request)
    if not isinstance(data, dict):
        return api_error("请求数据必须是 JSON 对象", 400)
    target = str(data.get("target") or "").strip().lower()
    if target not in SUPPORTED_TARGETS:
        return api_error("下载目标无效", 400)

    row = db.get_download_request(request_id)
    if not row:
        return api_error("待处理请求不存在", 404)
    if not _attention_stages(row):
        return api_error("该请求已经处理，无需重复提交", 409)
    capability = download_resubmit_capabilities(row).get(target) or {}
    if not capability.get("enabled"):
        return api_error(str(capability.get("reason") or "当前目标不可重新提交"), 400)

    try:
        result = resubmit_download_request(request_id, target)
    except Exception as exc:
        logger.exception(
            "重新提交下载请求失败 request=%s target=%s type=%s",
            request_id,
            target,
            type(exc).__name__,
        )
        return api_error("重新提交失败，请先核对下载服务状态", 502)

    public_result = _public_resubmit_result(
        result,
        source_request_id=request_id,
        target=target,
    )
    if public_result["duplicate"]:
        return api_response(public_result, 409)
    if not public_result["ok"]:
        return api_response(public_result, 502)
    return api_response(public_result)


@router.post("/issues/{request_id}/clear")
def clear_issue(request_id: int, request: Request):
    """仅确认并隐藏待处理告警，不删除任务、文件、请求或日志。"""
    require_api_login(request)
    result = db.clear_download_request_attention(request_id)
    if result == "not_found":
        return api_error("待处理请求不存在", 404)
    if result == "not_attention":
        return api_error("该请求当前无需处理", 409)
    return api_response({
        "ok": True,
        "request_id": int(request_id),
        "already_cleared": result == "already_cleared",
        "message": "已移出待处理",
    })


def _logs(source: str | None, status: str | None, keyword: str = "",
          page: int = 1, page_size: int = 20):
    total = db.count_download_logs(source=source, status=status, keyword=keyword)
    pages = (total + page_size - 1) // page_size if total else 0
    effective_page = min(page, pages) if pages else 1
    rows = db.list_download_logs(
        source=source,
        status=status,
        keyword=keyword,
        limit=page_size,
        offset=(effective_page - 1) * page_size,
    )
    items = [{
        "id": row["id"],
        "source": row["source"],
        "title": row["title"] or "",
        "path": row["path"] or "",
        "status": row["status"] or "submitted",
        "rss_item_id": row["rss_item_id"],
        "request_id": row["request_id"] if "request_id" in row.keys() else None,
        "backend_task_id": row["backend_task_id"] if "backend_task_id" in row.keys() else "",
        "progress": row["progress"] if "progress" in row.keys() else 0,
        "error": row["error"] if "error" in row.keys() else "",
        "created_at": row["created_at"],
    } for row in rows]
    return {
        "items": items,
        "page": effective_page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
    }


@router.post("/qb/{action}")
def qb_action(action: str, request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    if action not in _QB_ACTIONS:
        return api_error("不支持的操作", 400)
    if not isinstance(data, dict):
        return api_error("请求数据必须是 JSON 对象", 400)
    try:
        hashes = _normalize_qb_hashes(data.get("hashes"))
    except ValueError as exc:
        return api_error(str(exc), 400)
    joined_hashes = "|".join(hashes)
    client = None
    try:
        # 安全检查与真实 qB 写请求必须共享同一个跨进程 lease，避免检查后、
        # 请求前本地整理刚好进入文件提交阶段。
        with qb_control_write_lease():
            assert_qb_control_allowed(hashes, operation=action)
            client = _qb()
            if client is None:
                return api_error("未连接到 qBittorrent", 400)
            if action == "pause":
                client.pause_torrents(joined_hashes)
            elif action == "resume":
                client.resume_torrents(joined_hashes)
            else:
                # 下载页删除始终只移除 qB 任务，不允许客户端请求删除媒体文件。
                client.delete_torrents(joined_hashes, delete_files=False)
        return api_response({
            "success": True,
            "action": action,
            "accepted": len(hashes),
        })
    except QBControlSafetyUnavailable as exc:
        return api_error(str(exc), 503)
    except QBControlConflict as exc:
        return api_error(str(exc), 409)
    except Exception as exc:
        logger.error("qB 任务操作失败 action=%s type=%s", action, type(exc).__name__)
        return api_error("连接失败，请检查地址、认证信息和网络", 502)
    finally:
        close_qbittorrent_client(client)
