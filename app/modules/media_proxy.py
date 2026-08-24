"""多实例 Emby / Jellyfin HTTP 媒体反代运行时。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import re
import secrets
import socket
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import formatdate
from functools import partial
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit

import httpx
import uvicorn
from aiohttp import (
    ClientSession, TCPConnector, TraceConfig, WSServerHandshakeError, WSMsgType,
)
from aiohttp.abc import AbstractResolver
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app import database
from app.clients.guangya import GuangYaClient
from app.config import get
from app.logger import get_logger, log_throttled
from app.modules.media_proxy_recorder import PlaybackRecordWriter
from app.modules.media_proxy_safety import safe_media_name as _safe_media_name
from app.modules.media_server_profiles import resolve_proxy_instance

logger = get_logger(__name__)
_STREAM_RE = re.compile(r"^(?:/emby)?/Videos/([^/]+)/stream(?:\.[^/]+)?/?$", re.IGNORECASE)
_VIDEO_ITEM_RE = re.compile(r"^(?:/emby)?/Videos/([^/]+)/", re.IGNORECASE)
_PLAYBACK_INFO_RE = re.compile(r"^(?:/emby)?/Items/([^/]+)/PlaybackInfo/?$", re.IGNORECASE)
_PLAYGY_RE = re.compile(r"^(?:/emby)?/playgy/([^/]+)(?:/.*)?$", re.IGNORECASE)
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
_REQUEST_ONLY_HEADERS = {"host", "content-length"}
_FORWARDED_CLIENT_HEADERS = {
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
}
_ALLOWED_HOSTS = {"127.0.0.1", "0.0.0.0", "::1", "::"}
_BLOCKED_METADATA_HOSTS = {
    "instance-data.ec2.internal",
    "metadata.google.internal",
    "metadata.google.internal.",
    "metadata.tencentyun.com",
}
_BLOCKED_METADATA_IPS = {
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
}
_PLAYBACK_SESSION_QUERY_KEY = "_mfps"
_PLAYBACK_SOURCE_QUERY_KEY = "_mfss"
_PLAYBACK_AUTHORIZATION_TTL_SECONDS = 15 * 60
_PLAYBACK_AUTHORIZATION_MAX_TTL_SECONDS = 12 * 60 * 60
_PLAYBACK_INFO_PREFERENCE_PARSE_MAX_BYTES = 1024 * 1024
_SENSITIVE_QUERY_KEYS = {
    "api_key", "apikey", "x-emby-token", "x-mediabrowser-token",
    _PLAYBACK_SESSION_QUERY_KEY, _PLAYBACK_SOURCE_QUERY_KEY,
}
_AUTH_SCOPE_SECRET = secrets.token_bytes(32)
_AUTH_TOKEN_RE = re.compile(
    r'(?:^|[,\s])Token\s*=\s*(?:"([^"]+)"|([^,\s]+))',
    re.IGNORECASE,
)
_AUTH_CLIENT_RE = re.compile(
    r'(?:^|[,\s])Client\s*=\s*(?:"([^"]+)"|([^,\s]+))',
    re.IGNORECASE,
)
_AUTH_DEVICE_ID_RE = re.compile(
    r'(?:^|[,\s])DeviceId\s*=\s*(?:"([^"]+)"|([^,\s]+))',
    re.IGNORECASE,
)
_MEDIA_BROWSER_AUTH_RE = re.compile(r"^\s*MediaBrowser(?:\s+|$)", re.IGNORECASE)
_HLS_PATH_RE = re.compile(
    r"(?:\.m3u8$|/(?:hls\d*|master|main)/|\.ts$|\.m4s$)",
    re.IGNORECASE,
)
_signed_url_caches: dict[int, SignedUrlCache] = {}
_signed_url_caches_lock = threading.RLock()

_PROXY_CONNECT_TIMEOUT_SECONDS = 10.0
PLAYGY_SIGNED_URL_TIMEOUT_SECONDS = 8.0
_PROXY_WRITE_TIMEOUT_SECONDS = 30.0
_PROXY_POOL_TIMEOUT_SECONDS = 5.0
_SIGNED_MEDIA_MAX_REDIRECTS = 5
_SIGNED_MEDIA_PROBE_MAX_CONCURRENCY = 32
_SIGNED_MEDIA_PROBE_QUEUE_TIMEOUT_SECONDS = 1.0
_SIGNED_MEDIA_PROBE_READ_TIMEOUT_SECONDS = 10.0
_SIGNED_MEDIA_PROBE_TOTAL_TIMEOUT_SECONDS = 10.0
_NATIVE_ANDROID_AUTH_MAX_CONCURRENCY = 16
_native_android_auth_capacity = threading.BoundedSemaphore(
    _NATIVE_ANDROID_AUTH_MAX_CONCURRENCY
)
# 跨所有媒体反代 runtime 共享的实际阻塞任务容量。旧 runtime 即使仍有
# 无法取消的 DNS/SDK 调用，新 runtime 也不能绕过上限继续堆积线程。
_signed_media_probe_worker_capacity = threading.BoundedSemaphore(
    _SIGNED_MEDIA_PROBE_MAX_CONCURRENCY
)
_signed_media_probe_executor = ThreadPoolExecutor(
    max_workers=_SIGNED_MEDIA_PROBE_MAX_CONCURRENCY,
    thread_name_prefix="media-proxy-head",
)
_SIGNED_MEDIA_REQUEST_HEADERS = {
    "accept",
    "if-range",
    "range",
    "user-agent",
}
_SIGNED_MEDIA_RESPONSE_HEADERS = {
    "accept-ranges",
    "content-length",
    "content-range",
    "content-type",
    "etag",
    "last-modified",
}
_UPSTREAM_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_MEDIAFLUX_SESSION_COOKIE_NAME = "session"


class ProxyRequestBodyTooLarge(ValueError):
    """反代请求体超过安全上限。"""


class ProxyUpstreamBodyTooLarge(ValueError):
    """需要缓冲处理的上游响应超过运行时上限。"""


class _SignedMediaProbeCapacityError(RuntimeError):
    """原生客户端媒体探测的受控后台工作容量已满。"""


class _WebSocketRedirectBlocked(RuntimeError):
    """上游 WebSocket 握手试图跨跳重定向。"""


async def _reject_websocket_redirect(*_args: Any, **_kwargs: Any) -> None:
    # aiohttp 的 ws_connect 默认会跟随 3xx；重定向后的 IP literal 会绕过
    # 首跳 pinned resolver，因此必须在任何第二跳连接发生前终止。
    raise _WebSocketRedirectBlocked("WebSocket upstream redirects are disabled")


def _websocket_trace_config() -> TraceConfig:
    trace = TraceConfig()
    trace.on_request_redirect.append(_reject_websocket_redirect)
    return trace


def _bounded_megabytes_setting(key: str, default: int, hard_max: int) -> int:
    try:
        configured_mb = int(get(key, str(default)) or default)
    except (TypeError, ValueError):
        configured_mb = default
    return max(1, min(configured_mb, hard_max)) * 1024 * 1024


def _proxy_request_body_limit() -> int:
    return _bounded_megabytes_setting("MEDIA_PROXY_MAX_REQUEST_BODY_MB", 64, 1024)


def _playback_info_response_limit() -> int:
    return _bounded_megabytes_setting("MEDIA_PROXY_MAX_PLAYBACK_INFO_MB", 8, 64)


def _proxy_websocket_message_limit() -> int:
    return _bounded_megabytes_setting("MEDIA_PROXY_MAX_WEBSOCKET_MESSAGE_MB", 4, 64)


def _upstream_timeout() -> httpx.Timeout:
    # 视频响应读取必须允许长时间持续；只限制建连、写入和连接池等待。
    return httpx.Timeout(
        connect=_PROXY_CONNECT_TIMEOUT_SECONDS,
        read=None,
        write=_PROXY_WRITE_TIMEOUT_SECONDS,
        pool=_PROXY_POOL_TIMEOUT_SECONDS,
    )


def _signed_media_timeout(method: str) -> httpx.Timeout:
    if str(method or "GET").upper() != "HEAD":
        return _upstream_timeout()
    # HEAD 只用于播放前探测，不能像长视频流一样无限等待响应头。
    return httpx.Timeout(
        connect=_PROXY_CONNECT_TIMEOUT_SECONDS,
        read=_SIGNED_MEDIA_PROBE_READ_TIMEOUT_SECONDS,
        write=_PROXY_WRITE_TIMEOUT_SECONDS,
        pool=_PROXY_POOL_TIMEOUT_SECONDS,
    )


async def _read_proxy_request_body(request: Request) -> bytes:
    limit = _proxy_request_body_limit()
    raw_length = str(request.headers.get("content-length", "") or "").strip()
    if raw_length:
        try:
            declared_length = int(raw_length)
        except ValueError as exc:
            raise ProxyRequestBodyTooLarge from exc
        if declared_length < 0 or declared_length > limit:
            raise ProxyRequestBodyTooLarge
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise ProxyRequestBodyTooLarge
        body.extend(chunk)
    return bytes(body)


async def _read_bounded_upstream_body(response: httpx.Response, limit: int) -> bytes:
    """有界读取需要改写的控制面响应；媒体流仍保持零拷贝式转发。"""
    raw_length = str(response.headers.get("content-length", "") or "").strip()
    if raw_length:
        try:
            declared_length = int(raw_length)
        except ValueError as exc:
            raise ProxyUpstreamBodyTooLarge from exc
        if declared_length < 0 or declared_length > limit:
            raise ProxyUpstreamBodyTooLarge
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > limit:
            raise ProxyUpstreamBodyTooLarge
        body.extend(chunk)
    return bytes(body)


def _register_signed_url_cache(instance_id: int, cache: SignedUrlCache) -> None:
    with _signed_url_caches_lock:
        _signed_url_caches[int(instance_id)] = cache


def _release_signed_url_cache(instance_id: int, cache: SignedUrlCache) -> int:
    """清理指定 runtime 的缓存；仅在仍为当前代际时移除全局映射。"""
    removed = cache.entry_count
    cache.clear()
    with _signed_url_caches_lock:
        if _signed_url_caches.get(int(instance_id)) is cache:
            _signed_url_caches.pop(int(instance_id), None)
    return removed


def classify_proxy_route(path: str, method: str = "GET") -> str:
    normalized = "/" + str(path or "").lstrip("/")
    if _PLAYGY_RE.match(normalized):
        return "guangya_direct"
    if _PLAYBACK_INFO_RE.match(normalized):
        return "playback_info"
    if _HLS_PATH_RE.search(normalized):
        return "upstream_hls"
    if _STREAM_RE.match(normalized):
        return "stream"
    return "upstream"


def _range_diagnostic(value: str) -> str:
    """只记录 Range 形态，不记录可能含敏感偏移信息的原始值。"""
    text = str(value or "").strip()
    if not text:
        return "none"
    if re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", text, re.IGNORECASE):
        return "single"
    return "invalid"


def clear_signed_url_cache(instance_id: int | None = None) -> int:
    with _signed_url_caches_lock:
        if instance_id is None:
            caches = list(_signed_url_caches.values())
        else:
            cache = _signed_url_caches.get(int(instance_id))
            caches = [cache] if cache else []
        removed = sum(cache.entry_count for cache in caches)
        for cache in caches:
            cache.clear()
        return removed


def signed_url_cache_metrics(instance_id: int | None = None) -> dict:
    with _signed_url_caches_lock:
        if instance_id is not None:
            cache = _signed_url_caches.get(int(instance_id))
            return cache.metrics() if cache else {
                "hits": 0, "misses": 0, "expired": 0, "fetches": 0,
                "failures": 0, "evictions": 0, "entries": 0, "capacity": 2048,
            }
        instances = {
            str(key): cache.metrics()
            for key, cache in sorted(_signed_url_caches.items())
        }
        keys = ("hits", "misses", "expired", "fetches", "failures", "evictions", "entries", "capacity")
        return {
            **{key: sum(int(value.get(key, 0)) for value in instances.values()) for key in keys},
            "instances": instances,
        }


def _resolved_instance(instance_id: int) -> dict[str, Any] | None:
    row = database.get_media_proxy_instance(instance_id)
    if not row:
        return None
    return resolve_proxy_instance(row)


_COMPACT_MEDIA_UUID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_CANONICAL_MEDIA_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$"
)


def _normalized_media_identifier(value: Any) -> str:
    """统一 Jellyfin UUID 的标准带连字符/无连字符表示，不改写普通标识。"""
    raw = str(value or "").strip()
    if _COMPACT_MEDIA_UUID_RE.fullmatch(raw):
        return raw.casefold()
    if _CANONICAL_MEDIA_UUID_RE.fullmatch(raw):
        return raw.replace("-", "").casefold()
    return raw


def _media_identifier_variants(value: Any) -> tuple[str, ...]:
    """返回数据库兼容查询需要的原始、compact、canonical UUID 形式。"""
    raw = str(value or "").strip()
    normalized = _normalized_media_identifier(raw)
    if not _COMPACT_MEDIA_UUID_RE.fullmatch(normalized):
        return (raw,)
    canonical = (
        f"{normalized[:8]}-{normalized[8:12]}-{normalized[12:16]}-"
        f"{normalized[16:20]}-{normalized[20:]}"
    )
    return tuple(
        dict.fromkeys(
            (raw, normalized, canonical, normalized.upper(), canonical.upper())
        )
    )


@dataclass
class _DynamicGuangYaMapping:
    file_id: str
    expires_at: float
    max_expires_at: float


class DynamicGuangYaMappings:
    """按代理实例隔离的短时光鸭来源映射。"""

    def __init__(
        self,
        ttl_seconds: float = _PLAYBACK_AUTHORIZATION_TTL_SECONDS,
        max_ttl_seconds: float = _PLAYBACK_AUTHORIZATION_MAX_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 4096,
    ) -> None:
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._max_ttl_seconds = max(
            self._ttl_seconds, float(max_ttl_seconds)
        )
        self._clock = clock
        self._max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[
            tuple[int, str, str], _DynamicGuangYaMapping
        ] = OrderedDict()
        self._lock = threading.RLock()

    def register(
        self,
        instance_id: int,
        item_id: str,
        source_id: str,
        file_id: str,
    ) -> None:
        normalized_item = _normalized_media_identifier(item_id)
        normalized_file = str(file_id or "").strip()
        if not normalized_item or not normalized_file:
            return
        key = (
            int(instance_id),
            normalized_item,
            _normalized_media_identifier(source_id),
        )
        with self._lock:
            now = self._clock()
            self._prune_expired_locked(now)
            self._entries.pop(key, None)
            self._entries[key] = _DynamicGuangYaMapping(
                file_id=normalized_file,
                expires_at=now + self._ttl_seconds,
                max_expires_at=now + self._max_ttl_seconds,
            )
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def _prune_expired_locked(self, now: float) -> None:
        expired = [
            key for key, entry in self._entries.items()
            if entry.expires_at <= now or entry.max_expires_at <= now
        ]
        for key in expired:
            self._entries.pop(key, None)

    def _touch_locked(
        self,
        key: tuple[int, str, str],
        entry: _DynamicGuangYaMapping,
        now: float,
    ) -> _DynamicGuangYaMapping:
        entry.expires_at = min(
            entry.max_expires_at, now + self._ttl_seconds
        )
        self._entries.move_to_end(key)
        return entry

    def get(self, instance_id: int, item_id: str, source_id: str = "") -> str | None:
        now = self._clock()
        instance_key = int(instance_id)
        item_key = _normalized_media_identifier(item_id)
        source_key = _normalized_media_identifier(source_id)
        with self._lock:
            self._prune_expired_locked(now)
            if source_key:
                key = (instance_key, item_key, source_key)
                entry = self._entries.get(key)
                if entry:
                    return self._touch_locked(key, entry, now).file_id
                return None

            matching = [
                (key, entry.file_id)
                for key, entry in self._entries.items()
                if key[0] == instance_key and key[1] == item_key
            ]
            candidates = {file_id for _, file_id in matching}
            if len(candidates) == 1:
                for key, _ in matching:
                    entry = self._entries.get(key)
                    if entry is not None:
                        self._touch_locked(key, entry, now)
                return next(iter(candidates))
            return None

    @property
    def entry_count(self) -> int:
        with self._lock:
            self._prune_expired_locked(self._clock())
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class SignedUrlCache:
    """签名 URL 短缓存；失败结果不缓存，同一文件并发请求只取一次。"""

    def __init__(
        self,
        ttl_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        lock_stripes: int = 64,
        max_entries: int = 2048,
        expiry_margin_seconds: float = 10,
    ) -> None:
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._clock = clock
        self._wall_clock = wall_clock
        self._expiry_margin_seconds = max(0.0, float(expiry_margin_seconds))
        self._max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[tuple[str, str, str], tuple[str, float]] = OrderedDict()
        stripe_count = max(1, int(lock_stripes))
        self._locks = tuple(asyncio.Lock() for _ in range(stripe_count))
        self._sync_locks = tuple(threading.Lock() for _ in range(stripe_count))
        self._guard = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._expired = 0
        self._fetches = 0
        self._failures = 0
        self._evictions = 0

    @staticmethod
    def _key(file_id: str, scope: str, user_agent: str, ua_bound: bool) -> tuple[str, str, str]:
        return (
            str(scope or "").strip(),
            str(file_id or "").strip(),
            str(user_agent or "").strip() if ua_bound else "",
        )

    def _provider_expiry(self, url: str) -> float | None:
        """解析常见签名 URL 的绝对到期时间；不保存或输出 URL。"""
        try:
            query = {key.lower(): value for key, value in parse_qsl(urlsplit(url).query)}
            for key in ("expires", "x-oss-expires", "oss-expires", "expiry", "exp"):
                value = query.get(key)
                if value:
                    parsed = float(value)
                    if parsed > self._wall_clock():
                        return parsed
            amz_date = query.get("x-amz-date")
            amz_ttl = query.get("x-amz-expires")
            if amz_date and amz_ttl:
                issued = datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                ).timestamp()
                return issued + float(amz_ttl)
        except (TypeError, ValueError, OverflowError):
            return None
        return None

    def _cache_expiry(self, url: str, now: float) -> float:
        expiry = now + self._ttl_seconds
        provider_expiry = self._provider_expiry(url)
        if provider_expiry is not None:
            remaining = provider_expiry - self._wall_clock() - self._expiry_margin_seconds
            expiry = min(expiry, now + max(0.0, remaining))
        return expiry

    def _prune_expired_locked(self, now: float) -> None:
        expired = [
            key for key, entry in self._entries.items()
            if entry[1] <= now
        ]
        for key in expired:
            self._entries.pop(key, None)
        self._expired += len(expired)

    def _cached(self, key: tuple[str, str, str]) -> str | None:
        now = self._clock()
        with self._guard:
            entry = self._entries.get(key)
            if entry and entry[1] > now:
                self._entries.move_to_end(key)
                return entry[0]
            if entry:
                self._expired += 1
            self._entries.pop(key, None)
        return None

    def _file_lock(self, key: tuple[str, str, str]) -> asyncio.Lock:
        return self._locks[hash(key) % len(self._locks)]

    def _sync_file_lock(self, key: tuple[str, str, str]) -> threading.Lock:
        return self._sync_locks[hash(key) % len(self._sync_locks)]

    def _remember(self, key: tuple[str, str, str], url: str) -> bool:
        with self._guard:
            now = self._clock()
            self._prune_expired_locked(now)
            expiry = self._cache_expiry(str(url), now)
            if expiry <= now:
                return False
            self._entries.pop(key, None)
            self._entries[key] = (str(url), expiry)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1
        return True

    @property
    def lock_count(self) -> int:
        return len(self._locks)

    async def get_or_fetch(
        self,
        file_id: str,
        fetcher: Callable[[], Awaitable[str | None]],
        *,
        scope: str = "",
        user_agent: str = "",
        ua_bound: bool = False,
    ) -> str | None:
        result = await self.get_or_fetch_result(
            file_id,
            fetcher,
            scope=scope,
            user_agent=user_agent,
            ua_bound=ua_bound,
        )
        return result.url

    async def get_or_fetch_result(
        self,
        file_id: str,
        fetcher: Callable[[], Awaitable[str | None]],
        *,
        scope: str = "",
        user_agent: str = "",
        ua_bound: bool = False,
    ) -> "SignedUrlCacheResult":
        normalized = str(file_id or "").strip()
        if not normalized:
            return SignedUrlCacheResult(None, False)
        key = self._key(normalized, scope, user_agent, ua_bound)
        cached = self._cached(key)
        if cached:
            with self._guard:
                self._hits += 1
            return SignedUrlCacheResult(cached, True)
        with self._guard:
            self._misses += 1
        async with self._file_lock(key):
            cached = self._cached(key)
            if cached:
                with self._guard:
                    self._hits += 1
                return SignedUrlCacheResult(cached, True)
            with self._guard:
                self._fetches += 1
            try:
                url = await fetcher()
            except Exception:
                with self._guard:
                    self._failures += 1
                raise
            if url:
                self._remember(key, str(url))
            else:
                with self._guard:
                    self._failures += 1
            return SignedUrlCacheResult(str(url) if url else None, False)

    def get_or_fetch_sync_result(
        self,
        file_id: str,
        fetcher: Callable[[], str | None],
        *,
        scope: str = "",
        user_agent: str = "",
        ua_bound: bool = False,
    ) -> "SignedUrlCacheResult":
        """同步路由使用的短缓存入口，保持与异步入口相同的单飞语义。"""
        normalized = str(file_id or "").strip()
        if not normalized:
            return SignedUrlCacheResult(None, False)
        key = self._key(normalized, scope, user_agent, ua_bound)
        cached = self._cached(key)
        if cached:
            with self._guard:
                self._hits += 1
            return SignedUrlCacheResult(cached, True)
        with self._guard:
            self._misses += 1
        with self._sync_file_lock(key):
            cached = self._cached(key)
            if cached:
                with self._guard:
                    self._hits += 1
                return SignedUrlCacheResult(cached, True)
            with self._guard:
                self._fetches += 1
            try:
                url = fetcher()
            except Exception:
                with self._guard:
                    self._failures += 1
                raise
            if url:
                self._remember(key, str(url))
            else:
                with self._guard:
                    self._failures += 1
            return SignedUrlCacheResult(str(url) if url else None, False)

    @property
    def entry_count(self) -> int:
        with self._guard:
            self._prune_expired_locked(self._clock())
            return len(self._entries)

    def clear(self) -> None:
        with self._guard:
            self._entries.clear()

    def clear_scope(self, scope_prefix: str) -> int:
        prefix = str(scope_prefix or "")
        with self._guard:
            keys = [key for key in self._entries if key[0].startswith(prefix)]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)

    def metrics(self) -> dict[str, int]:
        with self._guard:
            self._prune_expired_locked(self._clock())
            return {
                "hits": self._hits,
                "misses": self._misses,
                "expired": self._expired,
                "fetches": self._fetches,
                "failures": self._failures,
                "evictions": self._evictions,
                "entries": len(self._entries),
                "capacity": self._max_entries,
            }


@dataclass(frozen=True)
class SignedUrlCacheResult:
    url: str | None
    cache_hit: bool


class BrowserDirectTargetCache:
    """记录已完成公网目标校验的 signed URL，避免缓存命中仍重复 DNS。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = 60.0,
        max_entries: int = 2048,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.RLock()

    def contains(self, url: str) -> bool:
        key = str(url or "")
        now = self._clock()
        with self._lock:
            expiry = self._entries.get(key, 0.0)
            if expiry > now:
                self._entries.move_to_end(key)
                return True
            self._entries.pop(key, None)
            return False

    def remember(self, url: str) -> None:
        key = str(url or "")
        if not key:
            return
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = self._clock() + self._ttl_seconds
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


