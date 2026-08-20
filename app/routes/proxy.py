"""光鸭 302 播放代理路由。"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.clients.guangya import GuangYaClient
from app.logger import get_logger
from app.modules.media_proxy import (
    PLAYGY_SIGNED_URL_TIMEOUT_SECONDS,
    SignedUrlCache,
)

logger = get_logger(__name__)
router = APIRouter()
_playgy_signed_urls = SignedUrlCache(max_entries=512)


@router.api_route(
    "/playgy/{file_id}/{etag}/{size}/{filename:path}",
    methods=["GET", "HEAD"],
    name="proxy.play_gy",
)
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
            logger.info("[302] 光鸭 file_id=%s cache_hit=%s", file_id, result.cache_hit)
            return RedirectResponse(
                result.url,
                status_code=302,
                headers={
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                    "Referrer-Policy": "no-referrer",
                },
            )
        logger.warning(f"光鸭直链获取失败 file_id={file_id}")
        return JSONResponse({"error": "无法获取直链"}, status_code=404)
    except TimeoutError:
        logger.warning("playgy 获取直链超时 file_id=%s", file_id)
        return JSONResponse({"error": "光鸭播放地址获取超时"}, status_code=504)
    except Exception as exc:
        logger.error("playgy 异常 type=%s", type(exc).__name__)
        return JSONResponse({"error": "光鸭播放地址获取失败"}, status_code=500)
