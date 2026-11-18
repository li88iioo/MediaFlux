"""光鸭原画 302 与同一签名 STRM 的云端画质发现/解析。"""
from __future__ import annotations

import hashlib
import logging
import time
from functools import partial

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.clients.guangya import is_guangya_unauthorized
from app.logger import get_logger, log_throttled
from app.modules.media_proxy import (
    PLAYGY_SIGNED_URL_TIMEOUT_SECONDS,
    SignedUrlCache,
    SignedUrlCacheResult,
    _SignedMediaProbeCapacityError,
    _parse_range,
    media_content_type,
    resolve_guangya_playback,
    run_signed_media_probe_sync,
)

logger = get_logger(__name__)
router = APIRouter()
_playgy_signed_urls = SignedUrlCache(max_entries=512)


def _playgy_response_etag(value: str) -> str:
    digest = hashlib.sha256((value or "").encode("utf-8")).hexdigest()
    return f'"mf-{digest}"'


def play_gy(
    file_id: str, etag: str, size: str, filename: str,
    request: Request = None,  # type: ignore[assignment]
    v: str = "", sig: str = "", enc: str = "",
    view: str = "", quality: str = "",
):
    from app.modules.playgy_signing import decode_playgy_path_token, verify_playgy

    if enc:
        if enc != "b64":
            return JSONResponse({"error": "播放地址编码无效"}, status_code=400)
        try:
            file_id = decode_playgy_path_token(file_id)
            etag = decode_playgy_path_token(etag)
        except ValueError:
            return JSONResponse({"error": "播放地址编码无效"}, status_code=400)

    if not sig:
        return JSONResponse({"error": "播放地址缺少有效签名"}, status_code=403)
    if not verify_playgy(file_id, etag, size, v, sig):
        return JSONResponse({"error": "播放地址签名无效"}, status_code=403)
    is_head = str(getattr(request, "method", "")).upper() == "HEAD"
    if view not in {"", "variants", "resolve"} or bool(quality) != (view == "resolve"):
        return JSONResponse({"error": "画质查询参数无效"}, status_code=400)
    if view and is_head:
        return JSONResponse({"error": "画质查询请使用 GET"}, status_code=405, headers={"Allow": "GET"})
    content_length = 0
    if is_head:
        try:
            content_length = int(size)
        except (TypeError, ValueError):
            return JSONResponse({"error": "播放文件大小无效"}, status_code=400)
        if content_length < 0:
            return JSONResponse({"error": "播放文件大小无效"}, status_code=400)
    try:
        deadline = time.monotonic() + PLAYGY_SIGNED_URL_TIMEOUT_SECONDS
        result = run_signed_media_probe_sync(
            partial(
                resolve_guangya_playback, file_id, deadline=deadline,
                cache=None if view else _playgy_signed_urls,
                quality=quality if view == "resolve" else None,
                user_agent=request.headers.get("user-agent", "") if request is not None else "",
            ),
            deadline=deadline,
        )
        if result is None:
            logger.warning("playgy 被访问但光鸭未登录")
            _playgy_signed_urls.clear()
            return JSONResponse({"error": "光鸭未登录"}, status_code=503)
        if view:
            return JSONResponse(
                {"protocol": "mediaflux.playback.v1", **result},
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        assert isinstance(result, SignedUrlCacheResult)
        if result.url:
            if is_head:
                logger.debug(
                    "[HEAD] 光鸭 file_id=%s cache_hit=%s",
                    file_id,
                    result.cache_hit,
                )
                response_etag = _playgy_response_etag(etag)
                response_headers = {
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-store",
                    "ETag": response_etag,
                    "Content-Type": media_content_type(filename),
                    "Pragma": "no-cache",
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                }
                range_value = (request.headers.get("range", "")
                    if request is not None
                    else "").strip()
                if_range = (request.headers.get("if-range", "")
                    if request is not None
                    else "").strip()
                # If-Range 只接受本响应给出的强 entity-tag；weak tag、日期或
                # 其他 validator 均视为失配，并按完整表示返回 200。
                if range_value and if_range and if_range != response_etag:
                    range_value = ""
                try:
                    selected_range = _parse_range(range_value, content_length)
                except (TypeError, ValueError):
                    response_headers["Content-Range"] = (
                        f"bytes */{content_length}"
                    )
                    return Response(
                        status_code=416,
                        headers=response_headers,
                    )
                if selected_range is None:
                    response_headers["Content-Length"] = str(content_length)
                    return Response(
                        status_code=200,
                        headers=response_headers,
                    )
                start, end = selected_range
                response_headers.update({
                    "Content-Length": str(end - start + 1),
                    "Content-Range": (
                        f"bytes {start}-{end}/{content_length}"
                    ),
                })
                return Response(
                    status_code=206,
                    headers=response_headers,
                )
            logger.debug("[302] 光鸭 file_id=%s cache_hit=%s", file_id, result.cache_hit)
            return RedirectResponse(
                result.url,
                status_code=302,
                headers={
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                    "Referrer-Policy": "no-referrer",
                },
            )
        log_throttled(
            logger, logging.WARNING, "playgy-url-missing",
            "光鸭直链获取失败",
        )
        return JSONResponse({"error": "无法获取直链"}, status_code=404)
    except _SignedMediaProbeCapacityError:
        return JSONResponse({"error": "媒体直链探测繁忙，请稍后重试"}, status_code=503)
    except TimeoutError:
        log_throttled(
            logger, logging.WARNING, "playgy-timeout",
            "playgy 获取直链超时",
        )
        return JSONResponse({"error": "光鸭播放地址获取超时"}, status_code=504)
    except Exception as exc:
        if view and isinstance(exc, FileNotFoundError):
            return JSONResponse({"error": "所选画质已不可用，请刷新画质列表"}, status_code=404)
        stage = view or "original"
        upstream_status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        log_throttled(
            logger, logging.ERROR, f"playgy-error:{stage}:{type(exc).__name__}:{upstream_status}",
            "playgy 异常 stage=%s upstream_http=%s type=%s", stage, upstream_status, type(exc).__name__,
        )
        return JSONResponse({"error": "光鸭播放地址获取失败"}, status_code=502 if view or is_guangya_unauthorized(exc) else 500)

_PLAYGY_PATH = "/playgy/{file_id}/{etag}/{size}/{filename:path}"


@router.head(
    _PLAYGY_PATH,
    name="proxy.play_gy_head",
    operation_id="proxy_play_gy_head",
)
@router.get(
    _PLAYGY_PATH,
    name="proxy.play_gy_get",
    operation_id="proxy_play_gy_get",
)
def play_gy_endpoint(
    file_id: str,
    etag: str,
    size: str,
    filename: str,
    request: Request,
    v: str = "",
    sig: str = "",
    enc: str = "",
    view: str = "",
    quality: str = "",
):
    return play_gy(
        file_id,
        etag,
        size,
        filename,
        request=request,
        v=v,
        sig=sig,
        enc=enc,
        view=view,
        quality=quality,
    )