@dataclass
class _ItemBindingScope:
    binding_signature: str
    expires_at: float
    max_expires_at: float


class ItemLevelBindingScopes:
    """只允许已在单 MediaSource PlaybackInfo 中确认过的 Item 级绑定。"""

    def __init__(
        self,
        ttl_seconds: float = _PLAYBACK_AUTHORIZATION_TTL_SECONDS,
        max_ttl_seconds: float = _PLAYBACK_AUTHORIZATION_MAX_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 4096,
    ) -> None:
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._max_ttl_seconds = max(
            self._ttl_seconds, float(max_ttl_seconds)
        )
        self._clock = clock
        self._max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[
            tuple[str, int, str, str], _ItemBindingScope
        ] = OrderedDict()
        self._lock = threading.RLock()

    def _prune_expired_locked(self, now: float) -> None:
        expired = [
            key for key, entry in self._entries.items()
            if entry.expires_at <= now or entry.max_expires_at <= now
        ]
        for key in expired:
            self._entries.pop(key, None)

    def _touch_locked(
        self,
        key: tuple[str, int, str, str],
        entry: _ItemBindingScope,
        now: float,
    ) -> None:
        entry.expires_at = min(
            entry.max_expires_at, now + self._ttl_seconds
        )
        self._entries.move_to_end(key)

    def register(
        self,
        instance_id: int,
        item_id: str,
        source_id: str,
        binding_signature: str,
        auth_scope: str = "",
    ) -> None:
        key = (
            str(auth_scope or ""),
            int(instance_id),
            _normalized_media_identifier(item_id),
            _normalized_media_identifier(source_id),
        )
        with self._lock:
            now = self._clock()
            self._prune_expired_locked(now)
            self._entries.pop(key, None)
            self._entries[key] = _ItemBindingScope(
                binding_signature=str(binding_signature),
                expires_at=now + self._ttl_seconds,
                max_expires_at=now + self._max_ttl_seconds,
            )
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def matches(
        self,
        instance_id: int,
        item_id: str,
        source_id: str,
        binding_signature: str,
        auth_scope: str = "",
    ) -> bool:
        key = (
            str(auth_scope or ""),
            int(instance_id),
            _normalized_media_identifier(item_id),
            _normalized_media_identifier(source_id),
        )
        with self._lock:
            now = self._clock()
            self._prune_expired_locked(now)
            entry = self._entries.get(key)
            if entry and entry.binding_signature == str(binding_signature):
                self._touch_locked(key, entry, now)
                return True
            return False

    def matches_item(
        self,
        instance_id: int,
        item_id: str,
        binding_signature: str,
        auth_scope: str = "",
    ) -> bool:
        with self._lock:
            now = self._clock()
            self._prune_expired_locked(now)
            matches = [
                key for key, entry in self._entries.items()
                if key[0] == str(auth_scope or "")
                and key[1] == int(instance_id)
                and key[2] == _normalized_media_identifier(item_id)
                and entry.binding_signature == str(binding_signature)
            ]
            if len(matches) == 1:
                key = matches[0]
                entry = self._entries.get(key)
                if entry is not None:
                    self._touch_locked(key, entry, now)
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_dynamic_guangya_mappings = DynamicGuangYaMappings()
_item_level_binding_scopes = ItemLevelBindingScopes()

@dataclass
class _PlaybackGrant:
    source_type: str
    file_id: str
    binding_signature: str
    expires_at: float
    max_expires_at: float


