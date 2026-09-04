"""RSS 订阅引擎（mikan 蜜柑计划适配）。

能力：
- MikanParser：feedparser 解析 Mikan RSS，提取标题、下载地址、发布时间与 guid
- RSSEngine：订阅管理、刷新去重，并把 qB/光鸭任务接入统一下载状态机

下载目标由配置 RSS_DOWNLOAD_METHOD 决定：qb / guangya。
"""
from __future__ import annotations

import ipaddress
import json
import re
import secrets
import socket
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from app import database as db
from app.config import get, get_many
from app.indexers.providers.base import magnet_infohash
from app.logger import get_logger
from app.modules.media_identity import build_media_key, parse_episode_label

logger = get_logger(__name__)

RSS_REFRESH_BUSY_ERROR = "订阅正在刷新，请稍后重试"
RSS_REFRESH_CONFLICT_ERROR = "订阅配置已变化，请重新确认"
_RSS_REFRESH_GATE_LOCK = threading.Lock()
_RSS_REFRESHING_SUBSCRIPTIONS: set[int] = set()
_RSS_CONNECT_TIMEOUT_SECONDS = 10
_RSS_READ_TIMEOUT_SECONDS = 30
_RSS_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_RSS_MAX_SOURCE_URLS = 20
_RSS_MAX_SOURCE_URL_LENGTH = 2048
_RSS_MAX_SOURCE_TOTAL_LENGTH = 16 * 1024
_RSS_MAX_REDIRECTS = 3
_RSS_REFRESH_DEADLINE_SECONDS = 90.0
_RSS_DOWNLOAD_BATCH_SIZE = 20
_RSS_AUTO_DOWNLOAD_MAX_ENTRIES = 100
_RSS_AUTO_DOWNLOAD_DEADLINE_SECONDS = 120.0


def validate_rss_source_url(value: str, *, resolve: bool = False) -> str:
    """校验 RSS 上游边界；网络请求前必须启用公网 DNS 校验。"""
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > _RSS_MAX_SOURCE_URL_LENGTH:
        raise ValueError("RSS URL 长度无效")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or (parsed.port or 443) != 443
    ):
        raise ValueError("RSS URL 仅支持不含凭据和片段的 HTTPS 地址")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise ValueError("RSS URL 不允许访问本机或内网地址")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("RSS URL 不允许访问本机或内网地址")
    if resolve:
        _resolve_public_rss_addresses(host)
    return normalized


