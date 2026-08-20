"""登录页 TMDB 每日电影壁纸。"""
from __future__ import annotations

import json
import math
import random
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from app import config, database as db
from app.clients.tmdb import TMDBClient
from app.logger import get_logger

logger = get_logger(__name__)

CACHE_KEY = "login_wallpaper.tmdb.daily.v1"
CACHE_TTL_SECONDS = 24 * 60 * 60
FAILURE_RETRY_SECONDS = 5 * 60
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w1280"
TMDB_WEB_BASE = "https://www.themoviedb.org/movie"
_SAFE_IMAGE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
_RANDOM = random.SystemRandom()
_REFRESH_LOCK = threading.Lock()
_refreshing = False


def _image_path(value: object) -> str:
    path = str(value or "").strip()
    if (
        not path
        or not _SAFE_IMAGE_PATH.fullmatch(path)
        or any(segment == ".." for segment in path.split("/"))
    ):
        return ""
    return "/" + path.lstrip("/")


def _wallpaper_record(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        tmdb_id = int(value.get("id") or value.get("tmdb_id") or 0)
    except (TypeError, ValueError):
        return None
    title = str(value.get("title") or value.get("original_title") or "").strip()
    image_path = _image_path(value.get("backdrop_path"))
    if not image_path:
        image_path = _image_path(value.get("poster_path"))
    if not image_path:
        image_path = _image_path(value.get("image_path"))
    if tmdb_id <= 0 or not title or not image_path:
        return None
    return {
        "tmdb_id": tmdb_id,
        "title": title[:160],
        "image_path": image_path,
    }


def _render_wallpaper(record: object) -> dict[str, Any] | None:
    normalized = _wallpaper_record(record)
    if not normalized:
        return None
    tmdb_id = normalized["tmdb_id"]
    return {
        "tmdb_id": tmdb_id,
        "title": normalized["title"],
        "image_url": f"{TMDB_IMAGE_BASE}{normalized['image_path']}",
        "tmdb_url": f"{TMDB_WEB_BASE}/{tmdb_id}",
    }


def _empty_cache(*, available: bool = True) -> dict[str, Any]:
    return {
        "available": available,
        "expires_at": 0.0,
        "retry_after": 0.0,
        "record": None,
        "wallpaper": None,
    }


def _load_cache() -> dict[str, Any]:
    try:
        raw = db.kv_get(CACHE_KEY)
        if not raw:
            return _empty_cache()
        payload = json.loads(raw)
        expires_at = float(payload.get("expires_at") or 0)
        retry_after = float(payload.get("retry_after") or 0)
        if (
            not math.isfinite(expires_at)
            or not math.isfinite(retry_after)
            or expires_at < 0
            or retry_after < 0
        ):
            return _empty_cache()
        record = _wallpaper_record(payload.get("wallpaper"))
        return {
            "available": True,
            "expires_at": expires_at,
            "retry_after": retry_after,
            "record": record,
            "wallpaper": _render_wallpaper(record),
        }
    except Exception as exc:
        logger.warning("登录页壁纸缓存读取失败 (%s)", type(exc).__name__)
        return _empty_cache(available=False)


def _store_cache(
    *,
    expires_at: float,
    retry_after: float,
    record: dict[str, Any] | None,
) -> None:
    try:
        db.kv_set(
            CACHE_KEY,
            json.dumps(
                {
                    "expires_at": float(expires_at),
                    "retry_after": float(retry_after),
                    "wallpaper": record,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    except Exception as exc:
        logger.warning("登录页壁纸缓存写入失败 (%s)", type(exc).__name__)


def _configured() -> bool:
    try:
        return (
            config.get("LOGIN_WALLPAPER_MODE", "default").strip().lower() == "tmdb"
            and bool(config.get("TMDB_API_KEY", "").strip())
        )
    except Exception:
        return False


def refresh_login_wallpaper(
    *,
    now: float | None = None,
    chooser: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    client_factory: Callable[..., TMDBClient] = TMDBClient,
) -> dict[str, Any] | None:
    """同步刷新缓存；只由后台单飞任务和测试调用。"""
    if not _configured():
        return None
    current = time.time() if now is None else float(now)
    previous = _load_cache()
    try:
        client = client_factory(timeout=(2.0, 5.0), retries=0)
        payload = client.get(
            "/movie/popular",
            {"page": 1, "include_adult": "false"},
        )
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            raise ValueError("TMDB 热门电影响应结构无效")
        candidates = [
            record
            for record in (_wallpaper_record(row) for row in rows)
            if record is not None
        ]
        if not candidates:
            raise ValueError("TMDB 热门电影没有可用图片")
        record = (chooser or _RANDOM.choice)(candidates)
        wallpaper = _render_wallpaper(record)
        if not wallpaper:
            raise ValueError("TMDB 壁纸候选无效")
        _store_cache(
            expires_at=current + CACHE_TTL_SECONDS,
            retry_after=0,
            record=record,
        )
        return wallpaper
    except Exception as exc:
        _store_cache(
            expires_at=previous["expires_at"],
            retry_after=current + FAILURE_RETRY_SECONDS,
            record=previous["record"],
        )
        logger.warning("登录页 TMDB 壁纸刷新失败 (%s)", type(exc).__name__)
        return previous["wallpaper"]


def _refresh_worker() -> None:
    global _refreshing
    try:
        refresh_login_wallpaper()
    finally:
        with _REFRESH_LOCK:
            _refreshing = False


def schedule_login_wallpaper_refresh(*, force: bool = False) -> bool:
    """后台单飞刷新；调用方永不等待 TMDB 网络。"""
    global _refreshing
    if not _configured():
        return False
    current = time.time()
    cached = _load_cache()
    if not cached["available"]:
        return False
    if not force:
        if cached["wallpaper"] and cached["expires_at"] > current:
            return False
        if cached["retry_after"] > current:
            return False
    with _REFRESH_LOCK:
        if _refreshing:
            return False
        _refreshing = True
    thread = threading.Thread(
        target=_refresh_worker,
        name="mediaflux-login-wallpaper",
        daemon=True,
    )
    thread.start()
    return True


def get_login_wallpaper(
    *,
    now: float | None = None,
    allow_refresh: bool = True,
) -> dict[str, Any] | None:
    """立即返回新鲜或陈旧缓存，必要时后台刷新，绝不等待 TMDB。"""
    if not _configured():
        return None
    current = time.time() if now is None else float(now)
    cached = _load_cache()
    if not cached["available"]:
        return None
    if (
        allow_refresh
        and cached["expires_at"] <= current
        and cached["retry_after"] <= current
    ):
        schedule_login_wallpaper_refresh()
    return cached["wallpaper"]