class PlaybackGrantRegistry:
    """按不可逆认证 scope 隔离的短时播放授权；不保存原始凭据。"""

    def __init__(self, ttl_seconds: float = _PLAYBACK_AUTHORIZATION_TTL_SECONDS,
                 max_ttl_seconds: float = _PLAYBACK_AUTHORIZATION_MAX_TTL_SECONDS,
                 max_entries: int = 8192,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._max_ttl_seconds = max(
            self._ttl_seconds, float(max_ttl_seconds)
        )
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._entries: OrderedDict[tuple[str, int, str, str], _PlaybackGrant] = OrderedDict()
        self._lock = threading.RLock()

    def _prune_locked(self, now: float) -> None:
        for key in [
            key for key, value in self._entries.items()
            if value.expires_at <= now or value.max_expires_at <= now
        ]:
            self._entries.pop(key, None)

    def _touch_locked(
        self,
        key: tuple[str, int, str, str],
        grant: _PlaybackGrant,
        now: float,
    ) -> None:
        grant.expires_at = min(
            grant.max_expires_at, now + self._ttl_seconds
        )
        self._entries.move_to_end(key)

    def register(self, auth_scope: str, instance_id: int, item_id: str, source_id: str,
                 *, source_type: str, file_id: str = "", binding_signature: str = "") -> None:
        if not auth_scope:
            return
        key = (
            auth_scope,
            int(instance_id),
            _normalized_media_identifier(item_id),
            _normalized_media_identifier(source_id),
        )
        with self._lock:
            now = self._clock(); self._prune_locked(now); self._entries.pop(key, None)
            self._entries[key] = _PlaybackGrant(
                str(source_type),
                str(file_id or ""),
                str(binding_signature or ""),
                now + self._ttl_seconds,
                now + self._max_ttl_seconds,
            )
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def matches(self, auth_scope: str, instance_id: int, item_id: str, source_id: str,
                *, file_id: str = "", binding_signature: str = "") -> bool:
        if not auth_scope:
            return False
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            normalized_item = _normalized_media_identifier(item_id)
            normalized_source = _normalized_media_identifier(source_id)
            keys = [(auth_scope, int(instance_id), normalized_item, normalized_source)]
            if not normalized_source:
                keys = [
                    key for key in self._entries
                    if key[:3] == (auth_scope, int(instance_id), normalized_item)
                ]
            matches = []
            for key in keys:
                grant = self._entries.get(key)
                if not grant: continue
                if file_id and grant.file_id != str(file_id): continue
                if binding_signature and grant.binding_signature != str(binding_signature): continue
                matches.append(key)
            if len(matches) == 1:
                key = matches[0]
                grant = self._entries.get(key)
                if grant is not None:
                    self._touch_locked(key, grant, now)
                return True
            return False

    def allows_file(self, auth_scope: str, instance_id: int, file_id: str) -> bool:
        if not auth_scope or not file_id: return False
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            matches = [
                (key, grant)
                for key, grant in self._entries.items()
                if key[0] == auth_scope
                and key[1] == int(instance_id)
                and grant.file_id == str(file_id)
            ]
            if len(matches) == 1:
                key, grant = matches[0]
                self._touch_locked(key, grant, now)
                return True
            return False

    def clear(self) -> None:
        with self._lock: self._entries.clear()


_playback_grants = PlaybackGrantRegistry()


def _playback_media_name(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("Name", "ItemName", "Title"):
        name = _safe_media_name(payload.get(key))
        if name:
            return name
    media_sources = payload.get("MediaSources")
    if not isinstance(media_sources, list):
        return ""
    for source in media_sources:
        if not isinstance(source, dict):
            continue
        name = _safe_media_name(source.get("Path"), path_value=True)
        if name:
            return name
    for source in media_sources:
        if not isinstance(source, dict):
            continue
        name = _safe_media_name(source.get("Name"))
        if name:
            return name
    return ""


def _playback_media_source_names(payload: Any) -> dict[str, str]:
    """提取 PlaybackInfo 中可安全展示、且与 MediaSource 精确绑定的名称。"""
    if not isinstance(payload, dict):
        return {}
    shared_name = ""
    for key in ("Name", "ItemName", "Title"):
        shared_name = _safe_media_name(payload.get(key))
        if shared_name:
            break
    media_sources = payload.get("MediaSources")
    if not isinstance(media_sources, list):
        return {}
    result: dict[str, str] = {}
    conflicted: set[str] = set()
    for source in media_sources:
        if not isinstance(source, dict):
            continue
        source_id = _normalized_media_identifier(source.get("Id"))
        if not source_id or source_id in conflicted:
            continue
        source_name = shared_name
        if not source_name:
            source_name = _safe_media_name(source.get("Path"), path_value=True)
        if not source_name:
            source_name = _safe_media_name(source.get("Name"))
        if not source_name:
            continue
        previous = result.get(source_id)
        if previous and previous != source_name:
            # 重复 SourceId 却给出不同名称时不建立继承上下文，避免串标题。
            result.pop(source_id, None)
            conflicted.add(source_id)
            continue
        result[source_id] = source_name
    return result


def _playback_media_source_ids(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    media_sources = payload.get("MediaSources")
    if not isinstance(media_sources, list):
        return ()
    return tuple(dict.fromkeys(
        source_id
        for source_id in (
            _normalized_media_identifier(source.get("Id"))
            for source in media_sources
            if isinstance(source, dict)
        )
        if source_id
    ))


@dataclass
class _PlaybackSessionLink:
    token: str
    auth_scope: str
    instance_id: int
    item_id: str
    source_id: str
    file_id: str
    media_name: str
    upstream_session_token: str
    expires_at: float
    capability_expires_at: float
    browser_relay: bool = False
    native_cross_protocol_relay: bool = False
    native_signed_media_relay: bool = False
    native_client_fingerprint: str = ""
    native_verified_auth_scope: str = ""


class PlaybackSessionRegistry:
    """把一次 PlaybackInfo/stream/HLS 请求链关联为不含原始凭据的短时会话。"""

    def __init__(self, ttl_seconds: float = 30 * 60,
                 capability_ttl_seconds: float = _PLAYBACK_AUTHORIZATION_TTL_SECONDS,
                 capability_max_ttl_seconds: float = _PLAYBACK_AUTHORIZATION_MAX_TTL_SECONDS,
                 max_entries: int = 8192,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._capability_ttl_seconds = max(1.0, float(capability_ttl_seconds))
        self._capability_max_ttl_seconds = max(
            self._capability_ttl_seconds,
            float(capability_max_ttl_seconds),
        )
        self._max_entries = max(1, int(max_entries))
        self._max_capabilities = max(4, self._max_entries * 4)
        self._clock = clock
        self._entries: OrderedDict[tuple[str, int, str], _PlaybackSessionLink] = OrderedDict()
        self._capability_index: OrderedDict[
            tuple[int, str], tuple[tuple[str, int, str], float, float]
        ] = OrderedDict()
        self._entry_capabilities: dict[
            tuple[str, int, str], set[tuple[int, str]]
        ] = {}
        self._capability_browser_direct_redirect_signatures: dict[
            tuple[int, str], tuple[str, ...]
        ] = {}
        self._upstream_session_index: dict[
            tuple[str, int, str], tuple[str, int, str]
        ] = {}
        self._media_name_index: OrderedDict[
            tuple[str, int, str, str], tuple[str, float]
        ] = OrderedDict()
        self._lock = threading.RLock()

    @staticmethod
    def _token(value: str) -> str:
        token = str(value or "").strip()
        return token if 0 < len(token) <= 256 else ""

    def _set_upstream_session_locked(
        self,
        key: tuple[str, int, str],
        entry: _PlaybackSessionLink,
        token: str,
    ) -> None:
        normalized_token = self._token(token)
        if not normalized_token:
            return
        previous = entry.upstream_session_token
        if previous and previous != normalized_token:
            previous_key = (entry.auth_scope, entry.instance_id, previous)
            if self._upstream_session_index.get(previous_key) == key:
                self._upstream_session_index.pop(previous_key, None)
        entry.upstream_session_token = normalized_token
        self._upstream_session_index[
            (entry.auth_scope, entry.instance_id, normalized_token)
        ] = key

    def _refresh_capability_expiry_locked(
        self, key: tuple[str, int, str]
    ) -> None:
        entry = self._entries.get(key)
        if entry is None:
            return
        entry.capability_expires_at = max(
            (
                self._capability_index[capability_key][1]
                for capability_key in self._entry_capabilities.get(key, set())
                if capability_key in self._capability_index
            ),
            default=0.0,
        )

    def _remove_capability_locked(self, capability_key: tuple[int, str]) -> None:
        linked = self._capability_index.pop(capability_key, None)
        self._capability_browser_direct_redirect_signatures.pop(
            capability_key, None
        )
        if linked is None:
            return
        key, _expires_at, _max_expires_at = linked
        capabilities = self._entry_capabilities.get(key)
        if capabilities is not None:
            capabilities.discard(capability_key)
            if not capabilities:
                self._entry_capabilities.pop(key, None)
        self._refresh_capability_expiry_locked(key)

    def _register_capability_locked(
        self,
        key: tuple[str, int, str],
        entry: _PlaybackSessionLink,
        token: str,
        expires_at: float,
        max_expires_at: float,
        browser_direct_redirect_signatures: tuple[str, ...] = (),
    ) -> None:
        capability_key = (entry.instance_id, token)
        if capability_key in self._capability_index:
            self._remove_capability_locked(capability_key)
        self._capability_index[capability_key] = (
            key,
            min(expires_at, max_expires_at),
            max_expires_at,
        )
        self._entry_capabilities.setdefault(key, set()).add(capability_key)
        normalized_signatures = tuple(
            dict.fromkeys(
                signature
                for signature in (
                    str(value or "").strip()
                    for value in browser_direct_redirect_signatures
                )
                if signature
            )
        )
        if normalized_signatures:
            self._capability_browser_direct_redirect_signatures[
                capability_key
            ] = normalized_signatures
        else:
            self._capability_browser_direct_redirect_signatures.pop(
                capability_key, None
            )
        entry.capability_expires_at = max(entry.capability_expires_at, expires_at)
        while len(self._capability_index) > self._max_capabilities:
            oldest = next(iter(self._capability_index))
            self._remove_capability_locked(oldest)

    def _prune_capabilities_locked(self, now: float) -> None:
        for capability_key in [
            capability_key
            for capability_key, (
                _key, expires_at, max_expires_at
            ) in self._capability_index.items()
            if expires_at <= now or max_expires_at <= now
        ]:
            self._remove_capability_locked(capability_key)

    def _remove_locked(self, key: tuple[str, int, str]) -> _PlaybackSessionLink | None:
        entry = self._entries.pop(key, None)
        for capability_key in list(self._entry_capabilities.pop(key, set())):
            self._capability_index.pop(capability_key, None)
            self._capability_browser_direct_redirect_signatures.pop(
                capability_key, None
            )
        if entry and entry.upstream_session_token:
            upstream_key = (
                entry.auth_scope, entry.instance_id, entry.upstream_session_token
            )
            if self._upstream_session_index.get(upstream_key) == key:
                self._upstream_session_index.pop(upstream_key, None)
        return entry

    def _prune_locked(self, now: float) -> None:
        for key in [
            key for key, value in self._entries.items()
            if value.expires_at <= now
        ]:
            self._remove_locked(key)
        self._prune_media_names_locked(now)

    def _prune_media_names_locked(self, now: float) -> None:
        for key in [
            key for key, (_media_name, expires_at) in self._media_name_index.items()
            if expires_at <= now
        ]:
            self._media_name_index.pop(key, None)

    @staticmethod
    def _media_name_key(
        auth_scope: str,
        instance_id: int,
        item_id: str,
        source_id: str,
    ) -> tuple[str, int, str, str] | None:
        normalized_scope = str(auth_scope or "")
        normalized_item = _normalized_media_identifier(item_id)
        normalized_source = _normalized_media_identifier(source_id)
        if not normalized_scope or not normalized_item or not normalized_source:
            return None
        return (
            normalized_scope,
            int(instance_id),
            normalized_item,
            normalized_source,
        )

    def _remember_media_name_locked(
        self,
        auth_scope: str,
        instance_id: int,
        item_id: str,
        source_id: str,
        media_name: str,
        now: float,
    ) -> None:
        key = self._media_name_key(auth_scope, instance_id, item_id, source_id)
        safe_name = _safe_media_name(media_name)
        if key is None or not safe_name:
            return
        self._media_name_index[key] = (safe_name, now + self._ttl_seconds)
        self._media_name_index.move_to_end(key)
        while len(self._media_name_index) > self._max_entries:
            self._media_name_index.popitem(last=False)

    def _resolve_media_name_locked(
        self,
        auth_scope: str,
        instance_id: int,
        item_id: str,
        source_ids: tuple[str, ...],
        now: float,
    ) -> str:
        self._prune_media_names_locked(now)
        normalized_sources = tuple(dict.fromkeys(
            source_id
            for source_id in (
                _normalized_media_identifier(value) for value in source_ids
            )
            if source_id
        ))
        if not normalized_sources:
            return ""
        resolved: list[tuple[tuple[str, int, str, str], str]] = []
        for source_id in normalized_sources:
            key = self._media_name_key(
                auth_scope, instance_id, item_id, source_id
            )
            if key is None:
                return ""
            cached = self._media_name_index.get(key)
            if cached is None:
                return ""
            resolved.append((key, cached[0]))
        names = {media_name for _key, media_name in resolved}
        if len(names) != 1:
            return ""
        for key, media_name in resolved:
            self._media_name_index[key] = (
                media_name, now + self._ttl_seconds
            )
            self._media_name_index.move_to_end(key)
        return resolved[0][1]

    def remember_media_names(
        self,
        auth_scope: str,
        instance_id: int,
        item_id: str,
        media_names: dict[str, str],
    ) -> None:
        """保存短时标题上下文；只接受认证域内的精确 item/source。"""
        if not auth_scope or not item_id or not media_names:
            return
        with self._lock:
            now = self._clock()
            self._prune_media_names_locked(now)
            for source_id, media_name in media_names.items():
                self._remember_media_name_locked(
                    auth_scope,
                    instance_id,
                    item_id,
                    source_id,
                    media_name,
                    now,
                )

    def resolve_media_name(
        self,
        auth_scope: str,
        instance_id: int,
        item_id: str,
        source_ids: tuple[str, ...],
    ) -> str:
        """按认证域、实例、媒体和来源精确恢复近期安全标题。"""
        if not auth_scope or not item_id or not source_ids:
            return ""
        with self._lock:
            return self._resolve_media_name_locked(
                auth_scope,
                instance_id,
                item_id,
                source_ids,
                self._clock(),
            )

    def _enforce_capacity_locked(self) -> None:
        while len(self._entries) > self._max_entries:
            key = next(iter(self._entries))
            self._remove_locked(key)

    def _touch_locked(
        self,
        key: tuple[str, int, str],
        entry: _PlaybackSessionLink,
        *,
        item_id: str = "",
        source_id: str = "",
        file_id: str = "",
        media_name: str = "",
        upstream_session_token: str = "",
        browser_relay: bool | None = None,
        native_cross_protocol_relay: bool | None = None,
        native_signed_media_relay: bool | None = None,
        native_client_fingerprint: str = "",
        native_verified_auth_scope: str = "",
    ) -> _PlaybackSessionLink:
        now = self._clock()
        normalized_item = (
            _normalized_media_identifier(item_id) if item_id else entry.item_id
        )
        normalized_source = (
            _normalized_media_identifier(source_id)
            if source_id else entry.source_id
        )
        identity_changed = bool(
            (
                item_id
                and entry.item_id
                and entry.item_id != normalized_item
            )
            or (
                source_id
                and entry.source_id
                and entry.source_id != normalized_source
            )
        )
        if item_id:
            entry.item_id = normalized_item
        if source_id:
            entry.source_id = normalized_source
        if file_id:
            entry.file_id = str(file_id)
        if media_name:
            entry.media_name = str(media_name)
        elif identity_changed:
            # 同 token 切换 item/source 时，旧标题不能污染新的精确来源键。
            entry.media_name = ""
        if entry.item_id and entry.source_id:
            if not entry.media_name:
                entry.media_name = self._resolve_media_name_locked(
                    entry.auth_scope,
                    entry.instance_id,
                    entry.item_id,
                    (entry.source_id,),
                    now,
                )
            if entry.media_name:
                self._remember_media_name_locked(
                    entry.auth_scope,
                    entry.instance_id,
                    entry.item_id,
                    entry.source_id,
                    entry.media_name,
                    now,
                )
        if upstream_session_token:
            self._set_upstream_session_locked(key, entry, upstream_session_token)
        if browser_relay is not None:
            entry.browser_relay = bool(browser_relay)
        if native_cross_protocol_relay is not None:
            entry.native_cross_protocol_relay = bool(
                native_cross_protocol_relay
            )
        if native_signed_media_relay is not None:
            entry.native_signed_media_relay = bool(native_signed_media_relay)
        if native_client_fingerprint:
            entry.native_client_fingerprint = str(native_client_fingerprint)
        if native_verified_auth_scope:
            entry.native_verified_auth_scope = str(native_verified_auth_scope)
        entry.expires_at = now + self._ttl_seconds
        self._entries.move_to_end(key)
        return entry

    def begin(
        self,
        auth_scope: str,
        instance_id: int,
        *,
        token: str = "",
        item_id: str = "",
        source_id: str = "",
        file_id: str = "",
        media_name: str = "",
        upstream_session_token: str = "",
        server_capability: bool = False,
        browser_relay: bool = False,
        native_cross_protocol_relay: bool = False,
        native_signed_media_relay: bool = False,
        native_client_fingerprint: str = "",
        native_verified_auth_scope: str = "",
    ) -> _PlaybackSessionLink:
        normalized_token = self._token(token) or secrets.token_urlsafe(24)
        key = (str(auth_scope or ""), int(instance_id), normalized_token)
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            self._prune_capabilities_locked(now)
            entry = self._entries.get(key)
            if entry is None:
                normalized_item = _normalized_media_identifier(item_id)
                normalized_source = _normalized_media_identifier(source_id)
                resolved_media_name = str(media_name or "")
                if not resolved_media_name and normalized_item and normalized_source:
                    resolved_media_name = self._resolve_media_name_locked(
                        key[0],
                        key[1],
                        normalized_item,
                        (normalized_source,),
                        now,
                    )
                entry = _PlaybackSessionLink(
                    token=normalized_token,
                    auth_scope=key[0],
                    instance_id=key[1],
                    item_id=normalized_item,
                    source_id=normalized_source,
                    file_id=str(file_id or ""),
                    media_name=resolved_media_name,
                    upstream_session_token="",
                    expires_at=now + self._ttl_seconds,
                    capability_expires_at=0.0,
                    browser_relay=bool(browser_relay),
                    native_cross_protocol_relay=bool(
                        native_cross_protocol_relay
                    ),
                    native_signed_media_relay=bool(native_signed_media_relay),
                    native_client_fingerprint=str(
                        native_client_fingerprint or ""
                    ),
                    native_verified_auth_scope=str(
                        native_verified_auth_scope or ""
                    ),
                )
                self._entries[key] = entry
                if normalized_item and normalized_source and resolved_media_name:
                    self._remember_media_name_locked(
                        key[0],
                        key[1],
                        normalized_item,
                        normalized_source,
                        resolved_media_name,
                        now,
                    )
                if upstream_session_token:
                    self._set_upstream_session_locked(
                        key, entry, upstream_session_token
                    )
            else:
                self._touch_locked(
                    key,
                    entry,
                    item_id=item_id,
                    source_id=source_id,
                    file_id=file_id,
                    media_name=media_name,
                    upstream_session_token=upstream_session_token,
                    browser_relay=browser_relay,
                    native_cross_protocol_relay=native_cross_protocol_relay,
                    native_signed_media_relay=native_signed_media_relay,
                    native_client_fingerprint=native_client_fingerprint,
                    native_verified_auth_scope=native_verified_auth_scope,
                )
            if server_capability:
                self._register_capability_locked(
                    key,
                    entry,
                    normalized_token,
                    now + self._capability_ttl_seconds,
                    now + self._capability_max_ttl_seconds,
                )
            self._enforce_capacity_locked()
            return entry

    def _upstream_entry_locked(
        self,
        auth_scope: str,
        instance_id: int,
        upstream_session_token: str,
        item_id: str,
    ) -> tuple[tuple[str, int, str], _PlaybackSessionLink] | None:
        alias_key = (auth_scope, instance_id, upstream_session_token)
        key = self._upstream_session_index.get(alias_key)
        if key is None and alias_key in self._entries:
            key = alias_key
        if key is None:
            return None
        entry = self._entries.get(key)
        if entry is None:
            self._upstream_session_index.pop(alias_key, None)
            return None
        if (
            item_id
            and entry.item_id
            and entry.item_id != _normalized_media_identifier(item_id)
        ):
            return None
        return key, entry

    def finalize_capability(
        self,
        auth_scope: str,
        instance_id: int,
        token: str,
        *,
        item_id: str = "",
        media_name: str = "",
        upstream_session_token: str = "",
        browser_relay: bool = False,
        browser_direct_redirect_signatures: tuple[str, ...] = (),
        native_cross_protocol_relay: bool = False,
        native_signed_media_relay: bool = False,
        native_client_fingerprint: str = "",
        native_verified_auth_scope: str = "",
    ) -> _PlaybackSessionLink:
        """成功重写 PlaybackInfo 后签发 capability，并绑定上游播放会话别名。"""
        normalized_scope = str(auth_scope or "")
        normalized_instance = int(instance_id)
        normalized_token = self._token(token) or secrets.token_urlsafe(24)
        normalized_upstream = self._token(upstream_session_token)
        normalized_item = _normalized_media_identifier(item_id)
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            self._prune_capabilities_locked(now)
            resolved = None
            if normalized_upstream:
                resolved = self._upstream_entry_locked(
                    normalized_scope,
                    normalized_instance,
                    normalized_upstream,
                    normalized_item,
                )
            if resolved is not None:
                key, entry = resolved
                self._touch_locked(
                    key,
                    entry,
                    item_id=normalized_item,
                    media_name=media_name,
                    upstream_session_token=normalized_upstream,
                    browser_relay=browser_relay,
                    native_cross_protocol_relay=native_cross_protocol_relay,
                    native_signed_media_relay=native_signed_media_relay,
                    native_client_fingerprint=native_client_fingerprint,
                    native_verified_auth_scope=native_verified_auth_scope,
                )
            else:
                canonical_token = normalized_upstream or normalized_token
                key = (normalized_scope, normalized_instance, canonical_token)
                entry = self._entries.get(key)
                if (
                    entry is not None
                    and normalized_item
                    and entry.item_id
                    and entry.item_id != normalized_item
                ):
                    key = (normalized_scope, normalized_instance, normalized_token)
                    entry = self._entries.get(key)
                    normalized_upstream = ""
                if entry is None:
                    entry = _PlaybackSessionLink(
                        token=key[2],
                        auth_scope=key[0],
                        instance_id=key[1],
                        item_id=normalized_item,
                        source_id="",
                        file_id="",
                        media_name=str(media_name or ""),
                        upstream_session_token="",
                        expires_at=now + self._ttl_seconds,
                        capability_expires_at=0.0,
                        browser_relay=bool(browser_relay),
                        native_cross_protocol_relay=bool(
                            native_cross_protocol_relay
                        ),
                        native_signed_media_relay=bool(
                            native_signed_media_relay
                        ),
                        native_client_fingerprint=str(
                            native_client_fingerprint or ""
                        ),
                        native_verified_auth_scope=str(
                            native_verified_auth_scope or ""
                        ),
                    )
                    self._entries[key] = entry
                else:
                    self._touch_locked(
                        key,
                        entry,
                        item_id=normalized_item,
                        media_name=media_name,
                        browser_relay=browser_relay,
                        native_cross_protocol_relay=native_cross_protocol_relay,
                        native_signed_media_relay=native_signed_media_relay,
                        native_client_fingerprint=native_client_fingerprint,
                        native_verified_auth_scope=native_verified_auth_scope,
                    )
                if normalized_upstream:
                    self._set_upstream_session_locked(
                        key, entry, normalized_upstream
                    )
            self._register_capability_locked(
                key,
                entry,
                normalized_token,
                now + self._capability_ttl_seconds,
                now + self._capability_max_ttl_seconds,
                browser_direct_redirect_signatures,
            )
            self._enforce_capacity_locked()
            return entry

    def capability_allows_browser_direct_redirect(
        self,
        instance_id: int,
        token: str,
        source_signature: str,
    ) -> bool:
        normalized_token = self._token(token)
        normalized_signature = str(source_signature or "").strip()
        if not normalized_token or not normalized_signature:
            return False
        with self._lock:
            self._prune_capabilities_locked(self._clock())
            allowed = self._capability_browser_direct_redirect_signatures.get(
                (int(instance_id), normalized_token), ()
            )
            return any(
                hmac.compare_digest(normalized_signature, signature)
                for signature in allowed
            )

    def resolve_capability(
        self,
        instance_id: int,
        token: str,
        *,
        item_id: str = "",
        source_id: str = "",
        source_signature: str = "",
    ) -> _PlaybackSessionLink | None:
        """用服务端生成的短时 token 恢复认证 scope，不保存或暴露原始凭据。"""
        normalized_instance = int(instance_id)
        normalized_token = self._token(token)
        if not normalized_token:
            return None
        with self._lock:
            now = self._clock()
            capability_key = (normalized_instance, normalized_token)
            linked = self._capability_index.get(capability_key)
            if linked is None:
                return None
            key, capability_expires_at, capability_max_expires_at = linked
            entry = self._entries.get(key)
            if entry is None:
                self._remove_capability_locked(capability_key)
                return None
            if entry.expires_at <= now:
                self._remove_locked(key)
                return None
            if (
                not entry.auth_scope
                or capability_expires_at <= now
                or capability_max_expires_at <= now
            ):
                self._remove_capability_locked(capability_key)
                return None
            if (
                item_id
                and entry.item_id
                and entry.item_id != _normalized_media_identifier(item_id)
            ):
                return None
            expected_source_signature = _playback_source_signature(
                normalized_instance,
                _normalized_media_identifier(item_id or entry.item_id),
                _normalized_media_identifier(source_id),
                normalized_token,
            )
            if not source_signature or not hmac.compare_digest(
                str(source_signature), expected_source_signature
            ):
                return None
            renewed_expires_at = min(
                capability_max_expires_at,
                now + self._capability_ttl_seconds,
            )
            self._capability_index[capability_key] = (
                key,
                renewed_expires_at,
                capability_max_expires_at,
            )
            self._capability_index.move_to_end(capability_key)
            self._refresh_capability_expiry_locked(key)
            return self._touch_locked(
                key, entry, item_id=item_id, source_id=source_id
            )

    def resolve(self, auth_scope: str, instance_id: int, *, token: str = "",
                item_id: str = "", source_id: str = "", file_id: str = "",
                media_name: str = "", create: bool = False) -> _PlaybackSessionLink | None:
        normalized_scope = str(auth_scope or "")
        normalized_instance = int(instance_id)
        normalized_token = self._token(token)
        with self._lock:
            self._prune_locked(self._clock())
            if normalized_token:
                key = (normalized_scope, normalized_instance, normalized_token)
                entry = self._entries.get(key)
                if entry is None:
                    alias_key = (normalized_scope, normalized_instance, normalized_token)
                    canonical_key = self._upstream_session_index.get(alias_key)
                    if canonical_key is not None:
                        entry = self._entries.get(canonical_key)
                        if entry is None:
                            self._upstream_session_index.pop(alias_key, None)
                        else:
                            key = canonical_key
                if (
                    entry is not None
                    and item_id
                    and entry.item_id
                    and entry.item_id != _normalized_media_identifier(item_id)
                ):
                    if create:
                        return self.begin(
                            normalized_scope,
                            normalized_instance,
                            item_id=item_id,
                            source_id=source_id,
                            file_id=file_id,
                            media_name=media_name,
                        )
                    return None
                if entry is not None:
                    return self._touch_locked(
                        key,
                        entry,
                        item_id=item_id,
                        source_id=source_id,
                        file_id=file_id,
                        media_name=media_name,
                    )
                if create:
                    return self.begin(
                        normalized_scope, normalized_instance, token=normalized_token,
                        item_id=item_id, source_id=source_id, file_id=file_id,
                        media_name=media_name,
                    )
                return None

            candidates = [
                (key, entry) for key, entry in self._entries.items()
                if key[0] == normalized_scope and key[1] == normalized_instance
            ]
            preferred: list[tuple[tuple[str, int, str], _PlaybackSessionLink]] = []
            if file_id:
                preferred = [(key, entry) for key, entry in candidates if entry.file_id == str(file_id)]
            elif item_id and source_id:
                pending = [
                    (key, entry) for key, entry in candidates
                    if entry.item_id == _normalized_media_identifier(item_id) and not entry.source_id
                ]
                exact = [
                    (key, entry) for key, entry in candidates
                    if (
                        entry.item_id == _normalized_media_identifier(item_id)
                        and entry.source_id == _normalized_media_identifier(source_id)
                    )
                ]
                if len(pending) == 1:
                    preferred = pending
                elif len(exact) == 1:
                    preferred = exact
            elif item_id:
                preferred = [
                    (key, entry) for key, entry in candidates
                    if entry.item_id == _normalized_media_identifier(item_id)
                ]
            elif len(candidates) == 1:
                preferred = candidates

            if len(preferred) == 1:
                key, entry = preferred[0]
                return self._touch_locked(
                    key,
                    entry,
                    item_id=item_id,
                    source_id=source_id,
                    file_id=file_id,
                    media_name=media_name,
                )

        if create and (item_id or file_id):
            return self.begin(
                normalized_scope,
                normalized_instance,
                item_id=item_id,
                source_id=source_id,
                file_id=file_id,
                media_name=media_name,
            )
        return None

    @staticmethod
    def persistent_key(entry: _PlaybackSessionLink) -> str:
        payload = (
            f"mediaflux-playback-session\0{entry.auth_scope}\0"
            f"{entry.instance_id}\0{entry.token}"
        ).encode("utf-8")
        return hmac.new(_AUTH_SCOPE_SECRET, payload, hashlib.sha256).hexdigest()[:48]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._capability_index.clear()
            self._entry_capabilities.clear()
            self._capability_browser_direct_redirect_signatures.clear()
            self._upstream_session_index.clear()
            self._media_name_index.clear()


_playback_sessions = PlaybackSessionRegistry()


def _apply_playback_session(request: Request, entry: _PlaybackSessionLink | None) -> None:
    if entry is None:
        return
    request.state.proxy_playback_session_token = entry.token
    request.state.proxy_playback_session_key = _playback_sessions.persistent_key(entry)
    request.state.proxy_browser_relay = bool(entry.browser_relay)
    capability_token = _request_query_value(
        request, _PLAYBACK_SESSION_QUERY_KEY
    )
    source_signature = _request_query_value(request, _PLAYBACK_SOURCE_QUERY_KEY)
    request.state.proxy_browser_direct_redirect = (
        _playback_sessions.capability_allows_browser_direct_redirect(
            entry.instance_id, capability_token, source_signature
        )
    )
    request.state.proxy_native_cross_protocol_relay = bool(
        entry.native_cross_protocol_relay
    )
    request.state.proxy_native_signed_media_relay = bool(
        entry.native_signed_media_relay
    )
    if entry.item_id:
        request.state.proxy_media_item_id = entry.item_id
    if entry.source_id:
        request.state.proxy_media_source_id = entry.source_id
    if entry.file_id:
        request.state.proxy_guangya_file_id = entry.file_id
    if entry.media_name:
        request.state.proxy_media_name = entry.media_name


def _request_query_value(request: Request, *names: str) -> str:
    values = _request_query_values(request, *names)
    return values[0] if values else ""


def _request_query_values(request: Request, *names: str) -> list[str]:
    accepted = {str(name).lower() for name in names}
    values: list[str] = []
    for key, value in request.query_params.multi_items():
        if str(key).lower() in accepted and str(value or "").strip():
            values.append(str(value).strip())
    return values


def _request_query_has_key(request: Request, *names: str) -> bool:
    accepted = {str(name).casefold() for name in names}
    return any(
        str(key).casefold() in accepted
        for key, _value in request.query_params.multi_items()
    )


def _request_query_has_conflicting_values(
    request: Request, *names: str
) -> bool:
    accepted = {str(name).lower() for name in names}
    values = [
        str(value or "").strip()
        for key, value in request.query_params.multi_items()
        if str(key).lower() in accepted
    ]
    if len(values) <= 1:
        return False
    return not values[0] or any(value != values[0] for value in values[1:])


def _extract_guangya_file_id(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        path = parsed.path
    except ValueError:
        return None
    if not path.startswith("/"):
        path = f"/{path}"
    match = _PLAYGY_RE.match(path)
    if not match:
        return None
    file_id = unquote(match.group(1)).strip()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if query.get("enc") == "b64":
        try:
            from app.modules.playgy_signing import decode_playgy_path_token
            file_id = decode_playgy_path_token(file_id)
        except ValueError:
            return None
    return file_id or None


def _route_prefix(path: str) -> str:
    return "/emby" if str(path or "").lower().startswith("/emby/") else ""


def _direct_stream_path(
    item_id: str,
    source_id: str = "",
    prefix: str = "",
    playback_session_token: str = "",
    playback_source_signature: str = "",
    container: str = "",
) -> str:
    normalized_container = str(container or "").strip().casefold().split(",", 1)[0]
    suffix = (
        f".{normalized_container}"
        if re.fullmatch(r"[a-z0-9]{1,12}", normalized_container)
        else ""
    )
    path = f"{prefix}/Videos/{quote(str(item_id), safe='')}/stream{suffix}"
    query: list[str] = []
    if source_id:
        query.append(f"MediaSourceId={quote(str(source_id), safe='')}")
    if playback_session_token:
        query.append(
            f"{_PLAYBACK_SESSION_QUERY_KEY}={quote(str(playback_session_token), safe='')}"
        )
    if playback_source_signature:
        query.append(
            f"{_PLAYBACK_SOURCE_QUERY_KEY}="
            f"{quote(str(playback_source_signature), safe='')}"
        )
    return f"{path}?{'&'.join(query)}" if query else path


def _mark_direct_source(
    source: dict[str, Any],
    stream_path: str,
    direct_stream_url: str | None = None,
    *,
    native_client_mode: str = "",
) -> None:
    requested_mode = str(native_client_mode or "").strip().casefold()
    mode = (
        requested_mode
        if requested_mode in {"web", "jellyfin_android", "findroid"}
        else ""
    )
    # Findroid 对 Protocol=HTTP 会直接采用 PlaybackInfo 返回的 Path；这样可以
    # 保留 MediaFlux 写入 URL 的短时 capability。Protocol=File 则会自行通过
    # SDK 重建标准 /Videos/{id}/stream URL，丢掉 capability 与访问令牌。
    findroid_http_source = mode == "findroid"
    web_direct_http_source = bool(
        mode == "web" and source.get("SupportsDirectPlay") is True
    )
    http_source = findroid_http_source or web_direct_http_source
    source["IsRemote"] = http_source
    source["Protocol"] = "Http" if http_source else "File"
    source["RequiresOpening"] = False
    source["RequiresClosing"] = False
    source["RequiresLooping"] = False
    source["ReadAtNativeFramerate"] = False
    source["Path"] = stream_path
    source["DirectStreamUrl"] = direct_stream_url or stream_path

    if mode == "web":
        # Web 的设备能力仍由 Jellyfin 上游判定，不能把原本不可直放的
        # MKV/HEVC 强制宣告为 Direct Play。对于上游已经确认可直放的 source，
        # 改成 Remote HTTP contract：Jellyfin Web 因 IsRemote=True 不会给
        # <video> 设置 crossorigin=anonymous，原生媒体元素即可用 no-cors GET
        # 跟随 MediaFlux 的 302。
        if web_direct_http_source:
            # Jellyfin Web 首次播放会在客户端侧探测并写入该私有字段；但切换
            # 清晰度时 changeStream() 会直接消费新的 PlaybackInfo，不再重复探测。
            # 明确标记后，它会继续采用下面携带 capability 的 Path，而不是重建
            # 一个丢失 _mfps/_mfss 的标准 stream URL。
            source["enableDirectPlay"] = True
            source["RequiredHttpHeaders"] = {}
            # Jellyfin Web 12 只要看到 TranscodingSubProtocol=HLS，就算当前
            # playMethod 是 DirectPlay 也会交给 hls.js/XHR，随后跨域 302 被 CDN
            # CORS 拦截。直放合同移除该 HLS 标记；若客户端重新请求转码合同，
            # 上游会在 SupportsDirectPlay=False 的响应中原样返回 HLS 字段。
            source.pop("TranscodingSubProtocol", None)
        return

    # Jellyfin Android 2.7 使用的 jellyfin-sdk-kotlin 1.7.1 把
    # TranscodingSubProtocol 声明为非空必填字段。即便禁用了转码，缺少该键也会
    # 让 PlaybackInfo 在客户端反序列化阶段直接失败，因而根本不会发出视频 GET。
    transcoding_sub_protocol = str(
        source.get("TranscodingSubProtocol") or ""
    ).strip().casefold()
    source["TranscodingSubProtocol"] = (
        transcoding_sub_protocol
        if transcoding_sub_protocol in {"http", "hls"}
        else "http"
    )

    # Jellyfin for Android 2.7 对 File 媒体优先选择 DirectPlay，并自行重建
    # 无扩展名 stream URL；现场实测该分支会在发出媒体 GET 前 0ms 结束。
    # 仅对官方 Android 客户端关闭 DirectPlay，强制进入已验证可请求
    # /Videos/{id}/stream.{container} 的 DirectStream 分支。
    source["SupportsDirectPlay"] = mode != "jellyfin_android"
    source["SupportsDirectStream"] = True
    # Android 后续 DirectStream 请求通过上游 PlaySessionId 恢复精确
    # item/source 授权；Findroid 则通过 HTTP Path 携带 capability。
    source["SupportsTranscoding"] = False
    # 原生直放必须删除 HLS 清单地址，否则客户端仍可能选择 TranscodingUrl。
    # TranscodingSubProtocol 因旧 Android SDK 契约要求而保留。
    for key in (
        "TranscodingUrl",
        "TranscodingContainer",
        "TranscodingInfo",
    ):
        source.pop(key, None)


def _client_stream_url(path: str, base_url: str, native_client_mode: str) -> str:
    """Findroid 的 HTTP MediaSource 需要绝对 URL；其他客户端保持相对路径。"""
    raw_path = str(path or "").strip()
    if str(native_client_mode or "").strip().casefold() != "findroid":
        return raw_path
    raw_base = str(base_url or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw_base)
    except ValueError:
        return raw_path
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return raw_path
    return f"{raw_base}/{raw_path.lstrip('/')}"


def validate_listen_host(value: str) -> str:
    host = str(value or "127.0.0.1").strip()
    if host not in _ALLOWED_HOSTS:
        raise ValueError("监听地址只允许 127.0.0.1、0.0.0.0、::1 或 ::")
    return host


def _validate_upstream_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if address in _BLOCKED_METADATA_IPS:
        raise ValueError("上游地址不能指向云元数据服务")
    if address.is_link_local or address.is_multicast or address.is_unspecified:
        raise ValueError("上游地址不能使用链路本地、组播或未指定地址")


def _parse_upstream_url(value: str) -> tuple[str, httpx.URL]:
    url = str(value or "").strip().rstrip("/")
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise ValueError("上游地址必须是有效的 HTTP/HTTPS URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ValueError("上游地址必须是有效的 HTTP/HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("上游地址不能包含凭据、查询参数或片段")
    host = str(parsed.host).strip().lower()
    if host in _BLOCKED_METADATA_HOSTS:
        raise ValueError("上游地址不能指向云元数据服务")
    return url, parsed


def _resolve_upstream_addresses(
    parsed: httpx.URL,
    *,
    allow_dns_failure: bool,
) -> tuple[str, ...]:
    """解析并验证一次地址快照；运行时必须把连接固定到该快照。"""
    host = str(parsed.host).strip().lower()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        _validate_upstream_address(literal)
        return (str(literal),)

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        if allow_dns_failure:
            return ()
        raise ValueError("无法解析上游地址") from exc

    addresses: list[str] = []
    for _, _, _, _, socket_address in resolved:
        if not socket_address:
            continue
        raw_address = str(socket_address[0]).split("%", 1)[0]
        try:
            resolved_address = ipaddress.ip_address(raw_address)
        except ValueError:
            continue
        _validate_upstream_address(resolved_address)
        normalized = str(resolved_address)
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses and not allow_dns_failure:
        raise ValueError("上游域名没有可用地址")
    return tuple(addresses)


def validate_upstream_url(value: str) -> str:
    """校验管理端保存的固定上游地址。

    配置阶段允许暂时无法解析的离线域名；实际代理请求会重新取得一次
    受控地址快照，并把 TCP/WebSocket 连接固定到该快照。
    """
    url, parsed = _parse_upstream_url(value)
    _resolve_upstream_addresses(parsed, allow_dns_failure=True)
    return url


def _join_upstream_url(parsed: httpx.URL, request_path: str) -> httpx.URL:
    """拼接固定上游路径，避免 upstream 与请求同时带 /emby 时重复。"""
    base_path = parsed.path.rstrip("/")
    path = "/" + str(request_path or "").lstrip("/")
    if base_path and base_path != "/" and (
        path.lower() == base_path.lower()
        or path.lower().startswith(base_path.lower() + "/")
    ):
        joined_path = path
    else:
        joined_path = f"{base_path}{path}" if base_path and base_path != "/" else path
    return parsed.copy_with(path="/" if joined_path == "/" else joined_path)


def _upstream_target(upstream: str, request_path: str) -> str:
    _, parsed = _parse_upstream_url(upstream)
    _resolve_upstream_addresses(parsed, allow_dns_failure=True)
    target = str(_join_upstream_url(parsed, request_path))
    return target.rstrip("/") if httpx.URL(target).path == "/" else target


@dataclass(frozen=True)
class _PinnedUpstreamTarget:
    logical_url: str
    connect_url: str
    host_header: str
    sni_hostname: str
    addresses: tuple[str, ...]


class _UpstreamClientPool:
    """按逻辑上游 authority 复用连接，并由代理应用生命周期统一关闭。"""

    def __init__(self) -> None:
        self._clients: dict[tuple[str, str, int], httpx.AsyncClient] = {}

    @staticmethod
    def _key(pinned: _PinnedUpstreamTarget) -> tuple[str, str, int]:
        logical = httpx.URL(pinned.logical_url)
        default_port = 443 if logical.scheme == "https" else 80
        return (logical.scheme, str(logical.host or ""), int(logical.port or default_port))

    def get(self, pinned: _PinnedUpstreamTarget) -> httpx.AsyncClient:
        key = self._key(pinned)
        client = self._clients.get(key)
        if client is None:
            client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=_upstream_timeout(),
                trust_env=False,
            )
            self._clients[key] = client
        return client

    async def aclose(self) -> None:
        clients = tuple(self._clients.values())
        self._clients.clear()
        if not clients:
            return
        results = await asyncio.gather(
            *(client.aclose() for client in clients),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning(
                    "媒体反代上游连接池关闭失败 type=%s",
                    type(result).__name__,
                )


def _pin_upstream_target(upstream: str, request_path: str) -> _PinnedUpstreamTarget:
    """把逻辑 Host/SNI 与一次性验证后的物理连接地址绑定。"""
    _, parsed = _parse_upstream_url(upstream)
    addresses = _resolve_upstream_addresses(parsed, allow_dns_failure=False)
    logical = _join_upstream_url(parsed, request_path)
    connect = logical.copy_with(host=addresses[0])
    return _PinnedUpstreamTarget(
        logical_url=str(logical),
        connect_url=str(connect),
        host_header=logical.netloc.decode("ascii"),
        sni_hostname=str(parsed.host),
        addresses=addresses,
    )


def _validate_signed_media_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> None:
    """Signed URL 由外部服务返回，回源时必须拒绝所有本地网络目标。"""
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    _validate_upstream_address(address)
    if not address.is_global or bool(getattr(address, "is_site_local", False)):
        raise ValueError("媒体直链只能指向公网地址")


def _pin_signed_media_target(value: str) -> _PinnedUpstreamTarget:
    """校验带签名查询参数的媒体 URL，并将连接固定到公共 DNS 快照。"""
    raw = str(value or "").strip()
    try:
        logical = httpx.URL(raw)
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise ValueError("媒体直链必须是有效的 HTTP/HTTPS URL") from exc
    if logical.scheme not in {"http", "https"} or not logical.host:
        raise ValueError("媒体直链必须是有效的 HTTP/HTTPS URL")
    if logical.username or logical.password or logical.fragment:
        raise ValueError("媒体直链不能包含凭据或片段")
    host = str(logical.host).strip().lower()
    if host in _BLOCKED_METADATA_HOSTS:
        raise ValueError("媒体直链不能指向云元数据服务")
    addresses = _resolve_upstream_addresses(logical, allow_dns_failure=False)
    for raw_address in addresses:
        _validate_signed_media_address(ipaddress.ip_address(raw_address))
    connect = logical.copy_with(host=addresses[0])
    return _PinnedUpstreamTarget(
        logical_url=str(logical),
        connect_url=str(connect),
        host_header=logical.netloc.decode("ascii"),
        sni_hostname=str(logical.host),
        addresses=addresses,
    )


async def probe_media_proxy_instance(
    instance_id: int,
    *,
    timeout_seconds: float = 8.0,
) -> dict[str, int]:
    """安全探测一个已保存实例；固定 DNS 快照且不返回地址或凭据。"""
    row = await asyncio.to_thread(database.get_media_proxy_instance, int(instance_id))
    if row is None:
        raise LookupError("media proxy instance not found")
    resolved = resolve_proxy_instance(row)
    credential = str(resolved.get("api_key") or "").strip()
    probe_path = "/System/Info" if credential else "/System/Info/Public"
    pinned = await asyncio.to_thread(
        _pin_upstream_target,
        str(resolved.get("upstream_url") or ""),
        probe_path,
    )
    headers = _apply_upstream_credential(
        {"Accept": "application/json"},
        credential,
        str(resolved.get("server_type") or ""),
    )
    headers["Host"] = pinned.host_header

    timeout_value = max(1.0, min(float(timeout_seconds), 15.0))
    started = time.monotonic()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_value),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        request = client.build_request(
            "GET",
            pinned.connect_url,
            headers=headers,
            extensions={"sni_hostname": pinned.sni_hostname},
        )
        response = await client.send(request, stream=True)
        try:
            await _read_bounded_upstream_body(
                response, _playback_info_response_limit()
            )
            status_code = int(response.status_code)
        finally:
            await response.aclose()
    return {
        "status_code": status_code,
        "latency_ms": max(0, round((time.monotonic() - started) * 1000)),
    }


class _PinnedResolver(AbstractResolver):
    """aiohttp WebSocket resolver：仅返回已校验的 DNS 快照。"""

    def __init__(self, hostname: str, addresses: tuple[str, ...]) -> None:
        self.hostname = str(hostname).rstrip(".").lower()
        self.addresses = tuple(addresses)

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET,
    ) -> list[dict[str, object]]:
        if str(host).rstrip(".").lower() != self.hostname:
            raise OSError("Unexpected upstream hostname")
        results: list[dict[str, object]] = []
        for raw in self.addresses:
            address = ipaddress.ip_address(raw)
            address_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            if family not in {socket.AF_UNSPEC, address_family}:
                continue
            results.append({
                "hostname": host,
                "host": raw,
                "port": port,
                "family": address_family,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            })
        if not results:
            raise OSError("No pinned upstream address for requested family")
        return results

    async def close(self) -> None:
        return None


def resolve_local_binding(local_root: str, relative_path: str) -> Path:
    root = Path(str(local_root or "")).expanduser().resolve(strict=False)
    relative = Path(str(relative_path or ""))
    if not str(local_root or "").strip() or relative.is_absolute():
        raise ValueError("本地绑定必须使用已配置根目录下的相对路径")
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("本地绑定路径越界") from exc
    return candidate


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_HEADERS | {"host", "set-cookie"}
    }


def _decoded_response_headers(headers: httpx.Headers) -> dict[str, str]:
    result = _response_headers(headers)
    result.pop("content-encoding", None)
    result.pop("Content-Encoding", None)
    result.pop("content-length", None)
    result.pop("Content-Length", None)
    return result


def _without_mediaflux_session_cookie(value: str) -> str:
    """保留媒体服务器 Cookie，但绝不把 MediaFlux 登录会话转给上游。"""
    retained: list[str] = []
    for raw_part in str(value or "").split(";"):
        part = raw_part.strip()
        if not part:
            continue
        name, separator, _cookie_value = part.partition("=")
        if separator and name.strip().casefold() == _MEDIAFLUX_SESSION_COOKIE_NAME:
            continue
        retained.append(part)
    return "; ".join(retained)


def _request_headers(request: Request) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_HEADERS | _REQUEST_ONLY_HEADERS
    }
    for key in tuple(headers):
        if key.casefold() != "cookie":
            continue
        safe_cookie = _without_mediaflux_session_cookie(headers[key])
        if safe_cookie:
            headers[key] = safe_cookie
        else:
            headers.pop(key, None)
    return headers


def _same_upstream_redirect_location(
    pinned: _PinnedUpstreamTarget,
    location: str,
    *,
    upstream_base_url: str = "",
) -> str:
    """只允许逻辑上游同源跳转，并改写为当前代理 origin 的相对地址。"""
    raw_location = str(location or "").strip()
    if not raw_location:
        raise ValueError("上游重定向缺少目标地址")
    joined = urljoin(pinned.logical_url, raw_location)
    base = urlsplit(pinned.logical_url)
    target = urlsplit(joined)
    base_port = base.port or (443 if base.scheme == "https" else 80)
    target_port = target.port or (443 if target.scheme == "https" else 80)
    if (
        target.scheme.casefold() != base.scheme.casefold()
        or str(target.hostname or "").casefold()
        != str(base.hostname or "").casefold()
        or target_port != base_port
        or target.username
        or target.password
    ):
        raise ValueError("上游重定向越过媒体服务器边界")
    target_path = target.path or "/"
    decoded_path = target_path
    for _ in range(3):
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path
    decoded_segments = decoded_path.split("/")
    if (
        target_path.startswith("//")
        or "//" in decoded_path
        or "\\" in decoded_path
        or any(segment in {".", ".."} for segment in decoded_segments)
    ):
        raise ValueError("上游重定向路径不安全")
    configured_base = urlsplit(str(upstream_base_url or pinned.logical_url))
    base_path = configured_base.path.rstrip("/")
    if base_path and base_path != "/" and not (
        target_path.casefold() == base_path.casefold()
        or target_path.casefold().startswith(base_path.casefold() + "/")
    ):
        raise ValueError("上游重定向离开已配置的媒体服务器路径")
    relative = target_path
    if target.query:
        relative = f"{relative}?{target.query}"
    if target.fragment:
        relative = f"{relative}#{target.fragment}"
    return relative


def _replace_header(
    headers: dict[str, str],
    name: str,
    value: str,
) -> None:
    """大小写无关地替换单值请求头，避免 httpx 合并重复字段。"""
    normalized_name = str(name or "").casefold()
    for key in tuple(headers):
        if key.casefold() == normalized_name:
            headers.pop(key, None)
    headers[name] = value


def _canonical_client_ip(request: Any) -> str:
    """返回 ASGI 已确认的 socket 客户端地址，不信任下游自带转发头。"""
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "").strip()
    if not host and isinstance(client, (tuple, list)) and client:
        host = str(client[0] or "").strip()
    if not host:
        return ""
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    if "%" in host:
        host = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return ""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def _canonical_forwarded_scheme(request: Any) -> str:
    scheme = str(
        getattr(getattr(request, "url", None), "scheme", "") or ""
    ).casefold()
    if scheme not in {"http", "https", "ws", "wss"}:
        return ""
    return {"ws": "http", "wss": "https"}.get(scheme, scheme)


def _apply_canonical_forwarded_headers(
    headers: dict[str, str],
    request: Any,
) -> dict[str, str]:
    """覆盖客户端可伪造的 forwarding headers，并统一供 HTTP/WS 上游使用。"""
    for key in tuple(headers):
        if key.casefold() in _FORWARDED_CLIENT_HEADERS:
            headers.pop(key, None)
    client_ip = _canonical_client_ip(request)
    if client_ip:
        headers["X-Forwarded-For"] = client_ip
        headers["X-Real-IP"] = client_ip
    scheme = _canonical_forwarded_scheme(request)
    if scheme:
        headers["X-Forwarded-Proto"] = scheme
    # X-Forwarded-Host 不能从客户端可控的 Host 派生；缺少受信任的公开
    # origin 配置时，宁可不发送，也不能让 Jellyfin 将其当作可信代理值。
    return headers


def _authorization_client_name(value: Any) -> str:
    match = _AUTH_CLIENT_RE.search(str(value or ""))
    return str((match.group(1) or match.group(2) or "") if match else "").strip()


def _request_client_name(request: Any) -> str:
    headers = request.headers
    for key in ("X-Emby-Client", "X-Jellyfin-Client"):
        value = str(headers.get(key) or "").strip()
        if value:
            return value
    for key in ("X-Emby-Authorization", "Authorization"):
        value = _authorization_client_name(headers.get(key))
        if value:
            return value
    return ""


def _request_is_web_client(request: Any) -> bool:
    client_name = _request_client_name(request).casefold()
    if client_name:
        return "web" in client_name or "browser" in client_name
    return "mozilla/" in str(request.headers.get("User-Agent") or "").casefold()


def _request_is_jellyfin_android_client(request: Any) -> bool:
    client_name = _request_client_name(request).casefold()
    if client_name:
        return (
            "jellyfin" in client_name
            and "android" in client_name
            and "web" not in client_name
        )
    user_agent = str(request.headers.get("User-Agent") or "").casefold()
    return "jellyfin" in user_agent and "android" in user_agent


def _request_uses_exoplayer(request: Any) -> bool:
    """识别默认禁止 HTTP/HTTPS 跨协议重定向的 Media3/ExoPlayer 客户端。"""
    client_name = _request_client_name(request).casefold()
    if "findroid" in client_name or _request_is_jellyfin_android_client(request):
        return True
    user_agent = str(request.headers.get("User-Agent") or "").casefold()
    return any(marker in user_agent for marker in (
        "exoplayer", "media3", "findroid", "jellyfin android",
        "jellyfin-android",
    ))


def _request_prefers_direct_signed_redirect(request: Any) -> bool:
    """保留已验证可直接跟随 HTTPS signed URL 的第三方客户端路径。"""
    client_name = _request_client_name(request).casefold()
    user_agent = str(request.headers.get("User-Agent") or "").casefold()
    return any(
        marker in client_name or marker in user_agent
        for marker in ("yamby", "moonfin")
    )


def _native_playback_rewrite_mode(
    request: Any,
    request_payload: Any = None,
) -> str:
    client_name = _request_client_name(request).casefold()
    if "findroid" in client_name:
        return "findroid"
    if _request_is_jellyfin_android_client(request):
        profile = _diagnostic_dict_value(request_payload, "DeviceProfile")
        profile_name = str(
            _diagnostic_dict_value(profile, "Name") or ""
        ).casefold()
        # Jellyfin Android 的界面本身运行在 WebView 中。使用网页播放器时，
        # Client 仍是 Jellyfin for Android，但 DeviceProfile 不是原生播放器
        # 档案；不能把浏览器错误地强制成 MKV DirectStream。
        if "jellyfin" in profile_name and "android" in profile_name:
            return "jellyfin_android"
        user_agent = str(request.headers.get("User-Agent") or "").casefold()
        if "mozilla/" in user_agent or "; wv)" in user_agent:
            return "web"
        return "jellyfin_android"
    if _request_is_web_client(request):
        return "web"
    return ""


def _diagnostic_dict_value(data: Any, *names: str) -> Any:
    if not isinstance(data, dict):
        return None
    folded = {str(key).casefold(): value for key, value in data.items()}
    for name in names:
        if str(name).casefold() in folded:
            return folded[str(name).casefold()]
    return None


def _diagnostic_dict_has_key(data: Any, name: str) -> bool:
    if not isinstance(data, dict):
        return False
    target = str(name).casefold()
    return any(str(key).casefold() == target for key in data)


def _profile_tokens(value: Any) -> set[str]:
    return {
        token.strip().casefold()
        for token in str(value or "").split(",")
        if token.strip()
    }


def _normalized_codec(value: Any) -> str:
    codec = str(value or "").strip().casefold().replace("_", "").replace("-", "")
    return {
        "h265": "hevc",
        "x265": "hevc",
        "avc": "h264",
        "avc1": "h264",
        "ec3": "eac3",
        "eac3": "eac3",
        "ac3": "ac3",
    }.get(codec, codec)


def _profile_supports_value(value: Any, candidate: Any, *, codec: bool = False) -> bool:
    declared = _profile_tokens(value)
    if not declared:
        return True
    normalized_candidate = (
        _normalized_codec(candidate)
        if codec
        else str(candidate or "").strip().casefold()
    )
    normalized_declared = (
        {_normalized_codec(token) for token in declared}
        if codec
        else declared
    )
    if normalized_candidate in normalized_declared:
        return True
    if not codec and normalized_candidate in {"mkv", "matroska"}:
        return bool(normalized_declared.intersection({"mkv", "matroska"}))
    return False


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _audio_stream_is_commentary(stream: dict[str, Any]) -> bool:
    if bool(stream.get("IsCommentary")):
        return True
    label = " ".join(
        str(stream.get(key) or "")
        for key in ("Title", "DisplayTitle")
    ).casefold()
    return any(marker in label for marker in (
        "commentary", "comment", "解说", "评论音轨",
    ))


def _web_direct_audio_retry_payload(
    request_payload: Any,
    response_payload: Any,
) -> dict[str, Any] | None:
    """在不伪造设备能力的前提下，为 Web 尝试一次可直放默认音轨。

    仅当当前音轨就是 Jellyfin 给出的默认音轨时才允许替换，避免覆盖用户
    已显式选择的语言或评论音轨。第二次 PlaybackInfo 仍由 Jellyfin 最终判定
    是否支持 DirectPlay；未提升直放能力时必须保留首次 HLS/转码合同。
    """
    if not isinstance(request_payload, dict) or not isinstance(response_payload, dict):
        return None
    profile = _diagnostic_dict_value(request_payload, "DeviceProfile")
    direct_profiles = _diagnostic_dict_value(profile, "DirectPlayProfiles")
    if not isinstance(direct_profiles, list):
        return None
    video_profiles = [
        item
        for item in direct_profiles
        if isinstance(item, dict)
        and str(_diagnostic_dict_value(item, "Type") or "").casefold() == "video"
    ]
    if not video_profiles:
        return None

    requested_source = _normalized_media_identifier(
        str(_diagnostic_dict_value(request_payload, "MediaSourceId") or "")
    )
    media_sources = response_payload.get("MediaSources")
    if not isinstance(media_sources, list):
        return None
    candidate_sources = [item for item in media_sources if isinstance(item, dict)]
    if not requested_source and len(candidate_sources) != 1:
        return None
    for source in candidate_sources:
        if not isinstance(source, dict) or source.get("SupportsDirectPlay") is True:
            continue
        source_id = _normalized_media_identifier(str(source.get("Id") or ""))
        if requested_source and source_id != requested_source:
            continue
        if not _extract_guangya_file_id(str(source.get("Path") or "")):
            continue
        streams = source.get("MediaStreams")
        if not isinstance(streams, list):
            continue
        video_stream = next(
            (
                item
                for item in streams
                if isinstance(item, dict)
                and str(item.get("Type") or "").casefold() == "video"
            ),
            None,
        )
        audio_streams = [
            item
            for item in streams
            if isinstance(item, dict)
            and str(item.get("Type") or "").casefold() == "audio"
            and _optional_int(item.get("Index")) is not None
        ]
        if video_stream is None or len(audio_streams) < 2:
            continue
        container = _native_stream_container(source)
        video_codec = video_stream.get("Codec")
        matching_profiles = [
            item
            for item in video_profiles
            if _profile_supports_value(
                _diagnostic_dict_value(item, "Container"), container
            )
            and _profile_supports_value(
                _diagnostic_dict_value(item, "VideoCodec"),
                video_codec,
                codec=True,
            )
        ]
        if not matching_profiles:
            continue

        default_audio_index = _optional_int(source.get("DefaultAudioStreamIndex"))
        if default_audio_index is None:
            default_audio_index = next(
                (
                    _optional_int(item.get("Index"))
                    for item in audio_streams
                    if bool(item.get("IsDefault"))
                ),
                None,
            )
        requested_audio_index = _optional_int(
            _diagnostic_dict_value(request_payload, "AudioStreamIndex")
        )
        if requested_audio_index is None:
            requested_audio_index = default_audio_index
        # 非默认 index 通常来自用户手动选轨，自动 302 不能改变其选择。
        if (
            requested_audio_index is None
            or default_audio_index is None
            or requested_audio_index != default_audio_index
        ):
            continue
        current_audio = next(
            (
                item
                for item in audio_streams
                if _optional_int(item.get("Index")) == requested_audio_index
            ),
            None,
        )
        if current_audio is None or _audio_stream_is_commentary(current_audio):
            continue
        if any(
            _profile_supports_value(
                _diagnostic_dict_value(item, "AudioCodec"),
                current_audio.get("Codec"),
                codec=True,
            )
            for item in matching_profiles
        ):
            continue

        current_language = str(current_audio.get("Language") or "").strip().casefold()
        compatible_audio = [
            item
            for item in audio_streams
            if _optional_int(item.get("Index")) != requested_audio_index
            and not _audio_stream_is_commentary(item)
            and (
                str(item.get("Language") or "").strip().casefold()
                == current_language
            )
            and any(
                _profile_supports_value(
                    _diagnostic_dict_value(candidate, "AudioCodec"),
                    item.get("Codec"),
                    codec=True,
                )
                for candidate in matching_profiles
            )
        ]
        if not compatible_audio:
            continue
        compatible_audio.sort(
            key=lambda item: (
                not bool(item.get("IsDefault")),
                -(_optional_int(item.get("Channels")) or 0),
                _optional_int(item.get("Index")) or 0,
            )
        )
        selected_index = _optional_int(compatible_audio[0].get("Index"))
        if selected_index is None:
            continue
        retry_payload = dict(request_payload)
        retry_payload["AudioStreamIndex"] = selected_index
        if source_id:
            retry_payload["MediaSourceId"] = str(source.get("Id") or "")
        return retry_payload
    return None


def _playback_source_direct_state(
    payload: Any,
    source_id: str,
) -> bool | None:
    if not isinstance(payload, dict):
        return None
    media_sources = payload.get("MediaSources")
    if not isinstance(media_sources, list):
        return None
    sources = [source for source in media_sources if isinstance(source, dict)]
    normalized_source = _normalized_media_identifier(source_id)
    if normalized_source:
        matches = [
            source
            for source in sources
            if _normalized_media_identifier(str(source.get("Id") or ""))
            == normalized_source
        ]
        if len(matches) != 1:
            return None
        return matches[0].get("SupportsDirectPlay") is True
    if len(sources) != 1:
        return None
    return sources[0].get("SupportsDirectPlay") is True


def _playback_source_gained_direct_play(
    initial_payload: Any,
    retry_payload: Any,
    source_id: str,
) -> bool:
    return (
        _playback_source_direct_state(initial_payload, source_id) is False
        and _playback_source_direct_state(retry_payload, source_id) is True
    )


def _native_playback_diagnostic_summary(
    request_payload: Any,
    response_payload: Any,
    *,
    item_id: str,
    changed: bool,
) -> dict[str, Any]:
    """生成不含 token、播放 URL 和签名参数的原生客户端诊断摘要。"""
    request_data = request_payload if isinstance(request_payload, dict) else {}
    profile = _diagnostic_dict_value(request_data, "DeviceProfile")
    profile = profile if isinstance(profile, dict) else {}
    normalized_item = _normalized_media_identifier(item_id).replace("-", "").casefold()
    response_data = response_payload if isinstance(response_payload, dict) else {}
    source_summaries: list[dict[str, Any]] = []
    media_sources = response_data.get("MediaSources")
    if isinstance(media_sources, list):
        for source in media_sources[:4]:
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("Id") or "")
            source_path = str(source.get("Path") or "")
            direct_url = str(source.get("DirectStreamUrl") or "")
            try:
                parsed_source_path = urlsplit(source_path)
            except ValueError:
                parsed_source_path = urlsplit("")
            source_summaries.append({
                "id_present": bool(source_id),
                "id_matches_item": bool(
                    source_id
                    and source_id.replace("-", "").casefold() == normalized_item
                ),
                "protocol": str(source.get("Protocol") or ""),
                "container": str(source.get("Container") or ""),
                "direct_play": bool(source.get("SupportsDirectPlay")),
                "direct_stream": bool(source.get("SupportsDirectStream")),
                "transcoding": bool(source.get("SupportsTranscoding")),
                "path_relative": source_path.startswith("/"),
                "path_scheme": parsed_source_path.scheme,
                "path_host": parsed_source_path.hostname or "",
                "path_route": parsed_source_path.path,
                "direct_url_relative": direct_url.startswith("/"),
                "stream_count": len(source.get("MediaStreams") or []),
                "default_audio": source.get("DefaultAudioStreamIndex"),
                "default_subtitle": source.get("DefaultSubtitleStreamIndex"),
            })
    request_source_id = str(
        _diagnostic_dict_value(request_data, "MediaSourceId") or ""
    )
    return {
        "request_source_present": bool(request_source_id),
        "request_source_matches_item": bool(
            request_source_id
            and request_source_id.replace("-", "").casefold() == normalized_item
        ),
        "enable_direct_play": _diagnostic_dict_value(
            request_data, "EnableDirectPlay"
        ),
        "enable_direct_stream": _diagnostic_dict_value(
            request_data, "EnableDirectStream"
        ),
        "enable_transcoding": _diagnostic_dict_value(
            request_data, "EnableTranscoding"
        ),
        "profile_name": str(_diagnostic_dict_value(profile, "Name") or ""),
        "profile_direct_count": len(
            _diagnostic_dict_value(profile, "DirectPlayProfiles") or []
        ),
        "profile_transcode_count": len(
            _diagnostic_dict_value(profile, "TranscodingProfiles") or []
        ),
        "profile_codec_count": len(
            _diagnostic_dict_value(profile, "CodecProfiles") or []
        ),
        "play_session_present": bool(response_data.get("PlaySessionId")),
        "source_count": len(media_sources) if isinstance(media_sources, list) else -1,
        "rewritten": bool(changed),
        "sources": source_summaries,
    }