def _resolve_public_rss_addresses(
    host: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """解析并返回稳定排序的公网地址；调用方必须把连接固定到返回值。"""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    try:
        addresses = {
            literal
        } if literal is not None else {
            ipaddress.ip_address(record[4][0].split("%", 1)[0])
            for record in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        }
    except (OSError, socket.gaierror, IndexError, TypeError, ValueError) as exc:
        raise ValueError("RSS URL 域名解析失败") from exc
    if not addresses or any(address is None or not address.is_global for address in addresses):
        raise ValueError("RSS URL 不允许访问本机或内网地址")
    return sorted(
        (address for address in addresses if address is not None),
        key=lambda address: (address.version, str(address)),
    )


def _pinned_rss_request_target(url: str) -> tuple[str, httpx.URL, dict[str, str], dict]:
    """返回逻辑 URL 与固定公网 IP 的实际请求参数，消除 DNS 重绑定窗口。"""
    logical_url = validate_rss_source_url(url)
    parsed = httpx.URL(logical_url)
    host = str(parsed.host or "").rstrip(".").lower()
    address = _resolve_public_rss_addresses(host)[0]
    request_url = parsed.copy_with(host=str(address))
    return logical_url, request_url, {"Host": host}, {"sni_hostname": host}


def _fetch_rss_payload(
    url: str, *, user_agent: str, timeout_seconds: float | None = None,
) -> tuple[bytes, dict[str, str]]:
    """通过固定地址 transport 拉取 RSS，并遵守调用方的剩余总预算。"""
    current_url = validate_rss_source_url(url)
    budget = max(
        0.1,
        float(timeout_seconds)
        if timeout_seconds is not None else float(_RSS_READ_TIMEOUT_SECONDS),
    )
    deadline = time.monotonic() + budget
    with httpx.Client(
        timeout=httpx.Timeout(
            _RSS_READ_TIMEOUT_SECONDS,
            connect=_RSS_CONNECT_TIMEOUT_SECONDS,
        ),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": user_agent},
        limits=httpx.Limits(max_keepalive_connections=0),
    ) as client:
        for redirect_count in range(_RSS_MAX_REDIRECTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RSS 刷新超过本轮时间预算")
            logical_url, request_url, request_headers, extensions = (
                _pinned_rss_request_target(current_url)
            )
            attempt_timeout = httpx.Timeout(
                min(float(_RSS_READ_TIMEOUT_SECONDS), remaining),
                connect=min(float(_RSS_CONNECT_TIMEOUT_SECONDS), remaining),
            )
            with client.stream(
                "GET",
                request_url,
                headers=request_headers,
                extensions=extensions,
                timeout=attempt_timeout,
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = str(response.headers.get("Location") or "").strip()
                    if not location or redirect_count >= _RSS_MAX_REDIRECTS:
                        raise ValueError("RSS 重定向无效或超过上限")
                    current_url = validate_rss_source_url(
                        urljoin(logical_url, location)
                    )
                    continue
                response.raise_for_status()
                declared_size = response.headers.get("Content-Length")
                if declared_size:
                    try:
                        declared_bytes = int(declared_size)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("RSS 响应长度无效") from exc
                    if declared_bytes < 0 or declared_bytes > _RSS_MAX_RESPONSE_BYTES:
                        raise ValueError("RSS 响应体过大")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("RSS 刷新超过本轮时间预算")
                    if not chunk:
                        continue
                    payload.extend(chunk)
                    if len(payload) > _RSS_MAX_RESPONSE_BYTES:
                        raise ValueError("RSS 响应体过大")
                headers = {str(key): str(value) for key, value in response.headers.items()}
                headers.setdefault("content-location", logical_url)
                return bytes(payload), headers
    raise ValueError("RSS 重定向超过上限")


def validate_rss_source_urls(value: str) -> str:
    """规范化订阅 URL 列表并限制单订阅的网络工作量。"""
    raw = str(value or "").strip()
    if not raw or len(raw) > _RSS_MAX_SOURCE_TOTAL_LENGTH:
        raise ValueError("RSS URL 列表为空或过长")
    urls = [line.strip() for line in raw.splitlines() if line.strip()]
    if not urls or len(urls) > _RSS_MAX_SOURCE_URLS:
        raise ValueError(f"每个订阅最多配置 {_RSS_MAX_SOURCE_URLS} 个 RSS URL")
    return "\n".join(validate_rss_source_url(url) for url in urls)


def rss_subscription_refresh_revision(subscription) -> str:
    """计算刷新相关配置的稳定版本，不暴露订阅 URL 或过滤词。"""
    import hashlib

    def value(key: str, default=""):
        try:
            return subscription[key]
        except (KeyError, IndexError, TypeError):
            return default

    payload = {
        "id": int(value("id", "")),
        "updated_at": str(value("updated_at", "") or ""),
        "enabled": int(value("enabled", 0) or 0),
        "urls": str(value("urls", "") or ""),
        "parser": str(value("parser", "") or ""),
        "exclude_keywords": str(value("exclude_keywords", "") or ""),
        "action": str(value("action", "") or ""),
        "media_tmdb_id": str(value("media_tmdb_id", "") or ""),
        "media_default_season": int(value("media_default_season", 1)),
        "skip_existing_episodes": int(value("skip_existing_episodes", 0) or 0),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _try_acquire_rss_refresh(sub_id: int) -> bool:
    with _RSS_REFRESH_GATE_LOCK:
        if sub_id in _RSS_REFRESHING_SUBSCRIPTIONS:
            return False
        _RSS_REFRESHING_SUBSCRIPTIONS.add(sub_id)
        return True


def _release_rss_refresh(sub_id: int) -> None:
    with _RSS_REFRESH_GATE_LOCK:
        _RSS_REFRESHING_SUBSCRIPTIONS.discard(sub_id)


def capture_rss_qb_runtime_config() -> tuple[dict, str]:
    """冻结一次 RSS → qB 提交所需的运行时配置。"""
    keys = (
        "QB_URL", "QB_USERNAME", "QB_PASSWORD", "QB_API_KEY",
        "RSS_QB_CATEGORY", "RSS_QB_SAVE_PATH", "RSS_DOWNLOAD_METHOD",
    )
    values = get_many(keys, {"RSS_DOWNLOAD_METHOD": "qb"})
    runtime = {
        "url": str(values["QB_URL"] or "").strip(),
        "username": str(values["QB_USERNAME"] or ""),
        "password": str(values["QB_PASSWORD"] or ""),
        "api_key": str(values["QB_API_KEY"] or ""),
        "category": str(values["RSS_QB_CATEGORY"] or ""),
        "default_save_path": str(values["RSS_QB_SAVE_PATH"] or ""),
        "default_method": str(values["RSS_DOWNLOAD_METHOD"] or "qb").strip().lower(),
        "timeout": 10,
    }
    if not runtime["url"]:
        return runtime, "未配置 qBittorrent 地址"
    return runtime, ""


def _safe_download_source_marker(url: str) -> str:
    """只记录协议类型，避免把磁力哈希、私有 tracker 或 passkey 写入日志。"""
    normalized = str(url or "").strip().lower()
    if normalized.startswith("magnet:"):
        return "[magnet]"
    if normalized.startswith("https://"):
        return "[https]"
    if normalized.startswith("http://"):
        return "[http]"
    return "[torrent]" if normalized else ""


@dataclass
class RSSEntry:
    """单条 RSS 条目。"""
    title: str
    link: str = ""
    guid: str = ""
    pub_date: str = ""
    torrent_url: str = ""          # 种子直链（磁力或 .torrent）
    episode: str = ""              # 解析出的集数信息（如 S01E01）
    release_group: str = ""        # 发布组
    resolution: str = ""           # 分辨率
    series_title: str = ""         # 解析出的作品名，仅用于识别混合 RSS


class MikanParser:
    """蜜柑计划 RSS 解析器。"""

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )

    def __init__(self) -> None:
        self.last_error_code = ""
        self._refresh_timeout_seconds: float | None = None

    def parse(
        self, url: str, *, timeout_seconds: float | None = None,
    ) -> list[RSSEntry]:
        """拉取并解析一个 mikan RSS 源，返回条目列表。"""
        self.last_error_code = ""
        try:
            payload, response_headers = _fetch_rss_payload(
                url,
                user_agent=self.USER_AGENT,
                timeout_seconds=(
                    timeout_seconds
                    if timeout_seconds is not None else self._refresh_timeout_seconds
                ),
            )
            feed = feedparser.parse(bytes(payload), response_headers=response_headers)
        except Exception as exc:
            self.last_error_code = "fetch_failed"
            logger.error("RSS 拉取失败 type=%s", type(exc).__name__)
            return []

        if feed.bozo and not feed.entries:
            self.last_error_code = "parse_failed"
            logger.warning(
                "RSS 解析异常 type=%s",
                type(feed.bozo_exception).__name__,
            )
            return []

        entries: list[RSSEntry] = []
        for it in feed.entries:
            torrent = self._extract_torrent(it)
            guid = it.get("id") or it.get("guid") or it.get("link") or it.get("title", "")
            pub = self._format_date(it.get("published") or it.get("updated") or "")
            title = it.get("title", "").strip()
            if not title:
                continue
            info = self._parse_title(title)
            entries.append(RSSEntry(
                title=title,
                link=it.get("link", ""),
                guid=guid,
                pub_date=pub,
                torrent_url=torrent,
                episode=info.get("episode", ""),
                release_group=info.get("release_group", ""),
                resolution=info.get("resolution", ""),
                series_title=info.get("series_title", ""),
            ))
        logger.info("RSS 解析完成: %s 条", len(entries))
        return entries

    @staticmethod
    def _extract_torrent(entry) -> str:
        """从 feed 条目提取种子链接：优先 enclosure，回退 link。"""
        for enc in getattr(entry, "enclosures", []) or []:
            href = enc.get("href", "")
            if href:
                return href
        link = entry.get("link", "")
        return link

    @staticmethod
    def _format_date(raw: str) -> str:
        if not raw:
            return ""
        try:
            dt = datetime.strptime(raw[:25], "%a, %d %b %Y %H:%M:%S")
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return raw[:19]

    @staticmethod
    def _parse_title(title: str) -> dict:
        """用 guessit 解析标题中的集数/分辨率/发布组。失败不影响主流程。"""
        try:
            from guessit import guessit
            info = guessit(title)
            episode = ""
            episode_value = info.get("episode")
            if isinstance(episode_value, (list, tuple)):
                episode_value = episode_value[-1] if episode_value else None
            season_value = info.get("season", 1)
            if isinstance(season_value, (list, tuple)):
                season_value = season_value[-1] if season_value else 1
            if episode_value is not None:
                episode = f"S{int(season_value or 1):02d}E{int(episode_value):02d}"
            else:
                absolute_episode = info.get("absolute_episode")
                if isinstance(absolute_episode, (list, tuple)):
                    absolute_episode = absolute_episode[-1] if absolute_episode else None
                if absolute_episode is not None:
                    episode = f"E{int(absolute_episode):02d}"
            series_title = info.get("title", "")
            if isinstance(series_title, (list, tuple)):
                series_title = series_title[-1] if series_title else ""
            return {
                "episode": episode,
                "release_group": str(info.get("release_group", "") or ""),
                "resolution": str(info.get("screen_size", "") or ""),
                "series_title": str(series_title or "").strip(),
            }
        except Exception:
            return {}


class RSSEngine:
    """RSS 订阅引擎：刷新、去重、下载联动。"""

    def __init__(self, *, tmdb_client=None):
        self.parser = MikanParser()
        self._tmdb_client = tmdb_client
        self._owns_tmdb_client = tmdb_client is None

    def close(self) -> None:
        """释放本实例按需创建的 TMDB 连接池；注入客户端由调用方管理。"""
        if not self._owns_tmdb_client:
            return
        client = self._tmdb_client
        self._tmdb_client = None
        if client is None:
            return
        from app.clients.tmdb import close_tmdb_client

        close_tmdb_client(client)

    @staticmethod
    def _series_identity_key(value: str) -> str:
        """生成仅用于混合 RSS 防误绑的保守作品键。"""
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", normalized)

    @staticmethod
    def _tmdb_title_values(detail: dict) -> list[str]:
        """提取 TV detail 中可证明同一作品身份的正式名、原始名与别名。"""
        values: list[object] = [
            detail.get("name"),
            detail.get("original_name"),
        ]

        def append_collection(collection: object) -> None:
            if isinstance(collection, dict):
                collection = (
                    collection.get("titles")
                    or collection.get("results")
                    or collection.get("translations")
                    or []
                )
            for item in collection if isinstance(collection, list) else []:
                if isinstance(item, dict):
                    data = item.get("data") if isinstance(item.get("data"), dict) else {}
                    values.extend((
                        item.get("title"), item.get("name"),
                        data.get("title"), data.get("name"), data.get("english_name"),
                    ))
                else:
                    values.append(item)

        append_collection(detail.get("aliases") or [])
        append_collection(detail.get("alternative_titles") or [])
        append_collection(detail.get("translations") or [])
        return list(dict.fromkeys(
            str(value).strip() for value in values if str(value or "").strip()
        ))

    def _bound_tv_title_keys(self, tmdb_id: str) -> tuple[set[str], bool]:
        """读取绑定 TV 的可信标题集合；网络/配置异常时失败关闭。"""
        try:
            if self._tmdb_client is None:
                from app.clients.tmdb import TMDBClient

                self._tmdb_client = TMDBClient()
            detail = self._tmdb_client.detail_with_alternative_titles(tmdb_id, "tv")
        except Exception as exc:
            logger.warning(
                "RSS TMDB 标题核验失败 tmdb=%s error=%s",
                tmdb_id,
                type(exc).__name__,
            )
            return set(), False
        if not isinstance(detail, dict):
            return set(), False
        try:
            detail_id = str(int(detail.get("id")))
            expected_id = str(int(tmdb_id))
        except (TypeError, ValueError):
            return set(), False
        if detail_id != expected_id:
            return set(), False
        keys = {
            self._series_identity_key(value)
            for value in self._tmdb_title_values(detail)
        }
        keys.discard("")
        return keys, bool(keys)

    @staticmethod
    def _existing_library_episodes(tmdb_id: str) -> tuple[set[tuple[int, int]], dict[str, int]]:
        """读取精确 TMDB 身份的正向库存；不可用时不猜测、不阻断刷新。"""
        from app.services import inspect_series_episode_inventory_by_tmdb

        sources = inspect_series_episode_inventory_by_tmdb(
            tmdb_id, max_episodes=2000, include_specials=True
        )
        positions: set[tuple[int, int]] = set()
        ready = unavailable = truncated = 0
        for source in sources:
            status = str(source.get("status") or "")
            if status == "ready":
                ready += 1
                positions.update(
                    (int(item[0]), int(item[1]))
                    for item in source.get("episodes") or []
                    if isinstance(item, (list, tuple)) and len(item) == 2
                )
                if source.get("truncated"):
                    truncated += 1
            elif status == "unavailable":
                unavailable += 1
        return positions, {
            "sources": len(sources), "ready": ready,
            "unavailable": unavailable, "truncated": truncated,
        }

    # ===== 刷新 =====
    def refresh(self, sub_id: int, *, expected_revision: str = "") -> dict:
        """刷新单个订阅：同一进程内按订阅单飞，去重入库并返回统计。"""
        if not _try_acquire_rss_refresh(sub_id):
            return {"error": RSS_REFRESH_BUSY_ERROR, "busy": True}

        try:
            # 获取单飞门后重新读取配置，以门内快照作为本次刷新边界。
            # 这既避免确认版本在排队期间失效后仍继续执行，也保证解析
            # 全程使用同一份订阅配置。
            sub = db.get_rss_subscription(sub_id)
            if not sub:
                return {"error": "订阅项不存在"}
            urls = [u.strip() for u in (sub["urls"] or "").splitlines() if u.strip()]
            if not urls:
                return {"error": "订阅项无 URL"}
            if expected_revision and not secrets.compare_digest(
                rss_subscription_refresh_revision(sub), str(expected_revision)
            ):
                return {"error": RSS_REFRESH_CONFLICT_ERROR, "conflict": True}

            exclude = self._split_keywords(sub["exclude_keywords"])
            media_tmdb_id = str(sub["media_tmdb_id"] or "").strip()
            default_season = int(
                sub["media_default_season"]
                if sub["media_default_season"] is not None
                else 1
            )
            skip_existing_requested = bool(sub["skip_existing_episodes"] and media_tmdb_id)
            library_positions: set[tuple[int, int]] = set()
            library_check = {"sources": 0, "ready": 0, "unavailable": 0, "truncated": 0}
            failed_sources = 0
            parsed_entries: list[RSSEntry] = []
            refresh_deadline = time.monotonic() + _RSS_REFRESH_DEADLINE_SECONDS
            for index, url in enumerate(urls):
                remaining = refresh_deadline - time.monotonic()
                if remaining <= 0:
                    failed_sources += len(urls) - index
                    break
                if isinstance(self.parser, MikanParser):
                    self.parser._refresh_timeout_seconds = remaining
                try:
                    entries = self.parser.parse(url)
                finally:
                    if isinstance(self.parser, MikanParser):
                        self.parser._refresh_timeout_seconds = None
                parser_error = getattr(self.parser, "last_error_code", "")
                if isinstance(parser_error, str) and parser_error:
                    failed_sources += 1
                    continue
                parsed_entries.extend(entries)
            if failed_sources == len(urls):
                logger.warning("RSS 刷新失败 sub#%s: 全部 %s 个源不可用", sub_id, failed_sources)
                return {
                    "error": "RSS 源暂时不可用，请稍后重试",
                    "error_code": "all_sources_failed",
                    "failed_sources": failed_sources,
                }

            structured_entries = [
                entry for entry in parsed_entries
                if parse_episode_label(entry.episode, default_season=default_season) is not None
            ]
            structured_series_keys = [
                self._series_identity_key(entry.series_title) for entry in structured_entries
            ]
            series_keys = {key for key in structured_series_keys if key}
            unresolved_series_count = sum(not key for key in structured_series_keys)
            structure_binding_allowed = bool(
                media_tmdb_id
                and structured_entries
                and len(series_keys) == 1
                and unresolved_series_count == 0
            )
            binding_allowed = structure_binding_allowed
            binding_bypass_reason = ""
            if structure_binding_allowed:
                bound_title_keys, titles_verified = self._bound_tv_title_keys(media_tmdb_id)
                if not titles_verified:
                    binding_allowed = False
                    binding_bypass_reason = "tmdb_title_unverified"
                elif next(iter(series_keys)) not in bound_title_keys:
                    binding_allowed = False
                    binding_bypass_reason = "tmdb_title_mismatch"
            media_binding_bypassed = bool(
                media_tmdb_id and parsed_entries and not binding_allowed
            )
            if media_binding_bypassed and not binding_bypass_reason:
                if len(series_keys) > 1:
                    binding_bypass_reason = "mixed_feed"
                elif unresolved_series_count:
                    binding_bypass_reason = "series_unresolved"
                else:
                    binding_bypass_reason = "episode_unresolved"
            mixed_feed_bypassed = binding_bypass_reason == "mixed_feed"
            skip_existing = bool(skip_existing_requested and binding_allowed)
            if skip_existing:
                library_positions, library_check = self._existing_library_episodes(media_tmdb_id)
            elif media_binding_bypassed:
                logger.warning(
                    "RSS 无法安全确认单一剧目，已跳过 TMDB 绑定 "
                    "sub#%s reason=%s detected_series=%s unresolved_series=%s",
                    sub_id, binding_bypass_reason, len(series_keys), unresolved_series_count,
                )

            total, new, skipped = 0, 0, 0
            library_skipped = semantic_duplicates = identity_unresolved = 0
            for entry in parsed_entries:
                total += 1
                if self._excluded(entry.title, exclude):
                    skipped += 1
                    continue
                position = parse_episode_label(
                    entry.episode, default_season=default_season
                ) if binding_allowed else None
                media_key = ""
                automatic_skip_reason = ""
                if position is not None:
                    media_key = build_media_key(
                        media_tmdb_id, "tv", position[0], position[1]
                    )
                    if skip_existing and position in library_positions:
                        automatic_skip_reason = "媒体库已存在该剧集"
                elif media_tmdb_id and binding_allowed:
                    identity_unresolved += 1

                inserted = db.add_rss_entry_with_media(
                    sub_id=sub_id,
                    title=entry.title,
                    guid=entry.guid,
                    pub_date=entry.pub_date,
                    payload=json.dumps({
                        "link": entry.link,
                        "torrent_url": entry.torrent_url,
                        "episode": entry.episode,
                        "release_group": entry.release_group,
                        "resolution": entry.resolution,
                        "series_title": entry.series_title,
                    }, ensure_ascii=False),
                    media_key=media_key,
                    tmdb_id=media_tmdb_id if position is not None else "",
                    season=position[0] if position is not None else None,
                    episode=position[1] if position is not None else None,
                    skip_reason=automatic_skip_reason,
                )
                if inserted.get("id") is not None:
                    new += 1
                    reason = str(inserted.get("skip_reason") or "")
                    if reason:
                        skipped += 1
                        if reason == "媒体库已存在该剧集":
                            library_skipped += 1
                        else:
                            semantic_duplicates += 1
            logger.info(
                "RSS 刷新 sub#%s: 拉取 %s 新增 %s 排除 %s 库内 %s 语义重复 %s 失败源 %s",
                sub_id, total, new, skipped, library_skipped, semantic_duplicates, failed_sources,
            )
            db.update_rss_subscription(sub_id, {"last_refreshed_at": db.now()})
            result = {"total": total, "new": new, "skipped": skipped}
            if media_tmdb_id:
                result.update({
                    "library_skipped": library_skipped,
                    "semantic_duplicates": semantic_duplicates,
                    "identity_unresolved": identity_unresolved,
                })
                if media_binding_bypassed:
                    result.update({
                        "media_binding_bypassed": True,
                        "binding_bypass_reason": binding_bypass_reason,
                        "mixed_feed_bypassed": mixed_feed_bypassed,
                        "detected_series_count": len(series_keys),
                        "unresolved_series_count": unresolved_series_count,
                    })
            if skip_existing:
                result["library_check"] = library_check
            if failed_sources:
                result.update({"partial": True, "failed_sources": failed_sources})
            return result
        finally:
            try:
                self.close()
            finally:
                _release_rss_refresh(sub_id)

    # ===== 下载联动 =====
    @staticmethod
    def _torrent_infohash(value: str) -> str:
        direct = magnet_infohash(value)
        if direct:
            return direct.lower()
        from app.modules.download_dispatcher import http_torrent_infohash_hint

        return http_torrent_infohash_hint(value)

    @staticmethod
    def _entry_method(entry) -> str:
        return (entry["download_method"] or get("RSS_DOWNLOAD_METHOD", "qb")).strip().lower()

    @staticmethod
    def _entry_value(entry, key: str, default=""):
        """读取 dict/sqlite.Row 的可选字段；Agent 冻结快照只包含 qB 所需列。"""
        try:
            value = entry[key]
        except (IndexError, KeyError, TypeError):
            return default
        return default if value is None else value

    @staticmethod
    def _entry_torrent_url(entry) -> str:
        try:
            payload = json.loads(entry["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        return str(payload.get("torrent_url") or payload.get("link") or "").strip()

    @classmethod
    def _download_input(cls, entry, torrent_url: str):
        """把 RSS 条目转换为统一下载输入，并保留可验证的 BT 身份提示。"""
        from app.modules.download_dispatcher import (
            DownloadInput,
            normalize_download_url,
        )

        item = normalize_download_url(torrent_url)
        title = str(entry["title"] or item.title or "RSS 下载任务").strip()
        identity_hint = ""
        # 某些 RSS 只提供 HTTP .torrent 地址，但路径本身带标准 BTIH。
        # 保留原 URL 让后端取得 tracker/passkey，同时将 BTIH 用于统一幂等
        # 与 qB API 后续任务匹配；magnet/torrent 本身已有经过解析的身份。
        if item.kind == "http":
            infohash = cls._torrent_infohash(torrent_url)
            if infohash:
                identity_hint = f"btih:{infohash}"
        return DownloadInput(
            kind=item.kind,
            title=title,
            source_value=item.source_value,
            torrent_data=item.torrent_data,
            identity_hint=identity_hint,
        )

    @staticmethod
    def _accepted_duplicate(summary: dict, request, method: str) -> bool:
        if not summary.get("duplicate"):
            return False
        root_status = str(summary.get("existing_status") or "").strip().lower()
        if root_status not in {
            "pending", "submitting", "submitted", "downloading", "completed", "resubmitted",
        }:
            return False
        if request is None:
            return root_status == "pending"
        backend_field = "qb_status" if method == "qb" else "gy_status"
        try:
            backend_status = str(request[backend_field] or "").strip().lower()
        except (IndexError, KeyError, TypeError):
            backend_status = ""
        return backend_status in {"", "submitting", "submitted", "downloading", "completed"}

    @staticmethod
    def _backend_failure(method: str, submission: dict) -> tuple[str, bool, bool]:
        """从统一分发内部结果提取 RSS 所需的稳定失败分类。"""
        summary = submission.get("summary") or {}
        dispatch = submission.get("dispatch") or {}
        backend = (dispatch.get("results") or {}).get(method) or {}
        review_required = bool(
            summary.get("status") == "manual_review"
            or dispatch.get("review_required")
            or dispatch.get("outcome_unknown")
        )
        if method == "qb":
            code = str(backend.get("failure_code") or "").strip().lower()
            if review_required:
                return "qb_outcome_unknown", False, True
            return code or "qb_rejected", bool(backend.get("retryable")), False
        if review_required:
            return "guangya_outcome_unknown", False, True
        return "guangya_submit_failed", False, False

    def _download_entry(
        self,
        entry,
        *,
        entry_already_claimed: bool = False,
        qb_runtime_config: dict | None = None,
    ) -> dict:
        """将单条 RSS 下载统一接入 download_request / DownloadTracker。"""
        if not entry:
            return {"error": "条目不存在", "ok": False}

        entry_id = int(entry["id"])
        method = self._entry_method(entry)
        if method not in {"qb", "guangya"}:
            method = "qb"
        try:
            payload = json.loads(entry["payload"] or "{}")
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            if entry_already_claimed or db.claim_rss_entry(entry_id):
                db.record_rss_entry_failure(entry_id, "invalid_payload", False)
            return {"error": "条目数据无效", "ok": False, "method": method}

        torrent_url = str(payload.get("torrent_url") or payload.get("link") or "").strip()
        if not torrent_url:
            if entry_already_claimed or db.claim_rss_entry(entry_id):
                db.record_rss_entry_failure(entry_id, "missing_torrent_url", False)
            return {"error": "条目无种子链接", "ok": False, "method": method}

        infohash = self._torrent_infohash(torrent_url)
        if entry["status"] == "downloaded" or bool(entry["processed"]):
            return {
                "ok": True,
                "method": method,
                "status": "downloaded",
                "existing": True,
                "infohash": infohash,
                "already_processed": True,
            }
        if not entry_already_claimed and not db.claim_rss_entry(entry_id):
            return {"error": "条目正在提交或已被处理", "ok": False, "method": method}

        request_id = 0
        try:
            item = self._download_input(entry, torrent_url)
            runtime = qb_runtime_config if isinstance(qb_runtime_config, dict) else None
            qb_save_path = str(self._entry_value(entry, "qb_save_path", "") or "")
            if not qb_save_path and runtime is not None:
                qb_save_path = str(runtime.get("default_save_path") or "")
            qb_category = (
                str(runtime.get("category") or "")
                if runtime is not None
                else str(get("RSS_QB_CATEGORY", "") or "")
            )
            from app.indexers.downloads import submit_download_input

            submission = submit_download_input(
                item,
                method,
                origin=f"rss:{int(entry['rss_item_id'])}",
                gy_target_dir=str(self._entry_value(entry, "gy_target_dir", "") or ""),
                gy_target_name=str(
                    self._entry_value(entry, "gy_target_dir_name", "") or ""
                ),
                qb_save_path=qb_save_path,
                qb_category=qb_category,
                qb_runtime_config=runtime,
                qb_task_id_hint=infohash if method == "qb" else "",
                rss_item_id=int(entry["rss_item_id"]),
                log_path=_safe_download_source_marker(torrent_url),
            )
            request_id = int(submission.get("request_id") or 0)
        except ValueError:
            db.record_rss_entry_failure(entry_id, "invalid_payload", False)
            db.add_download_log(
                source=method,
                title=entry["title"],
                path=_safe_download_source_marker(torrent_url),
                rss_item_id=int(entry["rss_item_id"]),
                status="failed",
                backend_task_id=infohash,
                error="RSS 下载链接格式无效",
            )
            return {
                "ok": False,
                "method": method,
                "status": "failed",
                "existing": False,
                "unverified": False,
                "infohash": infohash,
                "error": "RSS 下载链接格式无效",
            }
        except Exception as exc:
            logger.warning(
                "RSS 统一下载提交异常 entry_id=%s method=%s type=%s",
                entry_id,
                method,
                type(exc).__name__,
            )
            db.record_rss_entry_failure(
                entry_id,
                "guangya_outcome_unknown" if method == "guangya" else "qb_outcome_unknown",
                False,
            )
            db.add_download_log(
                source=method,
                title=entry["title"],
                path=_safe_download_source_marker(torrent_url),
                rss_item_id=int(entry["rss_item_id"]),
                request_id=request_id or None,
                status="failed",
                backend_task_id=infohash,
                error="统一下载请求处理结果未知，请人工核对",
            )
            return {
                "ok": False,
                "method": method,
                "status": "failed",
                "existing": False,
                "unverified": False,
                "infohash": infohash,
                "error": "提交结果待核对，请先检查下载器状态，勿直接重复提交",
                "review_required": True,
                "request_id": request_id,
            }

        summary = submission.get("summary") or {}
        dispatch = submission.get("dispatch") or {}
        current_request = db.get_download_request(request_id) if request_id else None
        existing = self._accepted_duplicate(summary, current_request, method)
        ok = str(summary.get("status") or "") in {"submitted", "partial"} or existing
        backend = (dispatch.get("results") or {}).get(method) or {}
        task_id = str(backend.get("task_id") or "")
        if not task_id and current_request is not None and method == "qb":
            task_id = str(current_request["qb_task_id"] or "")
        unverified = bool(method == "qb" and ok and not task_id)
        error = str(summary.get("error") or "")
        review_required = False
        if ok:
            db.update_rss_entry_status(entry_id, "downloaded")
            if summary.get("duplicate"):
                db.add_download_log(
                    source=method,
                    title=entry["title"],
                    path=_safe_download_source_marker(torrent_url),
                    rss_item_id=int(entry["rss_item_id"]),
                    request_id=request_id or None,
                    status="existing",
                    backend_task_id=(
                        str(current_request["qb_task_id"] or "")
                        if current_request is not None and method == "qb"
                        else str(current_request["gy_task_id"] or "")
                        if current_request is not None
                        else infohash
                    ),
                )
        else:
            failure_code, retryable, review_required = self._backend_failure(method, submission)
            if summary.get("duplicate") and current_request is not None:
                backend_field = "qb_status" if method == "qb" else "gy_status"
                backend_status = str(current_request[backend_field] or "").strip().lower()
                if backend_status in {"outcome_unknown", "manual_review"}:
                    failure_code = (
                        "qb_outcome_unknown"
                        if method == "qb"
                        else "guangya_outcome_unknown"
                    )
                    retryable = False
                    review_required = True
            db.record_rss_entry_failure(entry_id, failure_code, retryable)
            if review_required:
                error = "提交结果待核对，请先检查下载器状态，勿直接重复提交"
            elif not error:
                error = "下载后端提交失败"

        result = {
            "ok": ok,
            "method": method,
            "status": "downloaded" if ok else "failed",
            "existing": existing,
            "unverified": unverified,
            "infohash": infohash,
            "request_id": request_id,
        }
        if not ok:
            result["error"] = error or "提交失败"
            if review_required:
                result["review_required"] = True
        return result

    def download(self, entry_id: int) -> dict:
        """下载单条条目并建立可持续跟踪的统一下载请求。"""
        return self._download_entry(db.get_rss_entry(entry_id))

    def download_many(self, entry_ids: list[int]) -> dict:
        ids = list(dict.fromkeys(int(item) for item in entry_ids))[:_RSS_DOWNLOAD_BATCH_SIZE]
        entries = [db.get_rss_entry(entry_id) for entry_id in ids]
        succeeded: list[dict] = []
        existing: list[dict] = []
        unverified: list[dict] = []
        failed: list[dict] = []
        jobs = list(enumerate(zip(ids, entries, strict=True)))
        groups: dict[str, list[tuple[int, tuple[int, object]]]] = {}
        for position, job in jobs:
            _entry_id, entry = job
            torrent_url = self._entry_torrent_url(entry) if entry else ""
            if torrent_url and entry is not None:
                try:
                    from app.modules.download_dispatcher import request_keys

                    group_key = request_keys(
                        self._download_input(entry, torrent_url)
                    )[0]
                except (TypeError, ValueError):
                    group_key = f"invalid:{position}"
            else:
                group_key = f"missing:{position}"
            groups.setdefault(group_key, []).append((position, job))

        def submit_group(
            group: list[tuple[int, tuple[int, object]]],
        ) -> list[tuple[int, int, dict]]:
            # 相同资源保持顺序；不同资源最多四路提交。统一 request_key 在 DB
            # 内提供跨订阅、跨入口幂等，不再维护 RSS 专属下载后端 claim。
            return [
                (position, entry_id, self._download_entry(entry))
                for position, (entry_id, entry) in group
            ]

        grouped_jobs = list(groups.values())
        worker_count = min(4, len(grouped_jobs))
        if worker_count > 1:
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="rss-download",
            ) as executor:
                grouped_outcomes = list(executor.map(submit_group, grouped_jobs))
        else:
            grouped_outcomes = [submit_group(group) for group in grouped_jobs]
        outcomes = sorted(
            (item for group in grouped_outcomes for item in group),
            key=lambda item: item[0],
        )

        for _position, entry_id, result in outcomes:
            item = {
                "id": entry_id,
                "method": result.get("method"),
                "infohash": result.get("infohash") or "",
                "request_id": int(result.get("request_id") or 0),
            }
            if result.get("existing"):
                existing.append(item)
            elif result.get("unverified") and result.get("ok"):
                unverified.append(item)
            elif result.get("ok"):
                succeeded.append(item)
            else:
                failed_item = {
                    "id": entry_id,
                    "error": result.get("error") or "提交失败",
                }
                if result.get("review_required"):
                    failed_item["review_required"] = True
                failed.append(failed_item)
        outcome_unknown_count = sum(
            1 for item in failed if item.get("review_required")
        )
        return {
            "total": len(ids),
            "succeeded": succeeded,
            "existing": existing,
            "unverified": unverified,
            "failed": failed,
            "success_count": len(succeeded),
            "existing_count": len(existing),
            "unverified_count": len(unverified),
            "failure_count": len(failed),
            "outcome_unknown_count": outcome_unknown_count,
            "review_required": outcome_unknown_count > 0,
        }

    def submit_pending_qb_snapshot(
        self,
        expected_entries: list[dict],
        runtime_config: dict,
    ) -> dict:
        """复核并提交 Agent 已确认的 qB pending 集合。"""
        return self._submit_qb_snapshot(
            expected_entries,
            runtime_config,
            claim=db.claim_pending_rss_qb_entries,
        )

    def retry_failed_qb_snapshot(
        self,
        expected_entries: list[dict],
        runtime_config: dict,
    ) -> dict:
        """复核并重试 Agent 已确认的可安全重试 qB 失败集合。"""
        return self._submit_qb_snapshot(
            expected_entries,
            runtime_config,
            claim=db.claim_retryable_failed_rss_qb_entries,
        )

    def _submit_qb_snapshot(
        self,
        expected_entries: list[dict],
        runtime_config: dict,
        *,
        claim,
    ) -> dict:
        requested = len(expected_entries)
        if not requested or requested > 20 or not str(runtime_config.get("url") or "").strip():
            return {
                "ok": False, "conflict": True, "requested": requested,
                "claimed": 0, "submitted": 0, "failed": 0,
            }
        claimed_rows = claim(
            expected_entries,
            default_method=str(runtime_config.get("default_method") or "qb"),
        )
        if len(claimed_rows) != requested:
            return {
                "ok": False, "conflict": True, "requested": requested,
                "claimed": 0, "submitted": 0, "failed": 0,
            }
        submitted, failed, outcome_unknown = self._submit_claimed_qb_rows(
            claimed_rows, runtime_config
        )
        result = {
            "ok": failed == 0,
            "conflict": False,
            "requested": requested,
            "claimed": len(claimed_rows),
            "submitted": submitted,
            "failed": failed,
        }
        if outcome_unknown:
            result["outcome_unknown"] = outcome_unknown
        return result

    def _submit_claimed_qb_rows(
        self, claimed_rows: list, runtime_config: dict
    ) -> tuple[int, int, int]:
        """提交已由 Agent 原子确认的 RSS 条目，仍复用统一下载状态机。"""
        submitted = 0
        failed = 0
        outcome_unknown_count = 0
        for row in claimed_rows:
            result = self._download_entry(
                row,
                entry_already_claimed=True,
                qb_runtime_config=runtime_config,
            )
            if result.get("ok"):
                submitted += 1
            else:
                failed += 1
                if result.get("review_required"):
                    outcome_unknown_count += 1
        return submitted, failed, outcome_unknown_count

    def auto_download(self, sub_id: int, *, expected_revision: str = "") -> dict:
        """刷新后自动下载所有 pending 条目。"""
        refreshed = self.refresh(sub_id, expected_revision=expected_revision)
        if refreshed.get("error"):
            return refreshed
        rows = db.list_rss_entries(
            sub_id=sub_id, status="pending", order="received_desc"
        )
        subscription = db.get_rss_subscription(sub_id)
        exclude = self._split_keywords(
            str(subscription["exclude_keywords"] or "") if subscription else ""
        )
        excluded_ids = [
            int(row["id"]) for row in rows
            if exclude and self._excluded(
                str(row["title"] or "") if "title" in row.keys() else "",
                exclude,
            )
        ]
        filtered = db.skip_pending_rss_entries(
            excluded_ids, "命中当前订阅排除关键词"
        )
        excluded = set(excluded_ids)
        pending_ids = [
            int(row["id"]) for row in rows if int(row["id"]) not in excluded
        ]
        ids = pending_ids[:_RSS_AUTO_DOWNLOAD_MAX_ENTRIES]
        deadline = time.monotonic() + _RSS_AUTO_DOWNLOAD_DEADLINE_SECONDS
        result = {
            "total": 0,
            "succeeded": [],
            "existing": [],
            "unverified": [],
            "failed": [],
            "success_count": 0,
            "existing_count": 0,
            "unverified_count": 0,
            "failure_count": 0,
            "outcome_unknown_count": 0,
            "review_required": False,
        }
        # download_many 是面向 Web/TG 批量操作的受控接口，单次最多 20 条；
        # 自动订阅必须消费本轮全部 pending，不能把 API 上限误当成业务上限。
        processed = 0
        for offset in range(0, len(ids), _RSS_DOWNLOAD_BATCH_SIZE):
            if time.monotonic() >= deadline:
                break
            batch = self.download_many(ids[offset:offset + _RSS_DOWNLOAD_BATCH_SIZE])
            processed += len(ids[offset:offset + _RSS_DOWNLOAD_BATCH_SIZE])
            for key in ("succeeded", "existing", "unverified", "failed"):
                result[key].extend(batch.get(key) or [])
            for key in (
                "total", "success_count", "existing_count", "unverified_count",
                "failure_count", "outcome_unknown_count",
            ):
                result[key] += max(0, int(batch.get(key) or 0))
            result["review_required"] = bool(result["review_required"]) or bool(
                batch.get("review_required")
            )
        outcome_unknown_count = max(
            0, int(result.get("outcome_unknown_count") or 0)
        )
        return {
            "refresh": refreshed,
            "downloaded": result["success_count"],
            "existing": result["existing_count"],
            "unverified": result["unverified_count"],
            "failed": result["failure_count"],
            "outcome_unknown_count": outcome_unknown_count,
            "review_required": bool(result.get("review_required"))
            or outcome_unknown_count > 0,
            "filtered": filtered,
            "deferred": max(0, len(pending_ids) - processed),
        }

    # ===== 工具 =====
    @staticmethod
    def _split_keywords(raw: str) -> list[str]:
        if not raw:
            return []
        return [k.strip() for k in re.split(r"[,，\s]+", raw) if k.strip()]

    @staticmethod
    def _excluded(title: str, keywords: list[str]) -> bool:
        t = title.lower()
        return any(k.lower() in t for k in keywords)
