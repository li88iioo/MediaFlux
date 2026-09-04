"""媒体服务器图片代理：为严格 CSP 提供同源海报地址。"""
from __future__ import annotations

from collections import OrderedDict
import logging
import re
import threading
import time

import requests
from fastapi import APIRouter, HTTPException, Request, Response

from app import config
from app.logger import get_logger, log_throttled
from app.modules.image_payload import ImagePayloadError, read_bounded_image

logger = get_logger(__name__)
router = APIRouter()
_ITEM_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_MISSING_IMAGE_TTL_SECONDS = 30
_MISSING_IMAGE_CACHE_MAX_ENTRIES = 512
_missing_image_cache: OrderedDict[tuple[str, str, str, str], float] = OrderedDict()
_missing_image_cache_lock = threading.Lock()


def _missing_image_cache_key(
    request: Request, server: str, base_url: str, item_id: str,
) -> tuple[str, str, str, str]:
    # 图片 tag 会在 Jellyfin 刷新海报后变化，把它纳入缓存键可避免旧缺图结果误伤新图。
    image_tag = str(request.query_params.get("tag") or "")[:128]
    return server, base_url, item_id, image_tag


def _is_missing_image_cached(key: tuple[str, str, str, str]) -> bool:
    now = time.monotonic()
    with _missing_image_cache_lock:
        expires_at = _missing_image_cache.get(key)
        if expires_at is None:
            return False
        if expires_at <= now:
            _missing_image_cache.pop(key, None)
            return False
        _missing_image_cache.move_to_end(key)
        return True


def _remember_missing_image(key: tuple[str, str, str, str]) -> None:
    with _missing_image_cache_lock:
        _missing_image_cache[key] = time.monotonic() + _MISSING_IMAGE_TTL_SECONDS
        _missing_image_cache.move_to_end(key)
        while len(_missing_image_cache) > _MISSING_IMAGE_CACHE_MAX_ENTRIES:
            _missing_image_cache.popitem(last=False)


def _missing_image_response() -> Response:
    return Response(
        status_code=404,
        headers={
            "Cache-Control": f"private, max-age={_MISSING_IMAGE_TTL_SECONDS}",
        },
    )


@router.get("/media-image/{server}/{item_id}", name="media_image.media_image")
def media_image(request: Request, server: str, item_id: str):
    if not request.session.get("logged_in"):
        raise HTTPException(status_code=401, detail="unauthorized")
    if not _ITEM_ID_RE.fullmatch(item_id):
        raise HTTPException(status_code=400, detail="invalid item id")
    if server == "jellyfin":
        base_url = config.get("JELLYFIN_URL", "").rstrip("/")
        token = config.get("JELLYFIN_API_KEY", "")
        headers = {"Authorization": f'MediaBrowser Token="{token}"'}
        params = {"maxWidth": 480, "quality": 90}
    elif server == "emby":
        base_url = config.get("EMBY_URL", "").rstrip("/")
        token = config.get("EMBY_TOKEN", "")
        headers = {
            "X-Emby-Token": token,
            "Authorization": f'MediaBrowser Token="{token}"',
        }
        params = {"maxWidth": 480, "quality": 90}
    else:
        raise HTTPException(status_code=404, detail="unknown media server")
    if not base_url or not token:
        raise HTTPException(status_code=404, detail="media server not configured")
    cache_key = _missing_image_cache_key(request, server, base_url, item_id)
    if _is_missing_image_cached(cache_key):
        return _missing_image_response()
    upstream = None
    try:
        upstream = requests.get(
            f"{base_url}/Items/{item_id}/Images/Primary",
            headers=headers,
            params=params,
            timeout=15,
            stream=True,
        )
        if upstream.status_code == 404:
            _remember_missing_image(cache_key)
            logger.debug(f"[{server}] 海报尚未就绪或不存在 item_id={item_id}")
            return _missing_image_response()
        upstream.raise_for_status()
        try:
            content, content_type = read_bounded_image(upstream)
        except ImagePayloadError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return Response(
            content=content,
            media_type=content_type,
            headers={
                "Cache-Control": "private, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except HTTPException:
        raise
    except requests.RequestException as exc:
        log_throttled(
            logger,
            logging.WARNING,
            f"media-image:{server}:{type(exc).__name__}",
            "媒体海报代理失败 server=%s type=%s",
            server,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="upstream image failed") from exc
    finally:
        if upstream is not None:
            upstream.close()