def _authorization_device_ids(value: Any) -> list[str]:
    values: list[str] = []
    for match in _AUTH_DEVICE_ID_RE.finditer(str(value or "")):
        device_id = str(match.group(1) or match.group(2) or "").strip()
        if device_id:
            values.append(device_id)
    return values


def _authorization_device_id(value: Any) -> str:
    values = _authorization_device_ids(value)
    return values[0] if values else ""


def _request_device_ids(request: Any) -> list[str]:
    values = _request_query_values(request, "DeviceId")
    for key in ("X-Emby-Device-Id", "X-Jellyfin-Device-Id"):
        values.extend(_header_values(request.headers, key))
    for key in ("X-Emby-Authorization", "Authorization"):
        for header_value in _header_values(request.headers, key):
            values.extend(_authorization_device_ids(header_value))
    return [str(value).strip() for value in values if str(value).strip()]


def _request_device_id(request: Any) -> str:
    values = _request_device_ids(request)
    return values[0] if values else ""


def _request_device_id_has_conflict(request: Any) -> bool:
    return len(set(_request_device_ids(request))) > 1


def _request_client_fingerprint(request: Any) -> str:
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "").strip().casefold()
    if not host and isinstance(client, (tuple, list)) and client:
        host = str(client[0] or "").strip().casefold()
    return (
        _auth_scope_fingerprint(f"mediaflux-native-client:{host}")
        if host
        else ""
    )


