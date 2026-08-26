"""Emby / Jellyfin 多实例媒体反代管理 API。"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Body, Request

from app import database as db
from app.modules.media_server_profiles import (
    list_configured_profiles,
    resolve_proxy_instance,
)
from app.modules.media_proxy import (
    clear_signed_url_cache,
    get_media_proxy_manager,
    probe_media_proxy_instance,
    ProxyUpstreamBodyTooLarge,
    signed_url_cache_metrics,
    validate_listen_host,
    validate_upstream_url,
)
from app.modules.media_proxy_forwarding import (
    decode_trusted_proxy_cidrs,
    encode_trusted_proxy_cidrs,
    normalize_trusted_proxy_cidrs,
)
from app.web import api_error, api_response, require_api_login

router = APIRouter(prefix="/api/media-proxy")
_MASK = "********"


def _manager(request: Request):
    return getattr(request.app.state, "media_proxy_manager", get_media_proxy_manager())


def _row_value(row, key: str, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _boolean_value(value, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("转发头信任开关无效")


def _instance_json(row, runtime: dict | None = None) -> dict:
    runtime = runtime or {}
    config_source = str(row["config_source"] or "custom")
    source_label = "自定义上游"
    upstream_url = str(row["upstream_url"] or "")
    has_api_key = bool(row["api_key"])
    try:
        resolved = resolve_proxy_instance(row)
        upstream_url = str(resolved.get("upstream_url") or upstream_url)
        has_api_key = bool(resolved.get("api_key"))
        source_label = str(resolved.get("source_label") or source_label)
    except ValueError:
        labels = {profile.source: profile.label for profile in list_configured_profiles()}
        source_label = labels.get(config_source, source_label)
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "server_type": row["server_type"],
        "config_source": config_source,
        "source_label": source_label,
        "upstream_url": upstream_url,
        "api_key": _MASK if has_api_key else "",
        "has_api_key": has_api_key,
        "listen_host": row["listen_host"],
        "listen_port": int(row["listen_port"]),
        "local_root": row["local_root"] or "",
        "trust_forwarded_headers": bool(
            int(_row_value(row, "trust_forwarded_headers", 0) or 0)
        ),
        "trusted_proxy_cidrs": list(
            decode_trusted_proxy_cidrs(
                _row_value(row, "trusted_proxy_cidrs_json", "[]")
            )
        ),
        "enabled": bool(row["enabled"]),
        "status": "running" if runtime.get("running") else (row["status"] or "stopped"),
        "last_error": row["last_error"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "proxy_url": f"http://{_display_host(row['listen_host'])}:{int(row['listen_port'])}",
    }


def _binding_json(row) -> dict:
    return {
        "id": int(row["id"]),
        "instance_id": int(row["instance_id"]),
        "media_item_id": row["media_item_id"],
        "media_source_id": row["media_source_id"] or "",
        "source_type": row["source_type"],
        "guangya_file_id": row["guangya_file_id"] or "",
        "local_relative_path": row["local_relative_path"] or "",
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _display_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return f"[{host}]" if ":" in host else host


def _validated_instance(data: dict, existing=None) -> tuple[dict | None, str]:
    name = str(data.get("name") or "").strip()
    existing_source = str(existing["config_source"] or "custom") if existing is not None else "custom"
    config_source = str(data.get("config_source") or existing_source).strip().lower()
    if not name or len(name) > 100:
        return None, "实例名称必填且不能超过 100 字符"
    if config_source not in {"configured:jellyfin", "configured:emby", "custom"}:
        return None, "媒体服务器来源无效"
    try:
        listen_host = validate_listen_host(data.get("listen_host"))
        listen_port = int(data.get("listen_port"))
    except (TypeError, ValueError) as exc:
        return None, str(exc)
    if listen_port < 1024 or listen_port > 65535:
        return None, "监听端口必须在 1024 到 65535 之间"
    local_root = str(data.get("local_root") or "").strip()
    if local_root:
        root = Path(local_root).expanduser()
        if not root.is_absolute():
            return None, "本地媒体根目录必须是绝对路径"
        local_root = str(root.resolve(strict=False))

    existing_trust = bool(
        int(_row_value(existing, "trust_forwarded_headers", 0) or 0)
    )
    try:
        trust_forwarded_headers = _boolean_value(
            data.get("trust_forwarded_headers"),
            default=existing_trust,
        )
        trusted_proxy_cidrs = normalize_trusted_proxy_cidrs(
            data.get(
                "trusted_proxy_cidrs",
                _row_value(existing, "trusted_proxy_cidrs_json", "[]"),
            )
        )
    except ValueError as exc:
        return None, str(exc)
    if trust_forwarded_headers and not trusted_proxy_cidrs:
        return None, "启用转发头信任时至少填写一个可信代理地址"

    if config_source == "custom":
        server_type = str(data.get("server_type") or "jellyfin").strip().lower()
        if server_type not in {"jellyfin", "emby"}:
            return None, "服务器类型只支持 Jellyfin 或 Emby"
        try:
            upstream_url = validate_upstream_url(data.get("upstream_url"))
        except ValueError as exc:
            return None, str(exc)
        api_key = str(data.get("api_key") or "").strip()
        if api_key == _MASK and existing is not None:
            api_key = str(existing["api_key"] or "")
    else:
        server_type = config_source.split(":", 1)[1]
        try:
            resolve_proxy_instance({
                "config_source": config_source,
                "server_type": server_type,
                "upstream_url": "",
                "api_key": "",
            })
        except ValueError as exc:
            return None, str(exc)
        upstream_url = ""
        api_key = ""
    return {
        "name": name,
        "server_type": server_type,
        "config_source": config_source,
        "upstream_url": upstream_url,
        "api_key": api_key,
        "listen_host": listen_host,
        "listen_port": listen_port,
        "local_root": local_root,
        "trust_forwarded_headers": 1 if trust_forwarded_headers else 0,
        "trusted_proxy_cidrs_json": encode_trusted_proxy_cidrs(
            trusted_proxy_cidrs
        ),
        "enabled": 1 if data.get("enabled", True) else 0,
    }, ""


def _validated_binding(data: dict, instance) -> tuple[dict | None, str]:
    media_item_id = str(data.get("media_item_id") or "").strip()
    media_source_id = str(data.get("media_source_id") or "").strip()
    source_type = str(data.get("source_type") or "").strip().lower()
    guangya_file_id = str(data.get("guangya_file_id") or "").strip()
    if not media_item_id or len(media_item_id) > 200:
        return None, "媒体项目 ID 必填且不能超过 200 字符"
    if source_type == "local":
        return None, "本地媒体由 Jellyfin/Emby 处理，不再支持本地绑定"
    if source_type != "guangya":
        return None, "播放来源只支持 guangya"
    if not guangya_file_id:
        return None, "光鸭绑定必须填写文件 ID"
    return {
        "media_item_id": media_item_id,
        "media_source_id": media_source_id,
        "source_type": source_type,
        "guangya_file_id": guangya_file_id,
        "local_relative_path": "",
        "enabled": 1 if data.get("enabled", True) else 0,
    }, ""


async def _reconcile(request: Request):
    if not bool(getattr(request.app.state, "background_services_enabled", False)):
        return {"started": [], "stopped": [], "failed": {}}
    return await _manager(request).reconcile()


@router.get("")
async def list_instances(request: Request):
    require_api_login(request)
    runtime = _manager(request).status()
    return api_response([
        _instance_json(row, runtime.get(int(row["id"])))
        for row in await asyncio.to_thread(db.list_media_proxy_instances)
    ])


@router.get("/profiles")
async def list_profiles(request: Request):
    require_api_login(request)
    return api_response([profile.public_dict() for profile in list_configured_profiles()])


@router.get("/sessions")
async def list_playback_sessions(
    request: Request,
    instance_id: int | None = None,
    status: str = "",
    source: str = "",
    page: int = 1,
    page_size: int = 20,
):
    require_api_login(request)
    try:
        sessions = await asyncio.to_thread(
            db.list_media_proxy_playback_sessions,
            instance_id=instance_id,
            status=status,
            source=source,
            page=page,
            page_size=page_size,
        )
        return api_response(sessions)
    except ValueError as exc:
        return api_error(str(exc), 400)


@router.get("/records")
async def list_playback_records(
    request: Request,
    instance_id: int | None = None,
    session_id: int | None = None,
    unlinked: bool = False,
    status: str = "",
    source: str = "",
    page: int = 1,
    page_size: int = 50,
):
    require_api_login(request)
    try:
        records = await asyncio.to_thread(
            db.list_media_proxy_playback_records,
            instance_id=instance_id,
            session_id=session_id,
            unlinked=unlinked,
            status=status,
            source=source,
            page=page,
            page_size=page_size,
        )
        return api_response(records)
    except ValueError as exc:
        return api_error(str(exc), 400)


@router.delete("/records")
async def clear_playback_records(request: Request, data: dict = Body(...)):
    require_api_login(request)
    if set(data) - {"confirm", "instance_id"}:
        return api_error("请求包含不允许的字段", 400)
    if data.get("confirm") != "CLEAR PLAYBACK RECORDS":
        return api_error("确认文本无效", 400)
    instance_id = data.get("instance_id")
    if instance_id is not None:
        if isinstance(instance_id, bool):
            return api_error("instance_id 无效", 400)
        try:
            instance_id = int(instance_id)
        except (TypeError, ValueError):
            return api_error("instance_id 无效", 400)
    deleted = await asyncio.to_thread(
        db.clear_media_proxy_playback_records, instance_id=instance_id
    )
    return api_response({"deleted": deleted})


@router.get("/cache-metrics")
async def cache_metrics(request: Request, instance_id: int | None = None):
    require_api_login(request)
    return api_response(signed_url_cache_metrics(instance_id))


@router.post("")
async def create_instance(request: Request, data: dict = Body(...)):
    require_api_login(request)
    payload, error = _validated_instance(data)
    if payload is None:
        return api_error(error or "实例参数校验失败", 400)
    try:
        instance_id = await asyncio.to_thread(
            db.add_media_proxy_instance, **payload
        )
    except sqlite3.IntegrityError:
        return api_error("监听地址和端口已被其他实例占用", 409)
    result = await _reconcile(request)
    row = await asyncio.to_thread(db.get_media_proxy_instance, instance_id)
    return api_response({"success": True, "instance": _instance_json(row), "runtime": result}, 201)


@router.put("/{instance_id}")
async def update_instance(instance_id: int, request: Request, data: dict = Body(...)):
    require_api_login(request)
    existing = await asyncio.to_thread(db.get_media_proxy_instance, instance_id)
    if not existing:
        return api_error("媒体反代实例不存在", 404)
    payload, error = _validated_instance(data, existing)
    if payload is None:
        return api_error(error or "实例参数校验失败", 400)
    try:
        await asyncio.to_thread(db.update_media_proxy_instance, instance_id, payload)
    except sqlite3.IntegrityError:
        return api_error("监听地址和端口已被其他实例占用", 409)
    clear_signed_url_cache(instance_id)
    result = await _reconcile(request)
    row = await asyncio.to_thread(db.get_media_proxy_instance, instance_id)
    return api_response({"success": True, "instance": _instance_json(row), "runtime": result})


@router.delete("/{instance_id}")
async def delete_instance(instance_id: int, request: Request):
    require_api_login(request)
    existing = await asyncio.to_thread(db.get_media_proxy_instance, instance_id)
    if not existing:
        return api_error("媒体反代实例不存在", 404)
    await asyncio.to_thread(
        db.update_media_proxy_instance, instance_id, {"enabled": 0}
    )
    clear_signed_url_cache(instance_id)
    await _reconcile(request)
    if not await asyncio.to_thread(db.delete_media_proxy_instance, instance_id):
        return api_error("媒体反代实例不存在", 404)
    return api_response({"success": True})


@router.post("/{instance_id}/reload")
async def reload_instance(instance_id: int, request: Request):
    require_api_login(request)
    if not await asyncio.to_thread(db.get_media_proxy_instance, instance_id):
        return api_error("媒体反代实例不存在", 404)
    clear_signed_url_cache(instance_id)
    result = await _reconcile(request)
    return api_response({"success": True, "runtime": result})


@router.post("/{instance_id}/test")
async def test_instance(instance_id: int, request: Request):
    require_api_login(request)
    if not await asyncio.to_thread(db.get_media_proxy_instance, instance_id):
        return api_error("媒体反代实例不存在", 404)
    try:
        result = await probe_media_proxy_instance(instance_id, timeout_seconds=10.0)
    except LookupError:
        return api_error("媒体反代实例不存在", 404)
    except ProxyUpstreamBodyTooLarge:
        return api_error("上游响应过大", 502)
    except ValueError as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        return api_error(f"连接失败：{exc}", 502)
    status_code = int(result["status_code"])
    return api_response({
        "success": 200 <= status_code < 400,
        "status_code": status_code,
        "elapsed_ms": int(result["latency_ms"]),
    }, 200 if status_code < 400 else 502)


@router.get("/{instance_id}/bindings")
async def list_bindings(instance_id: int, request: Request):
    require_api_login(request)
    if not await asyncio.to_thread(db.get_media_proxy_instance, instance_id):
        return api_error("媒体反代实例不存在", 404)
    return api_response([
        _binding_json(row)
        for row in await asyncio.to_thread(db.list_media_proxy_bindings, instance_id)
    ])


@router.post("/{instance_id}/bindings")
async def create_binding(instance_id: int, request: Request, data: dict = Body(...)):
    require_api_login(request)
    instance = await asyncio.to_thread(db.get_media_proxy_instance, instance_id)
    if not instance:
        return api_error("媒体反代实例不存在", 404)
    payload, error = _validated_binding(data, instance)
    if payload is None:
        return api_error(error or "绑定参数校验失败", 400)
    try:
        row = await asyncio.to_thread(
            db.create_media_proxy_binding, instance_id=instance_id, **payload
        )
    except sqlite3.IntegrityError:
        return api_error("该媒体项目和媒体源已经存在绑定", 409)
    return api_response({"success": True, "binding": _binding_json(row)}, 201)


@router.delete("/{instance_id}/bindings/{binding_id}")
async def delete_binding(instance_id: int, binding_id: int, request: Request):
    require_api_login(request)
    if not await asyncio.to_thread(
        db.delete_media_proxy_binding, binding_id, instance_id
    ):
        return api_error("媒体绑定不存在", 404)
    return api_response({"success": True})
