"""光鸭 302 播放代理路由。"""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.clients.guangya import GuangYaClient, close_guangya_client
from app.logger import get_logger, log_throttled
from app.modules.media_proxy import (
    PLAYGY_SIGNED_URL_TIMEOUT_SECONDS,
    SignedUrlCache,
    _parse_range,
    media_content_type,
)

logger = get_logger(__name__)
router = APIRouter()
_playgy_signed_urls = SignedUrlCache(max_entries=512)


def _playgy_response_etag(value: str) -> str:
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
    return f'"mf-{digest}"'


def play_gy(
    file_id: str, etag: str, size: str, filename: str,
    request: Request = None,  # type: ignore[assignment]
    v: str = "", sig: str = "", enc: str = "",
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
    content_length = 0
    if is_head:
        try:
            content_length = int(size)
        except (TypeError, ValueError):
            return JSONResponse({"error": "播放文件大小无效"}, status_code=400)
        if content_length < 0:
            return JSONResponse({"error": "播放文件大小无效"}, status_code=400)
    client = None
    try:
        client = GuangYaClient()
        if not client.logged_in:
            logger.warning("playgy 被访问但光鸭未登录")
            _playgy_signed_urls.clear()
            return JSONResponse({"error": "光鸭未登录"}, status_code=503)
        try:
            raw_client = client.raw
        except (AttributeError, RuntimeError):
            raw_client = getattr(client, "_raw", None)
        provider_token = str(getattr(raw_client, "token", "") or "")
        provider_scope = hashlib.sha256(
            provider_token.encode("utf-8")
        ).hexdigest()[:24]
        ua_bound = bool(
            getattr(raw_client, "download_url_user_agent_bound", False)
        )
        user_agent = request.headers.get("user-agent", "") if request is not None else ""

        def fetch_url() -> str | None:
            return client.get_download_url(
                file_id,
                timeout=PLAYGY_SIGNED_URL_TIMEOUT_SECONDS,
                raise_timeout=True,
            )

        result = _playgy_signed_urls.get_or_fetch_sync_result(
            file_id,
            fetch_url,
            scope=provider_scope,
            user_agent=user_agent,
            ua_bound=ua_bound,
        )
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
                range_value = str(
                    request.headers.get("range", "")
                    if request is not None
                    else ""
                ).strip()
                if_range = str(
                    request.headers.get("if-range", "")
                    if request is not None
                    else ""
                ).strip()
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
    except TimeoutError:
        log_throttled(
            logger, logging.WARNING, "playgy-timeout",
            "playgy 获取直链超时",
        )
        return JSONResponse({"error": "光鸭播放地址获取超时"}, status_code=504)
    except Exception as exc:
        log_throttled(
            logger, logging.ERROR, f"playgy-error:{type(exc).__name__}",
            "playgy 异常 type=%s", type(exc).__name__,
        )
        return JSONResponse({"error": "光鸭播放地址获取失败"}, status_code=500)
    finally:
        close_guangya_client(client)

_PLAYGY_PATH = "/playgy/{file_id}/{etag}/{size}/{filename:path}"


@router.get(
    _PLAYGY_PATH,
    name="proxy.play_gy_get",
    operation_id="proxy_play_gy_get",
)
def play_gy_get(
    file_id: str,
    etag: str,
    size: str,
    filename: str,
    request: Request,
    v: str = "",
    sig: str = "",
    enc: str = "",
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
    )


@router.head(
    _PLAYGY_PATH,
    name="proxy.play_gy_head",
    operation_id="proxy_play_gy_head",
)
def play_gy_head(
    file_id: str,
    etag: str,
    size: str,
    filename: str,
    request: Request,
    v: str = "",
    sig: str = "",
    enc: str = "",
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
    )