def _request_is_cross_protocol_media_target(
    request: Any,
    signed_url: str,
) -> bool:
    source_scheme = str(request.url.scheme or "").casefold()
    target_scheme = str(urlsplit(str(signed_url or "")).scheme or "").casefold()
    return bool(
        source_scheme in {"http", "https"}
        and target_scheme in {"http", "https"}
        and source_scheme != target_scheme
    )


def _browser_direct_target_is_secure(signed_url: str) -> bool:
    """浏览器真 302 只下发 HTTPS bearer URL，HTTP 目标统一走 relay。"""
    target_scheme = str(urlsplit(str(signed_url or "")).scheme or "").casefold()
    return target_scheme == "https"


def _request_uses_browser_media_element(request: Any) -> bool:
    """识别浏览器媒体元素或 hls.js/fetch 发起的媒体请求。"""
    headers = request.headers
    destination = str(headers.get("Sec-Fetch-Dest") or "").strip().casefold()
    if destination in {"audio", "video"}:
        return True
    user_agent = str(headers.get("User-Agent") or "").casefold()
    origin = str(headers.get("Origin") or "").strip()
    fetch_mode = str(headers.get("Sec-Fetch-Mode") or "").strip().casefold()
    return bool(
        origin
        and "mozilla/" in user_agent
        and fetch_mode in {"cors", "no-cors", "same-origin"}
    )


def _request_allows_browser_direct_redirect(request: Any) -> bool:
    """只允许原生 HTMLMediaElement 的 no-cors GET 跟随跨域 signed URL。

    Jellyfin Web 的 hls.js 使用 XHR/fetch（cors）读取媒体；即使 source 已由
    PlaybackInfo 判定为可 Direct Play，302 到未开放 CORS 的 CDN 仍会被浏览器
    拦截。原生 video/audio 且 no-cors 的请求则可以安全保持真正的 302 数据链。
    """
    headers = request.headers
    destination = str(headers.get("Sec-Fetch-Dest") or "").strip().casefold()
    fetch_mode = str(headers.get("Sec-Fetch-Mode") or "").strip().casefold()
    return bool(
        destination in {"audio", "video"}
        and fetch_mode in {"", "no-cors"}
    )


def _signed_media_request_headers(
    request: Any,
    *,
    head_probe: bool = False,
) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in _SIGNED_MEDIA_REQUEST_HEADERS
    }
    _replace_header(headers, "Accept-Encoding", "identity")
    if head_probe:
        # 光鸭部分 CDN 节点会对 HEAD + video/* 或 application/json 返回 406；
        # bytes=0-0 又会异常返回 200 / Content-Length: 1。探测统一请求 1 KiB，
        # 再由 MediaFlux 根据 Content-Range 为客户端合成正确的 HEAD 元数据。
        has_client_range = any(
            key.casefold() == "range" and str(value or "").strip()
            for key, value in headers.items()
        )
        for key in tuple(headers):
            lowered = key.casefold()
            if lowered in {"accept", "range"} or (
                lowered == "if-range" and not has_client_range
            ):
                headers.pop(key, None)
        headers["Accept"] = "*/*"
        headers["Range"] = "bytes=0-1023"
    return headers


def _signed_media_response_headers(headers: httpx.Headers) -> dict[str, str]:
    result = {
        key: value
        for key, value in headers.items()
        if key.lower() in _SIGNED_MEDIA_RESPONSE_HEADERS
    }
    result["Cache-Control"] = "private, no-store, no-cache, max-age=0"
    result["Pragma"] = "no-cache"
    result["Referrer-Policy"] = "no-referrer"
    return result


def _head_probe_response(
    status_code: int,
    headers: dict[str, str],
    *,
    requested_range: str,
) -> tuple[int, dict[str, str]]:
    """把内部 1 KiB GET 探测还原为客户端预期的 HEAD 响应。"""
    normalized = dict(headers)
    content_range_key = next(
        (key for key in normalized if key.casefold() == "content-range"),
        None,
    )
    content_length_key = next(
        (key for key in normalized if key.casefold() == "content-length"),
        None,
    )
    content_range = normalized.get(content_range_key, "") if content_range_key else ""
    range_match = re.fullmatch(
        r"\s*bytes\s+(?:\d+-\d+|\*)/(\d+)\s*",
        content_range,
        re.I,
    )
    total_size: int | None = int(range_match.group(1)) if range_match else None

    # 上游返回 200 表示它忽略了 Range，或 If-Range 条件未满足；必须保持
    # 完整表示语义，不能自行伪造成 206。
    if status_code == 200:
        if content_length_key:
            raw_length = str(normalized.get(content_length_key) or "").strip()
            if raw_length.isdigit():
                total_size = int(raw_length)
        if content_range_key:
            normalized.pop(content_range_key, None)
        if total_size is not None:
            normalized[content_length_key or "Content-Length"] = str(total_size)
        return 200, normalized

    if status_code != 206 or total_size is None:
        return status_code, normalized

    if content_range_key:
        normalized.pop(content_range_key, None)
    requested = str(requested_range or "").strip()
    if not requested:
        normalized[content_length_key or "Content-Length"] = str(total_size)
        return 200, normalized

    try:
        selected = _parse_range(requested, total_size)
    except (TypeError, ValueError):
        selected = None
    if selected is None:
        if content_length_key:
            normalized.pop(content_length_key, None)
        normalized["Content-Range"] = f"bytes */{total_size}"
        return 416, normalized

    range_start, range_end = selected
    normalized[content_length_key or "Content-Length"] = str(
        range_end - range_start + 1
    )
    normalized["Content-Range"] = (
        f"bytes {range_start}-{range_end}/{total_size}"
    )
    return 206, normalized


def _header_values(headers: Any, name: str) -> list[str]:
    """返回同名请求头的每个字段值，避免重复鉴权头被折叠或遗漏。"""
    raw_values: list[Any] | None = None
    for method_name in ("getlist", "get_list"):
        getter = getattr(headers, method_name, None)
        if not callable(getter):
            continue
        try:
            values = getter(name)
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(values, (str, bytes)):
            raw_values = [values]
        else:
            raw_values = list(values or [])
        break

    if raw_values is None:
        normalized_name = str(name).casefold()
        items = getattr(headers, "items", None)
        if callable(items):
            raw_values = [
                value
                for key, value in items()
                if str(key).casefold() == normalized_name
            ]
        else:
            getter = getattr(headers, "get", None)
            value = getter(name) if callable(getter) else None
            raw_values = [] if value is None else [value]

    result: list[str] = []
    for raw_value in raw_values:
        if isinstance(raw_value, bytes):
            value = raw_value.decode("latin-1", errors="replace").strip()
        else:
            value = str(raw_value or "").strip()
        if value:
            result.append(value)
    return result


def _request_auth_credentials(request: Any) -> list[str]:
    headers = request.headers
    values: list[str] = []

    for header_name in ("X-Emby-Token", "X-MediaBrowser-Token"):
        values.extend(_header_values(headers, header_name))

    values.extend(_request_query_auth_credentials(request))

    for header_name in ("Authorization", "X-Emby-Authorization"):
        for header_value in _header_values(headers, header_name):
            values.extend(_authorization_tokens(header_value))
    return values


def _request_query_auth_credentials(request: Any) -> list[str]:
    """只提取原始 query 中携带的媒体服务器用户凭据。"""
    query = request.query_params
    if hasattr(query, "multi_items"):
        query_items = list(query.multi_items())
    elif hasattr(query, "items"):
        query_items = list(query.items())
    else:
        query_items = []
    credential_keys = {
        "api_key",
        "apikey",
        "x-emby-token",
        "x-mediabrowser-token",
    }
    values: list[str] = []
    for key, raw_value in query_items:
        if str(key).casefold() not in credential_keys:
            continue
        value = str(raw_value or "").strip()
        if value:
            values.append(value)
    return values


def _request_query_auth_credential(request: Any) -> str:
    """返回 query 中唯一的用户凭据；冲突输入不做隐式选择。"""
    values = tuple(dict.fromkeys(_request_query_auth_credentials(request)))
    return values[0] if len(values) == 1 else ""


def _request_auth_credential(request: Any) -> str:
    values = _request_auth_credentials(request)
    return values[0] if values else ""


def _request_auth_has_conflict(request: Any) -> bool:
    return len(set(_request_auth_credentials(request))) > 1


def _request_has_websocket_auth_signal(request: Any) -> bool:
    """判断 WebSocket 握手是否携带任何可用的认证信号。

    Jellyfin Web 登录前会先建立一次匿名 ``/socket``，上游按预期返回
    401/403；登录后浏览器可能只通过 Cookie 维持会话，因此 Cookie 也必须
    视为认证信号，不能仅凭 query/header 中没有显式 Token 就降级真实故障。
    """
    if _request_auth_credentials(request):
        return True
    # 日志分级应比凭据解析更保守：即使 Authorization 格式不是当前支持的
    # MediaBrowser Token=...，它仍代表一次真实认证尝试，失败必须保留 WARNING。
    return any(
        str(value or "").strip()
        for header_name in (
            "Authorization",
            "X-Emby-Authorization",
            "X-Emby-Token",
            "X-MediaBrowser-Token",
            "Cookie",
        )
        for value in _header_values(request.headers, header_name)
    )


def _websocket_handshake_is_anonymous_rejection(
    request: Any,
    status: int,
) -> bool:
    return bool(
        int(status or 0) in {401, 403}
        and not _request_has_websocket_auth_signal(request)
    )


def _auth_scope_fingerprint(credential: str) -> str:
    value = str(credential or "").encode("utf-8")
    return hmac.new(_AUTH_SCOPE_SECRET, value, hashlib.sha256).hexdigest() if value else ""


def _upstream_playback_token_scope(instance_id: int, token: str) -> str:
    """把上游 PlaySessionId 绑定为仅进程内、同地址复核的短时授权域。"""
    normalized = PlaybackSessionRegistry._token(token)
    if not normalized:
        return ""
    return _auth_scope_fingerprint(
        "mediaflux-upstream-playback-token:"
        f"{int(instance_id)}:{normalized}"
    )


def _upstream_playback_session_scope(
    instance_id: int,
    token: str,
    device_id: str,
) -> str:
    """把 PlaySessionId 与设备绑定为仅进程内可复现的短时授权域。"""
    normalized = PlaybackSessionRegistry._token(token)
    normalized_device = str(device_id or "").strip()
    if not normalized or not normalized_device:
        return ""
    return _auth_scope_fingerprint(
        "mediaflux-upstream-playback-session:"
        f"{int(instance_id)}:{normalized}:{normalized_device}"
    )


def _playback_source_signature(
    instance_id: int,
    item_id: str,
    source_id: str,
    playback_session_token: str,
) -> str:
    token = str(playback_session_token or "").strip()
    if not token:
        return ""
    payload = (
        f"mediaflux-playback-source\0{int(instance_id)}\0"
        f"{_normalized_media_identifier(item_id)}\0"
        f"{_normalized_media_identifier(source_id)}\0{token}"
    ).encode("utf-8")
    return hmac.new(_AUTH_SCOPE_SECRET, payload, hashlib.sha256).hexdigest()[:48]


def _request_auth_scope(request: Any) -> str:
    return _auth_scope_fingerprint(_request_auth_credential(request))


def _media_browser_authorization(credential: str) -> str:
    token = str(credential or "").strip()
    if not token or any(character in token for character in "\r\n"):
        return ""
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    return f'MediaBrowser Token="{escaped}"'


def _is_single_media_browser_authorization(value: str, credential: str) -> bool:
    raw_value = str(value or "").strip()
    tokens = _authorization_tokens(raw_value)
    return bool(
        _MEDIA_BROWSER_AUTH_RE.match(raw_value)
        and len(tokens) == 1
        and tokens[0] == str(credential or "").strip()
    )


def _apply_upstream_credential(
    headers: dict[str, str],
    credential: str,
    server_type: str = "",
) -> dict[str, str]:
    token = str(credential or "").strip()
    if not token:
        return headers
    normalized_type = str(server_type or "").strip().casefold()
    lowered = {key.lower() for key in headers}
    if normalized_type == "jellyfin":
        # Jellyfin 12 的受保护 HTTP/WS 接口使用唯一的 canonical
        # MediaBrowser Authorization。只有现有 Authorization 的 Token 与
        # 已校验 credential 完全一致时才保留其客户端元数据；无关 Bearer、
        # legacy token 与 X-Emby-Authorization 均不得覆盖 canonical token。
        preserved_authorization = ""
        for key in tuple(headers):
            normalized_key = key.lower()
            if normalized_key == "authorization":
                value = str(headers.pop(key, "") or "").strip()
                if (
                    not preserved_authorization
                    and _is_single_media_browser_authorization(value, token)
                ):
                    preserved_authorization = value
            elif normalized_key in {
                "x-emby-token",
                "x-mediabrowser-token",
                "x-emby-authorization",
            }:
                headers.pop(key, None)
        authorization = preserved_authorization or _media_browser_authorization(token)
        if authorization:
            headers["Authorization"] = authorization
        return headers
    if not lowered.intersection({"x-emby-token", "x-mediabrowser-token"}):
        headers["X-Emby-Token"] = token
    return headers


def _upstream_request_headers(
    request: Any,
    server_type: str = "",
) -> dict[str, str]:
    credential = _request_auth_credential(request)
    headers = _apply_canonical_forwarded_headers(
        _request_headers(request), request
    )
    if str(server_type or "").strip().casefold() == "jellyfin" and credential:
        authorization_values = _header_values(request.headers, "Authorization")
        authorization_tokens = [
            token
            for value in authorization_values
            for token in _authorization_tokens(value)
        ]
        preserve_authorization = (
            len(authorization_values) == 1
            and len(authorization_tokens) == 1
            and _is_single_media_browser_authorization(
                authorization_values[0], credential
            )
        )
        if not preserve_authorization:
            for key in tuple(headers):
                if key.lower() == "authorization":
                    headers.pop(key, None)
    return _apply_upstream_credential(headers, credential, server_type)


def _sanitized_query_pairs(request: Any) -> list[tuple[str, str]]:
    query_params = getattr(request, "query_params", None)
    if query_params is not None and hasattr(query_params, "multi_items"):
        pairs = query_params.multi_items()
    else:
        pairs = parse_qsl(
            str(getattr(getattr(request, "url", None), "query", "")),
            keep_blank_values=True,
        )
    return [
        (str(key), str(value))
        for key, value in pairs
        if str(key).lower() not in _SENSITIVE_QUERY_KEYS
    ]


def _sanitized_query_string(request: Any) -> str:
    return urlencode(_sanitized_query_pairs(request), doseq=True)


def _upstream_query_string(request: Any, server_type: str = "") -> str:
    """构造上游 query；Jellyfin HLS 仅续传原请求 query 的用户凭据。"""
    pairs = _sanitized_query_pairs(request)
    is_jellyfin_hls = bool(
        str(server_type or "").strip().casefold() == "jellyfin"
        and _HLS_PATH_RE.search(str(request.url.path or ""))
    )
    if is_jellyfin_hls:
        # Jellyfin master playlist 会把当前 query 复制到 main/segment URI。
        # 仅把客户端原本放在 query 中且值唯一的用户 token 作为 canonical
        # api_key 续传给上游；header-only token 不扩大到 URL，且绝不使用
        # 实例管理员密钥为匿名 HLS 请求兜底。
        credential = _request_query_auth_credential(request)
        if credential:
            pairs.append(("api_key", credential))
    return urlencode(pairs, doseq=True)


def _playback_info_request_payload(body: bytes) -> dict[str, Any]:
    """尽力解析 PlaybackInfo JSON 请求体；无效或非对象内容按空对象处理。"""
    if not body or len(body) > _PLAYBACK_INFO_PREFERENCE_PARSE_MAX_BYTES:
        return {}
    try:
        payload = httpx.Response(
            status_code=200,
            content=body,
            headers={"content-type": "application/json"},
        ).json()
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _optional_request_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().casefold()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    return None


def _playback_info_direct_fallback_requested(
    request: Any,
    request_payload: dict[str, Any],
) -> bool:
    """识别客户端显式禁用 DirectPlay/DirectStream 的降级请求。"""
    preferences: dict[str, bool | None] = {
        "enabledirectplay": None,
        "enabledirectstream": None,
    }
    for key, value in _sanitized_query_pairs(request):
        normalized_key = key.casefold()
        if normalized_key in preferences:
            parsed = _optional_request_boolean(value)
            if parsed is not None:
                preferences[normalized_key] = parsed

    # Jellyfin Android 2.7 的 retry 2/3 将开关放在 POST JSON 顶层；body
    # 是实际 API 契约，存在明确布尔值时覆盖 query 中的同名值。
    for key in tuple(preferences):
        parsed = _optional_request_boolean(
            _diagnostic_dict_value(request_payload, key)
        )
        if parsed is not None:
            preferences[key] = parsed
    return any(value is False for value in preferences.values())


def _playback_info_query_string(
    request: Any,
    *,
    force_direct: bool = True,
) -> str:
    """保留客户端能力参数；仅默认请求才向上游要求直放/直传。"""
    pairs = _sanitized_query_pairs(request)
    if not force_direct:
        return urlencode(pairs, doseq=True)

    direct_keys = {"enabledirectplay", "enabledirectstream"}
    pairs = [
        (key, value)
        for key, value in pairs
        if key.casefold() not in direct_keys
    ]
    pairs.extend((
        ("EnableDirectPlay", "true"),
        ("EnableDirectStream", "true"),
    ))
    return urlencode(pairs, doseq=True)


def _parse_range(value: str, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("仅支持单段 bytes Range")
    start_text, separator, end_text = value[6:].partition("-")
    if not separator:
        raise ValueError("Range 格式无效")
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("Range 后缀无效")
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError("Range 超出文件范围")
    return start, min(end, size - 1)


def _file_chunks(path: Path, start: int, length: int, chunk_size: int = 1024 * 1024):
    remaining = length
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def local_file_response(request: Request, path: Path) -> Response:
    if not path.is_file():
        return JSONResponse({"error": "本地媒体文件不存在"}, status_code=404)
    stat = path.stat()
    size = stat.st_size
    etag = f'"{stat.st_mtime_ns:x}-{size:x}"'
    common = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
        "Content-Type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    }
    try:
        selected = _parse_range(request.headers.get("range", ""), size)
    except (ValueError, TypeError):
        return Response(
            status_code=416,
            headers={**common, "Content-Range": f"bytes */{size}"},
        )
    if selected is None:
        headers = {**common, "Content-Length": str(size)}
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers)
        return StreamingResponse(
            _file_chunks(path, 0, size),
            status_code=200,
            headers=headers,
            media_type=common["Content-Type"],
        )
    start, end = selected
    length = end - start + 1
    headers = {
        **common,
        "Content-Length": str(length),
        "Content-Range": f"bytes {start}-{end}/{size}",
    }
    if request.method == "HEAD":
        return Response(status_code=206, headers=headers)
    return StreamingResponse(
        _file_chunks(path, start, length),
        status_code=206,
        headers=headers,
        media_type=common["Content-Type"],
    )


async def _close_upstream(response: httpx.Response) -> None:
    await response.aclose()


async def _stream_and_close(
    response: httpx.Response,
    client: httpx.AsyncClient | None = None,
):
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        try:
            await response.aclose()
        finally:
            if client is not None:
                await client.aclose()


def _binding_value(binding: Any, key: str) -> Any:
    try:
        return binding[key]
    except (KeyError, IndexError, TypeError):
        return None


def _binding_has_field(binding: Any, key: str) -> bool:
    try:
        keys = binding.keys()
    except (AttributeError, TypeError):
        return False
    return key in keys


def _binding_signature(binding: Any) -> str:
    binding_id = _binding_value(binding, "id")
    if binding_id not in (None, ""):
        return f"id:{binding_id}"
    return "|".join([
        str(_binding_value(binding, "source_type") or ""),
        str(_binding_value(binding, "guangya_file_id") or ""),
        str(_binding_value(binding, "local_relative_path") or ""),
    ])


def _media_proxy_binding(
    instance_id: int,
    item_id: str,
    source_id: str,
    *,
    allow_item_level: bool = False,
    auth_scope: str = "",
) -> Any:
    """精确绑定直接返回；Item 级绑定必须先在单源 PlaybackInfo 中登记。"""
    requested_source = str(source_id or "").strip()
    normalized_requested_source = _normalized_media_identifier(requested_source)
    binding = None
    for item_variant in _media_identifier_variants(item_id):
        for source_variant in _media_identifier_variants(requested_source):
            candidate = database.get_media_proxy_binding(
                instance_id, item_variant, source_variant
            )
            if not candidate:
                continue
            bound_source = str(
                _binding_value(candidate, "media_source_id") or ""
            ).strip()
            if (
                bound_source
                and normalized_requested_source
                and _normalized_media_identifier(bound_source)
                != normalized_requested_source
            ):
                continue
            binding = candidate
            break
        if binding:
            break
    if not binding:
        return None
    if str(_binding_value(binding, "source_type") or "").strip().lower() != "guangya":
        return None
    bound_source = str(_binding_value(binding, "media_source_id") or "").strip()
    if bound_source:
        return binding
    if allow_item_level:
        return binding
    signature = _binding_signature(binding)
    if requested_source:
        return binding if _item_level_binding_scopes.matches(
            instance_id, item_id, requested_source, signature, auth_scope
        ) else None
    return binding if _item_level_binding_scopes.matches_item(
        instance_id, item_id, signature, auth_scope
    ) else None


def _native_stream_container(source: dict[str, Any]) -> str:
    """优先使用上游 Container，缺失时从原始媒体 Path 安全推断扩展名。"""
    declared = str(source.get("Container") or "").strip().casefold().split(",", 1)[0]
    if re.fullmatch(r"[a-z0-9]{1,12}", declared):
        return declared
    path = urlsplit(str(source.get("Path") or "")).path
    filename = path.rsplit("/", 1)[-1]
    suffix = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    return suffix if re.fullmatch(r"[a-z0-9]{1,12}", suffix) else ""


def rewrite_playback_info(
    payload: Any,
    instance_id: int,
    item_id: str,
    *,
    route_prefix: str = "",
    dynamic_mappings: DynamicGuangYaMappings | None = None,
    auth_scope: str = "",
    additional_auth_scopes: tuple[str, ...] = (),
    playback_session_token: str = "",
    native_client_mode: str = "",
    client_base_url: str = "",
    browser_direct_redirect_signatures: set[str] | None = None,
) -> tuple[Any, bool]:
    if not isinstance(payload, dict):
        return payload, False
    media_sources = payload.get("MediaSources")
    if not isinstance(media_sources, list):
        return payload, False
    mappings = dynamic_mappings or _dynamic_guangya_mappings
    grant_scopes = tuple(
        dict.fromkeys(
            scope
            for scope in (str(auth_scope or ""), *additional_auth_scopes)
            if scope
        )
    )
    changed = False
    allow_item_level = len(media_sources) == 1
    source_id_counts: dict[str, int] = {}
    for candidate in media_sources:
        if not isinstance(candidate, dict):
            continue
        normalized_source_id = _normalized_media_identifier(
            str(candidate.get("Id") or "")
        )
        source_id_counts[normalized_source_id] = (
            source_id_counts.get(normalized_source_id, 0) + 1
        )
    for source in media_sources:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("Id") or "")
        normalized_source_id = _normalized_media_identifier(source_id)
        browser_source_identity_unique = bool(
            allow_item_level
            or (
                normalized_source_id
                and source_id_counts.get(normalized_source_id, 0) == 1
            )
        )
        native_stream_container = (
            _native_stream_container(source)
            if native_client_mode == "jellyfin_android"
            else ""
        )
        if native_stream_container:
            # Android 2.7 的 DirectStream 回退会 requireNotNull(Container)；即使
            # 上游遗漏字段，也应把从原始 Path 安全推断的容器回填到响应。
            source["Container"] = native_stream_container
        source_signature = _playback_source_signature(
            instance_id, item_id, source_id, playback_session_token
        )
        emitted_source_signature = source_signature
        browser_direct_play_eligible = bool(
            str(native_client_mode or "").strip().casefold() == "web"
            and source.get("SupportsDirectPlay") is True
        )
        binding = _media_proxy_binding(
            instance_id,
            item_id,
            source_id,
            allow_item_level=allow_item_level,
            auth_scope=auth_scope,
        )
        if binding:
            direct_url = _direct_stream_path(
                item_id,
                source_id,
                route_prefix,
                playback_session_token,
                source_signature,
                native_stream_container,
            )
            # 真实数据库绑定始终含 media_source_id；旧式无该字段的绑定对象保留旧 Path。
            if _binding_has_field(binding, "media_source_id"):
                path = direct_url
            else:
                emitted_source_signature = _playback_source_signature(
                    instance_id, item_id, "", playback_session_token
                )
                path = _direct_stream_path(
                    item_id,
                    prefix=route_prefix,
                    playback_session_token=playback_session_token,
                    playback_source_signature=emitted_source_signature,
                    container=native_stream_container,
                )
            client_path = _client_stream_url(
                path, client_base_url, native_client_mode
            )
            client_direct_url = _client_stream_url(
                direct_url, client_base_url, native_client_mode
            )
            _mark_direct_source(
                source,
                client_path,
                client_direct_url,
                native_client_mode=native_client_mode,
            )
            if (
                browser_direct_play_eligible
                and browser_source_identity_unique
                and browser_direct_redirect_signatures is not None
            ):
                browser_direct_redirect_signatures.add(
                    emitted_source_signature
                )
            for grant_scope in grant_scopes:
                if not str(_binding_value(binding, "media_source_id") or "").strip():
                    _item_level_binding_scopes.register(
                        instance_id,
                        item_id,
                        source_id,
                        _binding_signature(binding),
                        grant_scope,
                    )
                _playback_grants.register(
                    grant_scope,
                    instance_id,
                    item_id,
                    source_id,
                    source_type=str(
                        _binding_value(binding, "source_type") or ""
                    ),
                    file_id=str(
                        _binding_value(binding, "guangya_file_id") or ""
                    ),
                    binding_signature=_binding_signature(binding),
                )
            changed = True
            continue

        file_id = _extract_guangya_file_id(source.get("Path"))
        if not file_id:
            continue
        mappings.register(instance_id, item_id, source_id, file_id)
        for grant_scope in grant_scopes:
            _playback_grants.register(
                grant_scope,
                instance_id,
                item_id,
                source_id,
                source_type="dynamic",
                file_id=file_id,
            )
        direct_url = _direct_stream_path(
            item_id,
            source_id,
            route_prefix,
            playback_session_token,
            source_signature,
            native_stream_container,
        )
        client_direct_url = _client_stream_url(
            direct_url, client_base_url, native_client_mode
        )
        _mark_direct_source(
            source,
            client_direct_url,
            client_direct_url,
            native_client_mode=native_client_mode,
        )
        if (
            browser_direct_play_eligible
            and browser_source_identity_unique
            and browser_direct_redirect_signatures is not None
        ):
            browser_direct_redirect_signatures.add(source_signature)
        changed = True
    return payload, changed


def _authorization_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for match in _AUTH_TOKEN_RE.finditer(str(value or "")):
        token = str(match.group(1) or match.group(2) or "").strip()
        if token:
            tokens.append(token)
    return tokens


def _authorization_token(value: str) -> str:
    tokens = _authorization_tokens(value)
    return tokens[0] if tokens else ""


async def _client_is_authorized(
    instance,
    request: Request,
    *,
    blocking_runner: Callable[..., Awaitable[Any]] | None = None,
    raise_timeout: bool = False,
) -> bool:
    if _request_auth_has_conflict(request):
        return False
    token = _request_auth_credential(request)
    if not token:
        return False
    try:
        if blocking_runner is None:
            pinned = await asyncio.to_thread(
                _pin_upstream_target, str(instance["upstream_url"]), "/Users/Me"
            )
        else:
            pinned = await blocking_runner(
                _pin_upstream_target, str(instance["upstream_url"]), "/Users/Me"
            )
    except _SignedMediaProbeCapacityError:
        raise
    except TimeoutError:
        if raise_timeout:
            raise
        return False
    except ValueError:
        return False
    headers = _upstream_request_headers(
        request,
        str(instance.get("server_type") or ""),
    )
    headers["Host"] = pinned.host_header
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=10,
            trust_env=False,
        ) as client:
            upstream_request = client.build_request(
                "GET",
                pinned.connect_url,
                headers=headers,
                extensions={"sni_hostname": pinned.sni_hostname},
            )
            response = await client.send(upstream_request)
        return 200 <= response.status_code < 300
    except (TimeoutError, httpx.TimeoutException) as exc:
        if raise_timeout:
            raise TimeoutError("媒体服务器鉴权校验超时") from exc
        logger.warning(
            f"媒体反代鉴权校验失败 instance={instance['id']}: {type(exc).__name__}"
        )
        return False
    except Exception as exc:
        logger.warning(
            f"媒体反代鉴权校验失败 instance={instance['id']}: {type(exc).__name__}"
        )
        return False


def _websocket_upstream_url(upstream: str, websocket: WebSocket, path: str) -> str:
    target = httpx.URL(_upstream_target(upstream, "/" + str(path).lstrip("/")))
    scheme = "wss" if target.scheme == "https" else "ws"
    websocket_target = str(target.copy_with(scheme=scheme))
    query = _sanitized_query_string(websocket) if hasattr(websocket, "query_params") else websocket.url.query
    return f"{websocket_target}?{query}" if query else websocket_target


def create_proxy_app(
    instance_id: int,
    signed_urls: SignedUrlCache | None = None,
    playback_record_writer: PlaybackRecordWriter | None = None,
) -> FastAPI:
    recorder = playback_record_writer or PlaybackRecordWriter(
        task_name=f"media-proxy-playback-records-{instance_id}"
    )
    upstream_clients = _UpstreamClientPool()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await recorder.start()
        try:
            yield
        finally:
            try:
                await upstream_clients.aclose()
            finally:
                try:
                    await recorder.stop()
                finally:
                    _release_signed_url_cache(instance_id, signed_urls)

    app = FastAPI(
        title=f"MediaFlux Media Proxy {instance_id}",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.playback_record_writer = recorder
    app.state.upstream_clients = upstream_clients
    signed_urls = signed_urls or SignedUrlCache()
    browser_direct_targets = BrowserDirectTargetCache()
    _register_signed_url_cache(instance_id, signed_urls)
    signed_media_probe_slots = asyncio.Semaphore(
        _SIGNED_MEDIA_PROBE_MAX_CONCURRENCY
    )

    async def run_signed_media_probe_blocking(
        func: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """在进程级共享有界线程池执行 signed media 的同步 SDK/DNS 调用。

        请求被绝对 deadline 取消后，系统级 DNS/SDK 调用未必能立刻停止；
        worker 槽位必须等底层 Future 真正结束后再释放，避免跨 runtime
        累积不可取消任务或空闲线程。
        """
        loop = asyncio.get_running_loop()
        worker_capacity = _signed_media_probe_worker_capacity
        worker_executor = _signed_media_probe_executor
        capacity_deadline = (
            loop.time()
            + max(0.001, _SIGNED_MEDIA_PROBE_QUEUE_TIMEOUT_SECONDS)
        )
        while not worker_capacity.acquire(blocking=False):
            remaining = capacity_deadline - loop.time()
            if remaining <= 0:
                raise _SignedMediaProbeCapacityError
            await asyncio.sleep(min(0.01, remaining))

        try:
            worker_future = worker_executor.submit(
                partial(func, *args, **kwargs)
            )
        except Exception:
            worker_capacity.release()
            raise

        def release_worker_slot(done_future) -> None:
            try:
                done_future.exception()
            except BaseException:
                pass
            worker_capacity.release()

        worker_future.add_done_callback(release_worker_slot)
        async_future = asyncio.wrap_future(worker_future, loop=loop)

        def consume_async_exception(done_future: asyncio.Future) -> None:
            if done_future.cancelled():
                return
            try:
                done_future.exception()
            except BaseException:
                pass

        async_future.add_done_callback(consume_async_exception)
        # shield 确保请求超时只停止等待，不会把尚在执行的同步 Future 标成
        # 已取消；容量由上面的 concurrent Future 完成回调按真实生命周期归还。
        return await asyncio.shield(async_future)

    @app.middleware("http")
    async def record_playback_attempt(request: Request, call_next):
        route_class = classify_proxy_route(request.url.path, request.method)
        credential = _request_auth_credential(request)
        credential_conflict = _request_auth_has_conflict(request)
        auth_scope = _auth_scope_fingerprint(credential)
        playback_match = _PLAYBACK_INFO_RE.match(request.url.path)
        stream_match = _STREAM_RE.match(request.url.path)
        video_match = (
            _VIDEO_ITEM_RE.match(request.url.path)
            if route_class == "upstream_hls"
            else None
        )
        media_match = playback_match or stream_match or video_match
        item_id = media_match.group(1) if media_match else ""
        source_values = _request_query_values(request, "MediaSourceId")
        source_id = source_values[0] if source_values else ""
        source_ambiguous = _request_query_has_conflicting_values(
            request, "MediaSourceId"
        )
        file_id = _extract_guangya_file_id(str(request.url)) or ""
        media_name = (
            _safe_media_name(str(request.url), path_value=True)
            if route_class == "guangya_direct" else ""
        )
        capability_token = _request_query_value(request, _PLAYBACK_SESSION_QUERY_KEY)
        source_signature = _request_query_value(request, _PLAYBACK_SOURCE_QUERY_KEY)
        upstream_session_token = _request_query_value(request, "PlaySessionId")
        upstream_session_ambiguous = _request_query_has_conflicting_values(
            request, "PlaySessionId"
        )
        device_id = _request_device_id(request)
        device_id_ambiguous = _request_device_id_has_conflict(request)
        playback_parameters_ambiguous = bool(
            ((stream_match or video_match) and source_ambiguous)
            or (
                (stream_match or playback_match or video_match)
                and (upstream_session_ambiguous or device_id_ambiguous)
            )
        )
        capability_authorized = False
        capability_rejected = False
        playback_session = None
        native_stream_resolution = (
            "credential" if stream_match and auth_scope else "anonymous"
        )
        if credential_conflict:
            # 冲突凭据必须在触碰有限容量的播放会话注册表前拒绝。
            pass
        elif route_class == "playback_info":
            # 仅在上游 PlaybackInfo 成功且即将返回重写响应时签发 capability；
            # 失败/非法响应不得占用 capability 索引或驱逐正在播放的会话。
            playback_session = None
        elif capability_token:
            playback_session = None
            if stream_match and request.method in {"GET", "HEAD"}:
                candidate = _playback_sessions.resolve_capability(
                    instance_id,
                    capability_token,
                    item_id=item_id,
                    source_id=source_id,
                    source_signature=source_signature,
                )
                candidate_allowed = bool(
                    candidate is not None
                    and (not auth_scope or candidate.auth_scope == auth_scope)
                )
                if (
                    candidate_allowed
                    and candidate is not None
                    and not auth_scope
                    and (
                        candidate.native_cross_protocol_relay
                        or candidate.native_signed_media_relay
                    )
                ):
                    native_client_fingerprint = _request_client_fingerprint(
                        request
                    )
                    candidate_allowed = bool(
                        candidate.native_verified_auth_scope
                        and native_client_fingerprint
                        and hmac.compare_digest(
                            candidate.native_client_fingerprint,
                            native_client_fingerprint,
                        )
                    )
                if candidate_allowed and candidate is not None:
                    playback_session = candidate
                    if not auth_scope:
                        auth_scope = candidate.auth_scope
                        capability_authorized = True
                else:
                    capability_rejected = True
            else:
                capability_rejected = True
        elif (
            not auth_scope
            and upstream_session_token
            and stream_match
            and request.method in {"GET", "HEAD"}
        ):
            # Jellyfin Android 的 ExoPlayer 可能不在视频 DataSource 请求中携带
            # access token，但一定会回传 PlaybackInfo 的 PlaySessionId。只接受
            # 本进程刚签发、item/source 精确匹配的短时会话，不做跨会话猜测。
            session_scope = _upstream_playback_session_scope(
                instance_id, upstream_session_token, device_id
            )
            playback_session = _playback_sessions.resolve(
                session_scope,
                instance_id,
                token=upstream_session_token,
                item_id=item_id,
                source_id=source_id,
                create=False,
            )
            native_client_fingerprint = _request_client_fingerprint(request)
            if (
                playback_session is not None
                and playback_session.native_cross_protocol_relay
                and playback_session.native_verified_auth_scope
                and native_client_fingerprint
                and hmac.compare_digest(
                    playback_session.native_client_fingerprint,
                    native_client_fingerprint,
                )
            ):
                auth_scope = session_scope
                capability_authorized = True
                native_stream_resolution = "device_scope"
            else:
                native_stream_resolution = (
                    "device_scope_rejected"
                    if playback_session is not None
                    else "device_scope_missing"
                )
                # Android 2.7 的 PlaybackInfo 请求并不保证携带 DeviceId，而视频
                # URL 又可能补上 DeviceId。设备标识只是客户端元数据，不能让它
                # 阻断已经由上游返回、同客户端地址、精确 item/source 绑定的
                # PlaySessionId bearer 会话，因此设备 scope 未命中时统一回退到
                # token scope。
                token_scope = _upstream_playback_token_scope(
                    instance_id, upstream_session_token
                )
                playback_session = _playback_sessions.resolve(
                    token_scope,
                    instance_id,
                    token=upstream_session_token,
                    item_id=item_id,
                    source_id=source_id,
                    create=False,
                )
                if (
                    playback_session is not None
                    and playback_session.native_cross_protocol_relay
                    and playback_session.native_verified_auth_scope
                    and native_client_fingerprint
                    and hmac.compare_digest(
                        playback_session.native_client_fingerprint,
                        native_client_fingerprint,
                    )
                ):
                    auth_scope = token_scope
                    capability_authorized = True
                    native_stream_resolution = "token_scope"
                else:
                    native_stream_resolution = (
                        "token_scope_rejected"
                        if playback_session is not None
                        else "token_scope_missing"
                    )
                    playback_session = _playback_sessions.resolve(
                        "",
                        instance_id,
                        token=upstream_session_token,
                        item_id=item_id,
                        source_id=source_id,
                        create=True,
                    )
        elif route_class == "guangya_direct" and not upstream_session_token:
            playback_session = _playback_sessions.begin(
                auth_scope, instance_id, file_id=file_id, media_name=media_name
            )
        else:
            playback_session = _playback_sessions.resolve(
                auth_scope,
                instance_id,
                token=upstream_session_token,
                item_id=item_id,
                source_id=source_id,
                file_id=file_id,
                media_name=media_name,
                create=(
                    bool(upstream_session_token)
                    or route_class in {"stream", "guangya_direct"}
                ),
            )
        request.state.proxy_auth_scope = auth_scope
        request.state.proxy_native_stream_resolution = native_stream_resolution
        request.state.proxy_capability_authorized = capability_authorized
        request.state.proxy_source_ambiguous = source_ambiguous
        request.state.proxy_playback_session_token = ""
        request.state.proxy_playback_session_key = ""
        request.state.proxy_browser_relay = False
        request.state.proxy_browser_direct_redirect = False
        request.state.proxy_native_cross_protocol_relay = False
        request.state.proxy_native_signed_media_relay = False
        request.state.proxy_media_item_id = item_id
        request.state.proxy_media_source_id = source_id
        request.state.proxy_guangya_file_id = file_id
        request.state.proxy_media_name = media_name
        _apply_playback_session(request, playback_session)
        if stream_match and not source_id:
            # 请求未指定 MediaSourceId 时不得继承同一 PlaySession 上一次触碰的
            # source。保持为空，让后续 mapping/grant 只在该 item 恰有唯一来源时
            # 自动消歧，避免多源条目误复用旧来源直链。
            request.state.proxy_media_source_id = ""
        request.state.proxy_route_class = route_class
        request.state.proxy_source = {
            "guangya_direct": "guangya",
            "playback_info": "playback_info",
            "upstream_hls": "hls",
        }.get(route_class, "upstream")
        request.state.proxy_action = {
            "guangya_direct": "guangya_302",
            "playback_info": "playback_passthrough",
            "upstream_hls": "upstream_hls",
            "stream": "upstream_stream",
        }.get(route_class, "upstream_passthrough")
        request.state.proxy_cache_hit = False
        request.state.proxy_upstream_latency_ms = 0
        request.state.proxy_failure_stage = ""
        if credential_conflict:
            request.state.proxy_failure_stage = "client_auth"
        elif playback_parameters_ambiguous:
            request.state.proxy_failure_stage = "query_parameters"
        elif capability_rejected:
            request.state.proxy_failure_stage = "playback_capability"
        started = time.monotonic()
        response: Response | None = None
        error_type = ""
        try:
            if credential_conflict:
                response = JSONResponse(
                    {"error": "媒体服务器凭据参数冲突"}, status_code=400
                )
                return response
            if playback_parameters_ambiguous:
                response = JSONResponse(
                    {"error": "播放参数重复或冲突"}, status_code=400
                )
                return response
            if capability_rejected:
                response = JSONResponse(
                    {"error": "播放会话无效或已过期"}, status_code=401
                )
                return response
            response = await call_next(request)
            return response
        except Exception as exc:
            error_type = type(exc).__name__
            request.state.proxy_failure_stage = (
                getattr(request.state, "proxy_failure_stage", "") or "proxy"
            )
            raise
        finally:
            total_latency_ms = round((time.monotonic() - started) * 1000)
            status_code = int(
                getattr(response, "status_code", 500 if error_type else 0) or 0
            )
            if status_code >= 400 and not request.state.proxy_failure_stage:
                request.state.proxy_failure_stage = "proxy"
            diagnostic = {
                "route": getattr(request.state, "proxy_route_class", route_class),
                "action": getattr(request.state, "proxy_action", "upstream_passthrough"),
                "method": str(request.method or "GET").upper(),
                "status": status_code,
                "range": _range_diagnostic(request.headers.get("range", "")),
                "if_range": int(bool(request.headers.get("if-range", ""))),
                "ua": int(bool(request.headers.get("user-agent", ""))),
                "cache_hit": int(bool(getattr(request.state, "proxy_cache_hit", False))),
                "upstream_ms": int(
                    getattr(request.state, "proxy_upstream_latency_ms", 0) or 0
                ),
                "total_ms": total_latency_ms,
                "failure_stage": getattr(request.state, "proxy_failure_stage", "") or "none",
            }
            logger.debug(
                "media_proxy_diag instance=%s route=%s action=%s method=%s "
                "status=%s range=%s if_range=%s ua=%s cache_hit=%s "
                "upstream_ms=%s total_ms=%s failure_stage=%s",
                instance_id,
                diagnostic["route"], diagnostic["action"], diagnostic["method"],
                diagnostic["status"], diagnostic["range"], diagnostic["if_range"],
                diagnostic["ua"], diagnostic["cache_hit"], diagnostic["upstream_ms"],
                diagnostic["total_ms"], diagnostic["failure_stage"],
            )
            if route_class != "upstream":
                recorder.enqueue({
                    "instance_id": instance_id,
                    "route_class": diagnostic["route"],
                    "method": diagnostic["method"],
                    "status_code": status_code,
                    "source": getattr(request.state, "proxy_source", "upstream"),
                    "cache_hit": bool(diagnostic["cache_hit"]),
                    "upstream_latency_ms": diagnostic["upstream_ms"],
                    "total_latency_ms": total_latency_ms,
                    "failure_stage": (
                        "" if diagnostic["failure_stage"] == "none"
                        else diagnostic["failure_stage"]
                    ),
                    "error": error_type,
                    "playback_session_key": getattr(
                        request.state, "proxy_playback_session_key", ""
                    ),
                    "media_item_id": getattr(request.state, "proxy_media_item_id", ""),
                    "media_source_id": getattr(
                        request.state, "proxy_media_source_id", ""
                    ),
                    "guangya_file_id": getattr(
                        request.state, "proxy_guangya_file_id", ""
                    ),
                    "media_name": getattr(request.state, "proxy_media_name", ""),
                })

    async def _relay_signed_media(request: Request, signed_url: str) -> Response:
        current_url = str(signed_url or "").strip()
        started = time.monotonic()
        relay_client: httpx.AsyncClient | None = None
        is_head_probe = request.method == "HEAD"
        # CDN 的 HEAD 行为并不稳定：部分节点会因 Accept 返回 406，另一些节点
        # 会回 Content-Length: 0。统一改用流式 GET 探测响应头，正文不会被读取。
        upstream_method = "GET" if is_head_probe else request.method
        requested_range = str(request.headers.get("range") or "").strip()

        async def close_relay_client() -> None:
            nonlocal relay_client
            if relay_client is not None:
                client, relay_client = relay_client, None
                await client.aclose()

        redirect_count = 0
        try:
            while True:
                try:
                    pinned = await run_signed_media_probe_blocking(
                        _pin_signed_media_target,
                        current_url,
                    )
                except _SignedMediaProbeCapacityError:
                    await close_relay_client()
                    request.state.proxy_failure_stage = "signed_url_probe_capacity"
                    return JSONResponse(
                        {"error": "媒体直链探测繁忙，请稍后重试"},
                        status_code=503,
                    )
                except ValueError as exc:
                    await close_relay_client()
                    request.state.proxy_failure_stage = "signed_url_target"
                    logger.warning(
                        "媒体反代直链回源地址无效 instance=%s reason=%s",
                        instance_id,
                        str(exc),
                    )
                    return JSONResponse({"error": "媒体直链地址无效"}, status_code=502)

                if relay_client is None:
                    relay_client = httpx.AsyncClient(
                        follow_redirects=False,
                        trust_env=False,
                        timeout=_signed_media_timeout(request.method),
                        limits=httpx.Limits(
                            max_connections=2,
                            max_keepalive_connections=0,
                        ),
                    )
                headers = _signed_media_request_headers(
                    request,
                    head_probe=is_head_probe,
                )
                headers["Host"] = pinned.host_header
                upstream_request = relay_client.build_request(
                    upstream_method,
                    pinned.connect_url,
                    headers=headers,
                    extensions={"sni_hostname": pinned.sni_hostname},
                )
                try:
                    response = await relay_client.send(
                        upstream_request,
                        stream=True,
                    )
                except httpx.TimeoutException as exc:
                    await close_relay_client()
                    request.state.proxy_failure_stage = "signed_url_timeout"
                    logger.warning(
                        "媒体反代直链回源超时 instance=%s type=%s",
                        instance_id,
                        type(exc).__name__,
                    )
                    return JSONResponse({"error": "媒体直链探测超时"}, status_code=504)
                except Exception as exc:
                    await close_relay_client()
                    request.state.proxy_failure_stage = "signed_url_connect"
                    logger.warning(
                        "媒体反代直链回源失败 instance=%s type=%s",
                        instance_id,
                        type(exc).__name__,
                    )
                    return JSONResponse({"error": "媒体直链暂不可用"}, status_code=502)

                if response.status_code in {301, 302, 303, 307, 308}:
                    location = str(response.headers.get("location") or "").strip()
                    await response.aclose()
                    if not location:
                        await close_relay_client()
                        request.state.proxy_failure_stage = "signed_url_redirect"
                        return JSONResponse(
                            {"error": "媒体直链重定向无效"}, status_code=502
                        )
                    if redirect_count >= _SIGNED_MEDIA_MAX_REDIRECTS:
                        await close_relay_client()
                        request.state.proxy_failure_stage = "signed_url_redirect"
                        return JSONResponse(
                            {"error": "媒体直链重定向过多"}, status_code=502
                        )
                    redirect_count += 1
                    current_url = urljoin(pinned.logical_url, location)
                    continue

                request.state.proxy_route_class = "guangya_direct"
                request.state.proxy_source = "guangya"
                request.state.proxy_action = "guangya_relay"
                request.state.proxy_upstream_latency_ms = round(
                    (time.monotonic() - started) * 1000
                )
                response_headers = _signed_media_response_headers(response.headers)
                if is_head_probe:
                    status_code, response_headers = _head_probe_response(
                        response.status_code,
                        response_headers,
                        requested_range=requested_range,
                    )
                    try:
                        await response.aclose()
                    finally:
                        await close_relay_client()
                    return Response(
                        status_code=status_code,
                        headers=response_headers,
                    )
                streaming_client, relay_client = relay_client, None
                return StreamingResponse(
                    _stream_and_close(response, streaming_client),
                    status_code=response.status_code,
                    headers=response_headers,
                )
        finally:
            await close_relay_client()

    async def guangya_redirect(instance, request: Request, file_id: str) -> Response:
        auth_scope = getattr(request.state, "proxy_auth_scope", "")
        playback_session = _playback_sessions.resolve(
            auth_scope,
            instance_id,
            token=getattr(request.state, "proxy_playback_session_token", ""),
            item_id=getattr(request.state, "proxy_media_item_id", ""),
            source_id=getattr(request.state, "proxy_media_source_id", ""),
            file_id=file_id,
            media_name=getattr(request.state, "proxy_media_name", ""),
            create=True,
        )
        _apply_playback_session(request, playback_session)
        request.state.proxy_route_class = "guangya_direct"
        request.state.proxy_source = "guangya"
        request.state.proxy_action = "guangya_302"
        is_head_probe = request.method == "HEAD"

        async def resolve_signed_media_response() -> Response:
            if not getattr(
                request.state, "proxy_capability_authorized", False
            ):
                try:
                    authorized = await _client_is_authorized(
                        instance,
                        request,
                        blocking_runner=(
                            run_signed_media_probe_blocking
                            if is_head_probe
                            else None
                        ),
                        raise_timeout=is_head_probe,
                    )
                except _SignedMediaProbeCapacityError:
                    request.state.proxy_failure_stage = (
                        "signed_url_probe_capacity"
                    )
                    return JSONResponse(
                        {"error": "媒体直链探测繁忙，请稍后重试"},
                        status_code=503,
                    )
                if not authorized:
                    request.state.proxy_failure_stage = "client_auth"
                    return JSONResponse(
                        {"error": "媒体服务器鉴权失败"},
                        status_code=401,
                    )

            client = GuangYaClient()
            if not client.logged_in:
                request.state.proxy_failure_stage = "provider_auth"
                signed_urls.clear()
                browser_direct_targets.clear()
                return JSONResponse({"error": "光鸭未登录"}, status_code=503)
            try:
                raw_client = client.raw
            except AttributeError:  # 兼容测试替身与旧包装器
                raw_client = getattr(client, "_raw", None)
            provider_token = str(getattr(raw_client, "token", "") or "")
            provider_scope = _auth_scope_fingerprint(provider_token)
            scope = f"{int(instance_id)}:{provider_scope}"
            ua_bound = bool(
                getattr(raw_client, "download_url_user_agent_bound", False)
            )

            async def fetch_url() -> str | None:
                options = {
                    "timeout": PLAYGY_SIGNED_URL_TIMEOUT_SECONDS,
                    "raise_timeout": True,
                }
                if is_head_probe:
                    return await run_signed_media_probe_blocking(
                        client.get_download_url,
                        file_id,
                        **options,
                    )
                return await asyncio.to_thread(
                    client.get_download_url,
                    file_id,
                    **options,
                )

            try:
                result = await signed_urls.get_or_fetch_result(
                    file_id,
                    fetch_url,
                    scope=scope,
                    user_agent=request.headers.get("user-agent", ""),
                    ua_bound=ua_bound,
                )
            except _SignedMediaProbeCapacityError:
                request.state.proxy_failure_stage = (
                    "signed_url_probe_capacity"
                )
                return JSONResponse(
                    {"error": "媒体直链探测繁忙，请稍后重试"},
                    status_code=503,
                )
            except TimeoutError:
                request.state.proxy_failure_stage = "signed_url_timeout"
                return JSONResponse(
                    {"error": "光鸭播放地址获取超时"},
                    status_code=504,
                )
            request.state.proxy_cache_hit = result.cache_hit
            if not result.url:
                request.state.proxy_failure_stage = "signed_url"
                return JSONResponse(
                    {"error": "无法获取光鸭直链"},
                    status_code=502,
                )
            client_url = str(result.url).strip()
            browser_direct_redirect = bool(
                not is_head_probe
                and getattr(
                    request.state, "proxy_browser_direct_redirect", False
                )
                and _request_allows_browser_direct_redirect(request)
                and _browser_direct_target_is_secure(client_url)
            )
            prefers_direct_redirect = _request_prefers_direct_signed_redirect(
                request
            )
            verified_native_chain_relay = bool(
                not prefers_direct_redirect
                and getattr(
                    request.state,
                    "proxy_native_signed_media_relay",
                    False,
                )
            )
            native_cross_protocol_relay = bool(
                not prefers_direct_redirect
                and not browser_direct_redirect
                and _request_is_cross_protocol_media_target(request, client_url)
                and (
                    _request_uses_exoplayer(request)
                    or getattr(
                        request.state,
                        "proxy_native_cross_protocol_relay",
                        False,
                    )
                )
            )
            browser_relay_required = bool(
                not browser_direct_redirect
                and (
                    getattr(request.state, "proxy_browser_relay", False)
                    or _request_uses_browser_media_element(request)
                )
            )
            if (
                is_head_probe
                or browser_relay_required
                or verified_native_chain_relay
                or native_cross_protocol_relay
            ):
                # HLS.js、转码与不具备直放能力的浏览器仍需同源响应规避 CDN
                # CORS；只有 PlaybackInfo 已由 Jellyfin 判定 SupportsDirectPlay
                # 的具体 source，才允许携带 capability 的媒体 GET 返回真 302。
                # Media3/ExoPlayer 的跨协议链路继续按原有兼容策略 relay。
                if verified_native_chain_relay:
                    request.state.proxy_action = "guangya_relay_native_chain"
                elif native_cross_protocol_relay:
                    request.state.proxy_action = "guangya_relay_cross_protocol"
                return await _relay_signed_media(request, client_url)
            redirect_url = client_url
            if browser_direct_redirect:
                # 浏览器会自行解析 Location，无法复用 relay 的物理 IP pinning；
                # 此处的解析结果只是对受信 provider 返回值做下发前的公网目标
                # 健全性检查，并不等同于钉住浏览器随后采用的 DNS 结果。真正需要
                # DNS pinning 的 HLS/XHR 与不安全协议降级仍统一走 relay。
                try:
                    if browser_direct_targets.contains(client_url):
                        pinned_redirect = None
                    else:
                        pinned_redirect = await asyncio.wait_for(
                            run_signed_media_probe_blocking(
                                _pin_signed_media_target,
                                client_url,
                            ),
                            timeout=max(
                                0.001,
                                _SIGNED_MEDIA_PROBE_TOTAL_TIMEOUT_SECONDS,
                            ),
                        )
                        browser_direct_targets.remember(client_url)
                except _SignedMediaProbeCapacityError:
                    request.state.proxy_failure_stage = (
                        "signed_url_probe_capacity"
                    )
                    return JSONResponse(
                        {"error": "媒体直链探测繁忙，请稍后重试"},
                        status_code=503,
                    )
                except TimeoutError:
                    request.state.proxy_failure_stage = "signed_url_probe_timeout"
                    return JSONResponse(
                        {"error": "媒体直链探测超时"},
                        status_code=504,
                    )
                except ValueError as exc:
                    request.state.proxy_failure_stage = "signed_url_target"
                    logger.warning(
                        "媒体反代浏览器直链地址无效 instance=%s reason=%s",
                        instance_id,
                        str(exc),
                    )
                    return JSONResponse(
                        {"error": "媒体直链地址无效"},
                        status_code=502,
                    )
                if pinned_redirect is not None:
                    redirect_url = pinned_redirect.logical_url
                request.state.proxy_action = "guangya_302_web_direct"
            return RedirectResponse(
                redirect_url,
                status_code=302,
                headers={
                    "Cache-Control": "private, no-store, no-cache, max-age=0",
                    "Pragma": "no-cache",
                    "Referrer-Policy": "no-referrer",
                },
            )

        if not is_head_probe:
            return await resolve_signed_media_response()

        # 原生 HEAD 的鉴权、排队、光鸭 signed URL 获取、DNS pinning 与
        # CDN 探测共用一个请求级绝对 deadline；不能把各阶段超时相加。
        loop = asyncio.get_running_loop()
        total_timeout = max(0.001, _SIGNED_MEDIA_PROBE_TOTAL_TIMEOUT_SECONDS)
        deadline = loop.time() + total_timeout
        queue_timeout = min(
            max(0.001, _SIGNED_MEDIA_PROBE_QUEUE_TIMEOUT_SECONDS),
            total_timeout,
        )
        try:
            await asyncio.wait_for(
                signed_media_probe_slots.acquire(),
                timeout=queue_timeout,
            )
        except TimeoutError:
            request.state.proxy_failure_stage = "signed_url_probe_capacity"
            return JSONResponse(
                {"error": "媒体直链探测繁忙，请稍后重试"},
                status_code=503,
            )

        try:
            try:
                async with asyncio.timeout_at(deadline):
                    return await resolve_signed_media_response()
            except TimeoutError:
                request.state.proxy_failure_stage = "signed_url_probe_timeout"
                return JSONResponse(
                    {"error": "媒体直链探测超时"},
                    status_code=504,
                )
        finally:
            signed_media_probe_slots.release()

    @app.websocket("/{path:path}")
    async def proxy_websocket(websocket: WebSocket, path: str):
        message_limit = _proxy_websocket_message_limit()
        if _request_auth_has_conflict(websocket):
            await websocket.close(code=1008, reason="Conflicting credentials")
            return
        try:
            instance = await asyncio.to_thread(_resolved_instance, instance_id)
        except ValueError as exc:
            await websocket.close(code=1013, reason=str(exc))
            return
        if not instance or not int(instance["enabled"] or 0):
            await websocket.close(code=1013, reason="Media proxy disabled")
            return
        headers = _upstream_request_headers(
            websocket,
            str(instance.get("server_type") or ""),
        )
        headers = {key: value for key, value in headers.items() if not key.lower().startswith("sec-websocket-")}
        protocols = websocket.scope.get("subprotocols") or []
        session: ClientSession | None = None
        try:
            pinned = await asyncio.to_thread(
                _pin_upstream_target,
                str(instance["upstream_url"]),
                "/" + str(path).lstrip("/"),
            )
            logical = httpx.URL(pinned.logical_url)
            scheme = "wss" if logical.scheme == "https" else "ws"
            target = str(logical.copy_with(scheme=scheme))
            query = _sanitized_query_string(websocket) if hasattr(websocket, "query_params") else websocket.url.query
            if query:
                target = f"{target}?{query}"
            connector = TCPConnector(
                resolver=_PinnedResolver(pinned.sni_hostname, pinned.addresses),
                use_dns_cache=True,
            )
            session = ClientSession(
                connector=connector,
                trace_configs=[_websocket_trace_config()],
            )
            upstream_ws = await session.ws_connect(
                target,
                headers=headers,
                protocols=protocols,
                autoping=True,
                heartbeat=30,
                max_msg_size=message_limit,
            )
        except _WebSocketRedirectBlocked:
            log_throttled(
                logger,
                logging.WARNING,
                f"media-proxy-ws-redirect:{instance_id}",
                "媒体反代 WebSocket 拒绝上游重定向 instance=%s",
                instance_id,
            )
            if session is not None:
                await session.close()
            await websocket.close(
                code=1011, reason="Upstream WebSocket redirect rejected"
            )
            return
        except WSServerHandshakeError as exc:
            status = int(getattr(exc, "status", 0) or 0)
            anonymous_rejection = _websocket_handshake_is_anonymous_rejection(
                websocket,
                status,
            )
            if anonymous_rejection:
                log_throttled(
                    logger,
                    logging.DEBUG,
                    f"media-proxy-ws-anonymous:{instance_id}:{status}",
                    "媒体反代匿名 WebSocket 被上游拒绝 instance=%s status=%s",
                    instance_id,
                    status,
                )
            else:
                log_throttled(
                    logger,
                    logging.WARNING,
                    f"media-proxy-ws-handshake:{instance_id}:{status}",
                    "媒体反代 WebSocket 上游握手失败 instance=%s status=%s",
                    instance_id,
                    status,
                )
            if session is not None:
                await session.close()
            await websocket.close(code=1011, reason="Upstream WebSocket unavailable")
            return
        except Exception as exc:
            error_type = type(exc).__name__
            log_throttled(
                logger,
                logging.WARNING,
                f"media-proxy-ws-connect:{instance_id}:{error_type}",
                "媒体反代 WebSocket 上游连接失败 instance=%s type=%s",
                instance_id,
                error_type,
            )
            if session is not None:
                await session.close()
            await websocket.close(code=1011, reason="Upstream WebSocket unavailable")
            return
        async def client_to_upstream():
            while True:
                message = await websocket.receive()
                kind = message.get("type")
                if kind == "websocket.disconnect":
                    break
                if message.get("text") is not None:
                    text = message["text"]
                    if len(text.encode("utf-8")) > message_limit:
                        await websocket.close(code=1009, reason="WebSocket message too large")
                        break
                    await upstream_ws.send_str(text)
                elif message.get("bytes") is not None:
                    payload = message["bytes"]
                    if len(payload) > message_limit:
                        await websocket.close(code=1009, reason="WebSocket message too large")
                        break
                    await upstream_ws.send_bytes(payload)

        async def upstream_to_client():
            async for message in upstream_ws:
                if message.type == WSMsgType.TEXT:
                    if len(message.data.encode("utf-8")) > message_limit:
                        await websocket.close(code=1009, reason="Upstream message too large")
                        break
                    await websocket.send_text(message.data)
                elif message.type == WSMsgType.BINARY:
                    if len(message.data) > message_limit:
                        await websocket.close(code=1009, reason="Upstream message too large")
                        break
                    await websocket.send_bytes(message.data)
                elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break

        tasks: set[asyncio.Task] = set()
        try:
            # 上游已建立后，下游可能已经离开；accept 也必须位于资源清理
            # 边界内，避免泄漏已连接的 upstream_ws / ClientSession。
            await websocket.accept(subprotocol=upstream_ws.protocol or None)
            tasks = {
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            }
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                try:
                    task.result()
                except Exception:
                    pass
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await upstream_ws.close()
            finally:
                await session.close()
            try:
                await websocket.close()
            except Exception:
                pass

    @app.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def proxy_request(request: Request, path: str):
        try:
            instance = await asyncio.to_thread(_resolved_instance, instance_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        if not instance or not int(instance["enabled"] or 0):
            return JSONResponse({"error": "媒体反代实例已停用"}, status_code=503)

        if _request_uses_exoplayer(request):
            logger.info(
                "media_proxy_native_request_diag instance=%s client=%s method=%s "
                "path=%s host=%s media_source=%s play_session=%s auth=%s",
                instance_id,
                _request_client_name(request) or "exoplayer",
                request.method,
                request.url.path,
                str(request.headers.get("host") or ""),
                int(bool(_request_query_value(request, "MediaSourceId"))),
                int(bool(_request_query_value(request, "PlaySessionId"))),
                int(bool(_request_auth_credential(request))),
            )

        credential = _request_auth_credential(request)
        auth_scope = (
            getattr(request.state, "proxy_auth_scope", "")
            or _auth_scope_fingerprint(credential)
        )
        playgy_file_id = _extract_guangya_file_id(str(request.url))
        if playgy_file_id and request.method in {"GET", "HEAD"}:
            if auth_scope and not _playback_grants.allows_file(auth_scope, instance_id, playgy_file_id):
                return JSONResponse({"error": "当前凭据没有该文件的播放授权"}, status_code=403)
            return await guangya_redirect(instance, request, playgy_file_id)

        match = _STREAM_RE.match(request.url.path)
        if match and request.method in {"GET", "HEAD"}:
            item_id = match.group(1)
            source_id = getattr(request.state, "proxy_media_source_id", "")
            binding = await asyncio.to_thread(
                _media_proxy_binding,
                instance_id,
                item_id,
                source_id,
                auth_scope=auth_scope,
            )
            binding_allowed = bool(binding and _playback_grants.matches(
                auth_scope, instance_id, item_id, source_id,
                binding_signature=_binding_signature(binding),
            ))
            if binding_allowed:
                if binding["source_type"] == "guangya":
                    return await guangya_redirect(
                        instance,
                        request,
                        str(binding["guangya_file_id"] or ""),
                    )
            dynamic_file_id = ""
            dynamic_allowed = False
            if not binding_allowed:
                dynamic_file_id = _dynamic_guangya_mappings.get(
                    instance_id, item_id, source_id
                ) or ""
                dynamic_allowed = bool(
                    dynamic_file_id
                    and _playback_grants.matches(
                        auth_scope,
                        instance_id,
                        item_id,
                        source_id,
                        file_id=dynamic_file_id,
                    )
                )
                if dynamic_allowed:
                    return await guangya_redirect(instance, request, dynamic_file_id)
            logger.info(
                "media_proxy_stream_fallback_diag instance=%s item=%s "
                "source=%s auth=%s play_session=%s device=%s capability=%s "
                "resolution=%s binding=%s dynamic=%s grant=%s query_keys=%s",
                instance_id,
                _normalized_media_identifier(item_id),
                int(bool(source_id)),
                int(bool(auth_scope)),
                int(bool(_request_query_value(request, "PlaySessionId"))),
                int(bool(_request_device_id(request))),
                int(bool(_request_query_value(request, _PLAYBACK_SESSION_QUERY_KEY))),
                getattr(request.state, "proxy_native_stream_resolution", "unknown"),
                int(bool(binding)),
                int(bool(dynamic_file_id)),
                int(bool(dynamic_allowed)),
                ",".join(sorted({str(key).lower() for key in request.query_params.keys()})),
            )

        try:
            pinned = await asyncio.to_thread(
                _pin_upstream_target,
                str(instance["upstream_url"]),
                request.url.path,
            )
        except ValueError as exc:
            request.state.proxy_failure_stage = "upstream_resolution"
            return JSONResponse({"error": str(exc)}, status_code=502)
        target = pinned.connect_url
        playback_match = _PLAYBACK_INFO_RE.match(request.url.path)
        try:
            body = await _read_proxy_request_body(request)
        except ProxyRequestBodyTooLarge:
            request.state.proxy_failure_stage = "request_body"
            return JSONResponse({"error": "请求体过大"}, status_code=413)
        playback_request_payload = (
            _playback_info_request_payload(body) if playback_match else {}
        )
        playback_rewrite_mode = (
            _native_playback_rewrite_mode(request, playback_request_payload)
            if playback_match
            else ""
        )
        direct_fallback_requested = bool(
            playback_match
            and _playback_info_direct_fallback_requested(
                request, playback_request_payload
            )
        )
        sanitized_query = (
            _playback_info_query_string(
                request,
                force_direct=not direct_fallback_requested,
            )
            if playback_match
            else _upstream_query_string(
                request,
                str(instance.get("server_type") or ""),
            )
        )
        if sanitized_query:
            target = f"{target}?{sanitized_query}"
        client = upstream_clients.get(pinned)
        upstream_headers = _upstream_request_headers(
            request,
            str(instance.get("server_type") or ""),
        )
        if playback_match:
            # PlaybackInfo 必须读取并改写 JSON。浏览器通常声明 br，但 MediaFlux
            # 的最小运行依赖不保证安装 Brotli 解码器；若把 br 原样转给 Jellyfin，
            # httpx 会在缺少可选解码器时把压缩字节当作正文，最终产生
            # application/json + 二进制 body。控制面统一请求 identity，既避免
            # 解码依赖，也确保首次请求和 Web 音轨协商重试使用同一合同。
            _replace_header(upstream_headers, "Accept-Encoding", "identity")
        upstream_headers["Host"] = pinned.host_header
        upstream_request = client.build_request(
            request.method,
            target,
            headers=upstream_headers,
            content=body,
            extensions={"sni_hostname": pinned.sni_hostname},
        )
        upstream_started = time.monotonic()
        try:
            response = await client.send(upstream_request, stream=True)
        except Exception as exc:
            request.state.proxy_failure_stage = "upstream_connect"
            logger.warning(
                f"媒体反代上游请求失败 instance={instance_id}: {type(exc).__name__}"
            )
            return JSONResponse({"error": "上游服务不可用"}, status_code=502)
        request.state.proxy_upstream_latency_ms = round(
            (time.monotonic() - upstream_started) * 1000
        )

        if playback_match and 200 <= response.status_code < 300:
            request.state.proxy_source = "playback_info"
            try:
                raw = await _read_bounded_upstream_body(
                    response,
                    _playback_info_response_limit(),
                )
                headers = _decoded_response_headers(response.headers)
            except ProxyUpstreamBodyTooLarge:
                request.state.proxy_failure_stage = "upstream_response_body"
                logger.warning(
                    "媒体反代 PlaybackInfo 响应过大 instance=%s",
                    instance_id,
                )
                return JSONResponse({"error": "上游响应过大"}, status_code=502)
            except httpx.DecodingError as exc:
                request.state.proxy_failure_stage = "upstream_playback_info"
                logger.warning(
                    "媒体反代 PlaybackInfo 解码失败 instance=%s type=%s",
                    instance_id,
                    type(exc).__name__,
                )
                return JSONResponse(
                    {"error": "上游播放信息无效"},
                    status_code=502,
                )
            finally:
                await response.aclose()
            try:
                payload = httpx.Response(
                    status_code=200,
                    content=raw,
                    headers={"content-type": headers.get("content-type", "application/json")},
                ).json()
            except ValueError as exc:
                request.state.proxy_failure_stage = "upstream_playback_info"
                logger.warning(
                    "媒体反代 PlaybackInfo JSON 无效 instance=%s type=%s",
                    instance_id,
                    type(exc).__name__,
                )
                return JSONResponse(
                    {"error": "上游播放信息无效"},
                    status_code=502,
                )
            if (
                request.method == "POST"
                and playback_rewrite_mode == "web"
                and not direct_fallback_requested
                # Query 中的选轨无法安全地用 JSON body 覆盖；无论其是否等于
                # 默认轨，都按客户端显式选择处理，避免 query/body 冲突。
                and not _request_query_has_key(request, "AudioStreamIndex")
                and not _diagnostic_dict_has_key(
                    playback_request_payload, "AudioStreamIndex"
                )
            ):
                retry_payload = _web_direct_audio_retry_payload(
                    playback_request_payload,
                    payload,
                )
                if retry_payload is not None:
                    retry_started = time.monotonic()
                    retry_request = client.build_request(
                        request.method,
                        target,
                        headers=upstream_headers,
                        content=json.dumps(
                            retry_payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8"),
                        extensions={"sni_hostname": pinned.sni_hostname},
                    )
                    retry_response: httpx.Response | None = None
                    try:
                        retry_response = await client.send(
                            retry_request,
                            stream=True,
                        )
                        if 200 <= retry_response.status_code < 300:
                            retry_raw = await _read_bounded_upstream_body(
                                retry_response,
                                _playback_info_response_limit(),
                            )
                            retry_headers = _decoded_response_headers(
                                retry_response.headers
                            )
                            retry_result = httpx.Response(
                                status_code=200,
                                content=retry_raw,
                                headers={
                                    "content-type": retry_headers.get(
                                        "content-type", "application/json"
                                    )
                                },
                            ).json()
                            retry_source_id = str(
                                _diagnostic_dict_value(
                                    retry_payload, "MediaSourceId"
                                )
                                or ""
                            )
                            if _playback_source_gained_direct_play(
                                payload, retry_result, retry_source_id
                            ):
                                raw = retry_raw
                                headers = retry_headers
                                payload = retry_result
                                playback_request_payload = retry_payload
                                request.state.proxy_action = (
                                    "playback_web_audio_direct_retry"
                                )
                    except (
                        ValueError,
                        ProxyUpstreamBodyTooLarge,
                        httpx.HTTPError,
                    ):
                        # 自动优化失败时必须无损回到首次 HLS/转码合同。
                        pass
                    finally:
                        if retry_response is not None:
                            await retry_response.aclose()
                        request.state.proxy_upstream_latency_ms += round(
                            (time.monotonic() - retry_started) * 1000
                        )
            payload_session_token = (
                str(payload.get("PlaySessionId") or "").strip()
                if isinstance(payload, dict) else ""
            )
            response_session_token = payload_session_token or _request_query_value(
                request, "PlaySessionId"
            )
            media_name = _playback_media_name(payload)
            media_source_names = _playback_media_source_names(payload)
            media_source_ids = _playback_media_source_ids(payload)
            android_device_id = _request_device_id(request)
            native_client_fingerprint = _request_client_fingerprint(request)
            native_mode = playback_rewrite_mode
            native_capability_requires_verification = native_mode in {
                "jellyfin_android",
                "findroid",
            }
            verified_native_candidate = bool(
                native_capability_requires_verification
                and not direct_fallback_requested
                and auth_scope
                and payload_session_token
                and native_client_fingerprint
            )
            # PlaybackInfo 能返回 200 不等于请求携带的 token 已通过用户鉴权。
            # 只有显式向上游验证成功，才登记 Android 后续无 token stream 请求
            # 可用的短时、会话级、精确 item/source 授权域。Findroid 直接使用
            # PlaybackInfo Path 中不可预测的 capability，不需要按地址降级授权。
            verified_native_client = False
            if verified_native_candidate:
                if not _native_android_auth_capacity.acquire(blocking=False):
                    request.state.proxy_failure_stage = "client_auth_capacity"
                    return JSONResponse(
                        {"error": "原生客户端播放鉴权繁忙，请稍后重试"},
                        status_code=503,
                    )
                try:
                    verified_native_client = await _client_is_authorized(
                        instance, request
                    )
                finally:
                    _native_android_auth_capacity.release()
            native_android_session = bool(
                verified_native_client
                and native_mode == "jellyfin_android"
                and payload_session_token
            )
            # Findroid 的 HTTP Path 由独立 DataSource 请求消费，且协议升级可能
            # 发生在 signed URL 的第二跳。验证成功后只对 Findroid 标记完整链 relay，
            # 避免改变已验证可用的 Jellyfin Android 同协议 302 行为。
            findroid_signed_media_relay = bool(
                verified_native_client
                and native_mode == "findroid"
                and payload_session_token
            )
            session_scope = (
                _upstream_playback_session_scope(
                    instance_id,
                    payload_session_token,
                    android_device_id,
                )
                if native_android_session and android_device_id
                else ""
            )
            token_scope = (
                _upstream_playback_token_scope(
                    instance_id, payload_session_token
                )
                if native_android_session and payload_session_token
                else ""
            )
            rewrite_scope = (
                auth_scope
                if not native_capability_requires_verification
                or verified_native_client
                else ""
            )
            additional_scopes = tuple(
                dict.fromkeys(
                    scope
                    for scope in (session_scope, token_scope)
                    if scope and scope != rewrite_scope
                )
            )
            body_source_id = _normalized_media_identifier(
                _diagnostic_dict_value(
                    playback_request_payload, "MediaSourceId"
                )
            )
            query_source_id = _normalized_media_identifier(
                _request_query_value(request, "MediaSourceId")
            )
            requested_source_conflict = bool(
                body_source_id
                and query_source_id
                and body_source_id != query_source_id
            )
            requested_source_id = body_source_id or query_source_id
            if requested_source_conflict:
                title_source_ids = ()
            elif requested_source_id and requested_source_id in media_source_ids:
                title_source_ids = (requested_source_id,)
            else:
                # 只接受上游 PlaybackInfo 实际确认过的 MediaSourceId。
                title_source_ids = media_source_ids
            title_scopes = tuple(dict.fromkeys(
                scope
                for scope in (auth_scope, rewrite_scope, *additional_scopes)
                if scope
            ))
            if not media_name and title_source_ids:
                for title_scope in title_scopes:
                    media_name = _playback_sessions.resolve_media_name(
                        title_scope,
                        instance_id,
                        playback_match.group(1),
                        title_source_ids,
                    )
                    if media_name:
                        break
            names_to_remember = dict(media_source_names)
            if media_name:
                for source_id in title_source_ids:
                    names_to_remember.setdefault(source_id, media_name)
            for title_scope in title_scopes:
                _playback_sessions.remember_media_names(
                    title_scope,
                    instance_id,
                    playback_match.group(1),
                    names_to_remember,
                )
            browser_direct_redirect_signatures: set[str] = set()
            if rewrite_scope and not direct_fallback_requested:
                capability_token = secrets.token_urlsafe(24)
                payload, changed = rewrite_playback_info(
                    payload,
                    instance_id,
                    playback_match.group(1),
                    route_prefix=_route_prefix(request.url.path),
                    auth_scope=rewrite_scope,
                    additional_auth_scopes=additional_scopes,
                    playback_session_token=capability_token,
                    native_client_mode=native_mode,
                    client_base_url=str(request.base_url).rstrip("/"),
                    browser_direct_redirect_signatures=(
                        browser_direct_redirect_signatures
                    ),
                )
            else:
                capability_token = ""
                changed = False
            if _request_uses_exoplayer(request):
                logger.info(
                    "media_proxy_native_playback_diag instance=%s client=%s "
                    "summary=%s",
                    instance_id,
                    _request_client_name(request) or "unknown",
                    _native_playback_diagnostic_summary(
                        playback_request_payload,
                        payload,
                        item_id=playback_match.group(1),
                        changed=changed,
                    ),
                )
            if changed:
                playback_session = _playback_sessions.finalize_capability(
                    rewrite_scope,
                    instance_id,
                    capability_token,
                    item_id=playback_match.group(1),
                    media_name=media_name,
                    upstream_session_token=response_session_token,
                    browser_relay=(
                        native_mode == "web" or _request_is_web_client(request)
                    ),
                    browser_direct_redirect_signatures=tuple(
                        sorted(browser_direct_redirect_signatures)
                    ),
                    native_cross_protocol_relay=native_android_session,
                    native_signed_media_relay=findroid_signed_media_relay,
                    native_client_fingerprint=(
                        native_client_fingerprint
                        if native_android_session or findroid_signed_media_relay
                        else ""
                    ),
                    native_verified_auth_scope=(
                        auth_scope
                        if native_android_session or findroid_signed_media_relay
                        else ""
                    ),
                )
                for native_session_scope in dict.fromkeys(
                    scope
                    for scope in (session_scope, token_scope)
                    if scope and scope != rewrite_scope
                ):
                    _playback_sessions.begin(
                        native_session_scope,
                        instance_id,
                        token=response_session_token,
                        item_id=playback_match.group(1),
                        media_name=media_name,
                        upstream_session_token=response_session_token,
                        browser_relay=False,
                        native_cross_protocol_relay=True,
                        native_client_fingerprint=native_client_fingerprint,
                        native_verified_auth_scope=auth_scope,
                    )
                _apply_playback_session(request, playback_session)
                request.state.proxy_action = "playback_rewrite"
                return JSONResponse(
                    payload,
                    status_code=response.status_code,
                    headers={
                        "Cache-Control": "private, no-store, no-cache, max-age=0",
                        "Pragma": "no-cache",
                        "Referrer-Policy": "no-referrer",
                    },
                )
            if isinstance(payload, dict):
                if response_session_token:
                    playback_session = _playback_sessions.resolve(
                        auth_scope,
                        instance_id,
                        token=response_session_token,
                        item_id=playback_match.group(1),
                        media_name=media_name,
                        create=True,
                    )
                else:
                    playback_session = _playback_sessions.begin(
                        auth_scope,
                        instance_id,
                        item_id=playback_match.group(1),
                        media_name=media_name,
                    )
                _apply_playback_session(request, playback_session)
            request.state.proxy_action = "playback_passthrough"
            return Response(content=raw, status_code=response.status_code, headers=headers)

        if response.status_code in _UPSTREAM_REDIRECT_STATUS_CODES:
            redirect_headers = _response_headers(response.headers)
            location = str(response.headers.get("location") or "").strip()
            await response.aclose()
            try:
                _replace_header(
                    redirect_headers,
                    "Location",
                    _same_upstream_redirect_location(
                        pinned,
                        location,
                        upstream_base_url=str(instance["upstream_url"]),
                    ),
                )
            except ValueError:
                request.state.proxy_failure_stage = "upstream_redirect"
                return JSONResponse(
                    {"error": "上游服务返回了不安全的重定向"},
                    status_code=502,
                )
            redirect_headers.pop("content-length", None)
            redirect_headers.pop("Content-Length", None)
            return Response(
                status_code=response.status_code,
                headers=redirect_headers,
            )

        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers=_response_headers(response.headers),
            background=BackgroundTask(_close_upstream, response),
        )

    return app


@dataclass
class ProxyRuntime:
    instance_id: int
    bind: tuple[str, int]
    server: uvicorn.Server
    task: asyncio.Task
    sock: socket.socket
    signed_urls: SignedUrlCache


class MediaProxyManager:
    def __init__(self):
        self._runtimes: dict[int, ProxyRuntime] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False

    async def start(self) -> None:
        async with self._lock:
            self._stopping = False
            self._loop = asyncio.get_running_loop()
        await self.reconcile()

    async def stop(self) -> None:
        async with self._lock:
            self._stopping = True
            self._loop = None
            runtimes = list(self._runtimes.values())
            tasks = [
                asyncio.create_task(
                    self._stop_runtime(runtime),
                    name=f"media-proxy-stop-{runtime.instance_id}",
                )
                for runtime in runtimes
            ]
            try:
                if tasks:
                    await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            finally:
                self._runtimes.clear()

    def request_reconcile(self) -> bool:
        """从同步配置接口线程安全地请求热重载；返回是否已成功排队。"""
        loop = self._loop
        if self._stopping or loop is None or loop.is_closed() or not loop.is_running():
            return False
        future = asyncio.run_coroutine_threadsafe(self.reconcile(), loop)
        future.add_done_callback(self._report_reconcile_failure)
        return True

    @staticmethod
    def _report_reconcile_failure(future) -> None:
        if future.cancelled():
            return
        try:
            future.result()
        except Exception as exc:
            logger.warning("媒体反代热重载失败 type=%s", type(exc).__name__)

    async def reconcile(self) -> dict[str, Any]:
        async with self._lock:
            if self._stopping:
                return {"started": [], "stopped": [], "failed": {}}
            instance_rows = await asyncio.to_thread(
                database.list_media_proxy_instances
            )
            raw_rows = {int(row["id"]): row for row in instance_rows}
            rows: dict[int, Any] = {}
            stopped: list[int] = []
            started: list[int] = []
            failed: dict[int, str] = {}

            for instance_id, row in raw_rows.items():
                if not int(row["enabled"] or 0):
                    rows[instance_id] = row
                    continue
                try:
                    rows[instance_id] = resolve_proxy_instance(row)
                except ValueError as exc:
                    message = str(exc)
                    failed[instance_id] = message
                    await asyncio.to_thread(
                        database.update_media_proxy_instance,
                        instance_id,
                        {"status": "error", "last_error": message},
                    )
                    runtime = self._runtimes.pop(instance_id, None)
                    if runtime:
                        await self._stop_runtime(runtime)
                        stopped.append(instance_id)

            for instance_id, runtime in list(self._runtimes.items()):
                row = rows.get(instance_id)
                if not row or not int(row["enabled"] or 0):
                    await self._stop_runtime(runtime)
                    self._runtimes.pop(instance_id, None)
                    stopped.append(instance_id)
                    if row:
                        await asyncio.to_thread(
                            database.update_media_proxy_instance,
                            instance_id,
                            {"status": "stopped", "last_error": ""},
                        )
                    continue
                if runtime.task.done():
                    try:
                        runtime_error = runtime.task.exception()
                    except asyncio.CancelledError:
                        runtime_error = None
                    logger.warning(
                        "媒体反代运行任务意外退出 id=%s type=%s",
                        instance_id,
                        type(runtime_error).__name__ if runtime_error else "CancelledError",
                    )
                    await self._stop_runtime(runtime)
                    self._runtimes.pop(instance_id, None)
                    stopped.append(instance_id)
                    await asyncio.to_thread(
                        database.update_media_proxy_instance,
                        instance_id,
                        {"status": "error", "last_error": "媒体反代运行任务意外退出"},
                    )
                    continue
                desired = (validate_listen_host(row["listen_host"]), int(row["listen_port"]))
                if desired == runtime.bind:
                    await asyncio.to_thread(
                        database.update_media_proxy_instance,
                        instance_id,
                        {"status": "running", "last_error": ""},
                    )
                    continue
                try:
                    replacement = await self._start_runtime(row)
                except Exception as exc:
                    message = str(exc)
                    failed[instance_id] = message
                    await asyncio.to_thread(
                        database.update_media_proxy_instance,
                        instance_id,
                        {"status": "error", "last_error": message},
                    )
                    continue
                await self._stop_runtime(runtime)
                self._runtimes[instance_id] = replacement
                started.append(instance_id)

            for instance_id, row in rows.items():
                if instance_id in self._runtimes or not int(row["enabled"] or 0):
                    continue
                try:
                    runtime = await self._start_runtime(row)
                except Exception as exc:
                    message = str(exc)
                    failed[instance_id] = message
                    await asyncio.to_thread(
                        database.update_media_proxy_instance,
                        instance_id,
                        {"status": "error", "last_error": message},
                    )
                    continue
                self._runtimes[instance_id] = runtime
                started.append(instance_id)

            return {"started": started, "stopped": stopped, "failed": failed}

    async def _start_runtime(self, row) -> ProxyRuntime:
        row = resolve_proxy_instance(row)
        instance_id = int(row["id"])
        host = validate_listen_host(str(row["listen_host"]))
        port = int(row["listen_port"])
        if port < 1024 or port > 65535:
            raise ValueError("监听端口必须在 1024 到 65535 之间")
        validate_upstream_url(str(row["upstream_url"]))
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            sock.listen(2048)
            sock.setblocking(False)
        except OSError as exc:
            sock.close()
            raise RuntimeError(f"监听端口 {host}:{port} 被占用") from exc
        signed_urls = SignedUrlCache()
        config = uvicorn.Config(
            create_proxy_app(instance_id, signed_urls),
            host=host,
            port=port,
            log_level="warning",
            log_config=None,
            access_log=False,
            lifespan="on",
            # 不能继承 FORWARDED_ALLOW_IPS 后让 Uvicorn 先改写 ASGI client；
            # 本反代只把真实 TCP peer 作为可信客户端来源。
            proxy_headers=False,
            ws_max_size=_proxy_websocket_message_limit(),
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve(sockets=[sock]), name=f"media-proxy-{instance_id}")
        await asyncio.sleep(0)
        if task.done():
            sock.close()
            _release_signed_url_cache(instance_id, signed_urls)
            error = task.exception()
            raise RuntimeError(f"媒体反代启动失败: {error or 'unknown error'}")
        await asyncio.to_thread(
            database.update_media_proxy_instance,
            instance_id,
            {"status": "running", "last_error": ""},
        )
        logger.info(f"媒体反代实例启动 id={instance_id} listen={host}:{port}")
        return ProxyRuntime(instance_id, (host, port), server, task, sock, signed_urls)

    @staticmethod
    async def _stop_runtime(runtime: ProxyRuntime) -> None:
        _release_signed_url_cache(runtime.instance_id, runtime.signed_urls)
        runtime.server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(runtime.task), timeout=5)
        except asyncio.TimeoutError:
            runtime.task.cancel()
            await asyncio.gather(runtime.task, return_exceptions=True)
        except asyncio.CancelledError:
            runtime.task.cancel()
            await asyncio.gather(runtime.task, return_exceptions=True)
            raise
        except Exception as exc:
            logger.warning(
                "媒体反代运行任务停止时已失败 id=%s type=%s",
                runtime.instance_id,
                type(exc).__name__,
            )
        finally:
            try:
                runtime.sock.close()
            except OSError:
                pass
        logger.info(f"媒体反代实例停止 id={runtime.instance_id}")

    def status(self) -> dict[int, dict[str, Any]]:
        return {
            instance_id: {
                "running": not runtime.task.done(),
                "listen_host": runtime.bind[0],
                "listen_port": runtime.bind[1],
            }
            for instance_id, runtime in self._runtimes.items()
        }


_manager: MediaProxyManager | None = None


def get_media_proxy_manager() -> MediaProxyManager:
    global _manager
    if _manager is None:
        _manager = MediaProxyManager()
    return _manager
