"""RSS 订阅引擎（mikan 蜜柑计划适配）。

能力：
- MikanParser：feedparser 解析 mikan RSS，提取标题/磁力种子/发布时间/guid
- RSSEngine：订阅项管理、刷新拉取去重、下载联动（推 qB / 推光鸭离线，跨下载器去重）

下载目标由配置 RSS_DOWNLOAD_METHOD 决定：qb / guangya。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import unicodedata
from urllib.parse import unquote, urlsplit
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import feedparser
import requests

from app import database as db
from app.config import get, get_bool
from app.logger import get_logger
from app.modules.media_identity import build_media_key, parse_episode_label
from app.indexers.providers.base import magnet_infohash

logger = get_logger(__name__)

RSS_REFRESH_BUSY_ERROR = "订阅正在刷新，请稍后重试"
RSS_REFRESH_CONFLICT_ERROR = "订阅配置已变化，请重新确认"
_RSS_REFRESH_GATE_LOCK = threading.Lock()
_RSS_REFRESHING_SUBSCRIPTIONS: set[int] = set()
_RSS_CONNECT_TIMEOUT_SECONDS = 10
_RSS_READ_TIMEOUT_SECONDS = 30
_RSS_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


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
        "media_default_season": int(value("media_default_season", 1) or 1),
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
    runtime = {
        "url": str(get("QB_URL", "") or "").strip(),
        "username": str(get("QB_USERNAME", "") or ""),
        "password": str(get("QB_PASSWORD", "") or ""),
        "api_key": str(get("QB_API_KEY", "") or ""),
        "category": str(get("RSS_QB_CATEGORY", "") or ""),
        "default_save_path": str(get("RSS_QB_SAVE_PATH", "") or ""),
        "default_method": str(get("RSS_DOWNLOAD_METHOD", "qb") or "qb").strip().lower(),
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

    def parse(self, url: str) -> list[RSSEntry]:
        """拉取并解析一个 mikan RSS 源，返回条目列表。"""
        self.last_error_code = ""
        try:
            with requests.get(
                url,
                headers={"User-Agent": self.USER_AGENT},
                timeout=(_RSS_CONNECT_TIMEOUT_SECONDS, _RSS_READ_TIMEOUT_SECONDS),
                stream=True,
            ) as response:
                response.raise_for_status()
                payload = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    payload.extend(chunk)
                    if len(payload) > _RSS_MAX_RESPONSE_BYTES:
                        raise ValueError("RSS 响应体过大")
                response_headers = dict(response.headers)
                response_headers.setdefault("content-location", response.url)
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
            default_season = int(sub["media_default_season"] or 1)
            skip_existing_requested = bool(sub["skip_existing_episodes"] and media_tmdb_id)
            library_positions: set[tuple[int, int]] = set()
            library_check = {"sources": 0, "ready": 0, "unavailable": 0, "truncated": 0}
            failed_sources = 0
            parsed_entries: list[RSSEntry] = []
            for url in urls:
                entries = self.parser.parse(url)
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
            _release_rss_refresh(sub_id)

    # ===== 下载联动 =====
    @staticmethod
    def _torrent_infohash(value: str) -> str:
        direct = magnet_infohash(value)
        if direct:
            return direct.lower()
        try:
            path = unquote(urlsplit(str(value or "")).path)
        except Exception:
            return ""
        for part in reversed([item for item in path.split("/") if item]):
            match = re.fullmatch(r"(?i)([0-9a-f]{40})(?:\.torrent)?", part)
            if match:
                return match.group(1).lower()
        return ""

    @classmethod
    def _submission_claim_key(cls, torrent_url: str) -> str:
        """返回后端提交的持久幂等键；不透明 URL 使用命名空间指纹。"""
        infohash = cls._torrent_infohash(torrent_url)
        if infohash:
            return infohash
        normalized_url = str(torrent_url or "").strip()
        if not normalized_url:
            return ""
        return hashlib.blake2b(
            ("rss-opaque-url-v1\0" + normalized_url).encode("utf-8"),
            digest_size=20,
        ).hexdigest()

    def _qb_client(self):
        from app.clients.qbittorrent import QBittorrentClient
        return QBittorrentClient(
            url=get("QB_URL"),
            username=get("QB_USERNAME"),
            password=get("QB_PASSWORD"),
            api_key=get("QB_API_KEY"),
        )

    @staticmethod
    def _entry_method(entry) -> str:
        return (entry["download_method"] or get("RSS_DOWNLOAD_METHOD", "qb")).strip().lower()

    @staticmethod
    def _entry_torrent_url(entry) -> str:
        try:
            payload = json.loads(entry["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        return str(payload.get("torrent_url") or payload.get("link") or "").strip()

    def _download_entry(
        self,
        entry,
        *,
        qb_client=None,
        known_hashes: set[str] | None = None,
        snapshot_error: str = "",
    ) -> dict:
        if not entry:
            return {"error": "条目不存在", "ok": False}

        entry_id = int(entry["id"])
        method = self._entry_method(entry)
        try:
            payload = json.loads(entry["payload"] or "{}")
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            if db.claim_rss_entry(entry_id):
                db.record_rss_entry_failure(entry_id, "invalid_payload", False)
            return {"error": "条目数据无效", "ok": False, "method": method}

        torrent_url = str(payload.get("torrent_url") or payload.get("link") or "").strip()
        if not torrent_url:
            if db.claim_rss_entry(entry_id):
                db.record_rss_entry_failure(entry_id, "missing_torrent_url", False)
            return {"error": "条目无种子链接", "ok": False, "method": method}

        infohash = self._torrent_infohash(torrent_url)
        claim_key = self._submission_claim_key(torrent_url)
        hashes = known_hashes if known_hashes is not None else set()
        if entry["status"] == "downloaded" or bool(entry["processed"]):
            if method == "qb" and infohash and infohash in hashes:
                return {
                    "ok": True, "method": "qb", "status": "downloaded",
                    "existing": True, "infohash": infohash, "already_processed": True,
                }
            return {
                "error": "条目已处理；如需重新下载，请先标记为未处理",
                "ok": False,
            }

        if method == "qb" and snapshot_error:
            return {"error": snapshot_error, "ok": False, "method": "qb"}

        guangya_claim = ""
        guangya_lease = ""
        qb_claim = ""
        qb_lease = ""
        entry_managed_by_backend_claim = False
        if method == "guangya":
            claim = db.claim_rss_guangya_download(claim_key, entry_id)
            guangya_claim = str(claim.get("status") or "")
            guangya_lease = str(claim.get("lease_token") or "")
            if guangya_claim in {"busy", "unknown", "unavailable"}:
                messages = {
                    "busy": "相同资源正在提交，请稍后刷新状态",
                    "unknown": "相同资源的提交结果待核对，已阻止重复提交",
                    "unavailable": "条目正在提交或已被处理",
                }
                return {
                    "error": messages[guangya_claim],
                    "ok": False,
                    "method": method,
                    "infohash": infohash,
                    "review_required": guangya_claim == "unknown",
                }
            entry_managed_by_backend_claim = guangya_claim in {"claimed", "submitted"}
        elif method == "qb":
            claim = db.claim_rss_qb_download(claim_key, entry_id)
            qb_claim = str(claim.get("status") or "")
            qb_lease = str(claim.get("lease_token") or "")
            if qb_claim in {"busy", "unknown", "unavailable"}:
                messages = {
                    "busy": "相同资源正在提交，请稍后刷新状态",
                    "unknown": "相同资源的提交结果待核对，已阻止重复提交",
                    "unavailable": "条目正在提交或已被处理",
                }
                return {
                    "error": messages[qb_claim],
                    "ok": False,
                    "method": method,
                    "infohash": infohash,
                    "review_required": qb_claim == "unknown",
                }
            entry_managed_by_backend_claim = qb_claim in {"claimed", "submitted"}
        elif not db.claim_rss_entry(entry_id):
            return {"error": "条目正在提交或已被处理", "ok": False}

        existing = bool(
            (method == "qb" and infohash and infohash in hashes)
            or guangya_claim == "submitted"
            or qb_claim == "submitted"
        )
        unverified = False
        failure_code = ""
        retryable = False
        error = ""
        outcome_unknown = False
        try:
            if existing:
                ok = True
            elif method == "guangya":
                submission = self._push_guangya(
                    torrent_url,
                    entry["title"],
                    target_dir_id=entry["gy_target_dir"] or "",
                    target_dir_name=entry["gy_target_dir_name"] or "",
                )
                ok = bool(submission.get("ok"))
                outcome_unknown = bool(
                    submission.get("outcome_unknown")
                    or submission.get("partial_success")
                )
                if not ok:
                    failure_code = (
                        "guangya_outcome_unknown"
                        if outcome_unknown else "guangya_submit_failed"
                    )
            else:
                method = "qb"
                push_kwargs = {"save_path": entry["qb_save_path"] or ""}
                if qb_client is not None:
                    push_kwargs["client"] = qb_client
                push_result = self._push_qb_detailed(torrent_url, **push_kwargs)
                ok = bool(push_result.ok)
                failure_code = str(push_result.failure_code or "")
                retryable = bool(push_result.retryable)
                outcome_unknown = failure_code == "qb_outcome_unknown"
                unverified = bool(ok and not infohash)
                if ok and infohash:
                    hashes.add(infohash)
        except Exception as exc:
            logger.warning(
                "RSS 下载失败 entry_id=%s method=%s type=%s",
                entry_id,
                method,
                type(exc).__name__,
            )
            ok = False
            outcome_unknown = True
            failure_code = (
                "guangya_outcome_unknown"
                if method == "guangya" else "qb_outcome_unknown"
            )
            retryable = False

        if guangya_claim == "claimed":
            outcome = (
                "submitted" if ok else
                "unknown" if outcome_unknown else
                "failed"
            )
            finalized = db.finalize_rss_guangya_download(
                claim_key, entry_id, guangya_lease, outcome=outcome
            )
            if not finalized:
                logger.error(
                    "RSS 光鸭提交终态写入冲突 entry_id=%s outcome=%s",
                    entry_id,
                    outcome,
                )
                ok = False
                outcome_unknown = True
                failure_code = "guangya_outcome_unknown"
                error = "提交结果已返回，但状态写入冲突，请人工核对"
        elif qb_claim == "claimed":
            outcome = (
                "submitted" if ok else
                "unknown" if outcome_unknown else
                "failed"
            )
            finalized = db.finalize_rss_qb_download(
                claim_key, entry_id, qb_lease, outcome=outcome,
                failure_code=failure_code, retryable=retryable,
            )
            if not finalized:
                logger.error(
                    "RSS qB 提交终态写入冲突 entry_id=%s outcome=%s",
                    entry_id,
                    outcome,
                )
                ok = False
                outcome_unknown = True
                failure_code = "qb_outcome_unknown"
                error = "提交结果已返回，但状态写入冲突，请人工核对"

        status = "downloaded" if ok else "failed"
        if not entry_managed_by_backend_claim:
            if ok:
                db.update_rss_entry_status(entry_id, status)
            else:
                db.record_rss_entry_failure(
                    entry_id, failure_code or "unknown_failure", retryable
                )
        if not ok and not error:
            error = (
                "提交结果待核对，请先检查下载器状态，勿直接重复提交"
                if outcome_unknown else
                "下载后端提交失败"
            )
        log_status = (
            "existing" if existing else
            "unverified" if unverified else
            "success" if ok else "failed"
        )
        db.add_download_log(
            source=method,
            title=entry["title"],
            path=_safe_download_source_marker(torrent_url),
            rss_item_id=entry["rss_item_id"],
            status=log_status,
            backend_task_id=infohash,
            error=error,
        )
        result = {
            "ok": ok,
            "method": method,
            "status": status,
            "existing": existing,
            "unverified": unverified,
            "infohash": infohash,
        }
        if not ok:
            result["error"] = error or "提交失败"
            if outcome_unknown:
                result["review_required"] = True
        return result

    def _qb_snapshot(self, entries: list) -> tuple[object | None, set[str], str]:
        # 只有能提取 infohash 的 qB 条目才需要提交前对账。
        # 对不透明下载链接无法可靠去重，继续提交并明确标记为“待核验”，
        # 避免因 qB 列表暂时不可读而阻断原有单条下载契约。
        needs_reconcile = any(
            self._entry_method(entry) == "qb"
            and bool(self._torrent_infohash(self._entry_torrent_url(entry)))
            for entry in entries
            if entry
        )
        if not needs_reconcile:
            return None, set(), ""
        client = self._qb_client()
        try:
            hashes = {
                str(task.hash or "").strip().lower()
                for task in client.list_torrents()
                if str(task.hash or "").strip()
            }
            return client, hashes, ""
        except Exception as exc:
            logger.error("读取 qB 任务快照失败 type=%s", type(exc).__name__)
            return client, set(), "无法读取 qB 任务列表，已停止提交以避免重复任务"

    def download(self, entry_id: int) -> dict:
        """下载单条条目；qB 提交前先按 infohash 对账。"""
        entry = db.get_rss_entry(entry_id)
        client, hashes, snapshot_error = self._qb_snapshot([entry] if entry else [])
        return self._download_entry(
            entry, qb_client=client, known_hashes=hashes, snapshot_error=snapshot_error
        )

    def download_many(self, entry_ids: list[int]) -> dict:
        ids = list(dict.fromkeys(int(item) for item in entry_ids))[:100]
        entries = [db.get_rss_entry(entry_id) for entry_id in ids]
        client, hashes, snapshot_error = self._qb_snapshot(entries)
        succeeded: list[dict] = []
        existing: list[dict] = []
        unverified: list[dict] = []
        failed: list[dict] = []
        for entry_id, entry in zip(ids, entries):
            result = self._download_entry(
                entry, qb_client=client, known_hashes=hashes,
                snapshot_error=snapshot_error,
            )
            item = {
                "id": entry_id,
                "method": result.get("method"),
                "infohash": result.get("infohash") or "",
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
        from app.clients.qbittorrent import QBittorrentClient, TorrentAddResult

        client = QBittorrentClient(
            url=str(runtime_config.get("url") or ""),
            username=str(runtime_config.get("username") or ""),
            password=str(runtime_config.get("password") or ""),
            api_key=str(runtime_config.get("api_key") or ""),
            timeout=max(1, int(runtime_config.get("timeout") or 10)),
        )
        category = str(runtime_config.get("category") or "")
        default_save_path = str(runtime_config.get("default_save_path") or "")
        submitted = 0
        failed = 0
        outcome_unknown_count = 0
        for row in claimed_rows:
            entry_id = int(row["id"])
            title = str(row["title"] or "")
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = None
            if not isinstance(payload, dict):
                db.record_rss_entry_failure(entry_id, "invalid_payload", False)
                failed += 1
                continue
            torrent_url = str(payload.get("torrent_url") or payload.get("link") or "")
            if not torrent_url:
                db.record_rss_entry_failure(entry_id, "missing_torrent_url", False)
                failed += 1
                continue

            infohash = self._torrent_infohash(torrent_url)
            claim_key = self._submission_claim_key(torrent_url)
            claim_status = ""
            lease_token = ""
            existing = False
            claim = db.claim_rss_qb_download(
                claim_key, entry_id, entry_already_claimed=True
            )
            claim_status = str(claim.get("status") or "")
            lease_token = str(claim.get("lease_token") or "")
            if claim_status == "submitted":
                existing = True
                result = TorrentAddResult(True, "", False)
            elif claim_status != "claimed":
                if claim_status == "busy":
                    failure_code = "qb_dedupe_busy"
                    retryable = True
                    failure_message = "相同资源正在提交，本条已保留为可重试状态"
                elif claim_status == "unknown":
                    failure_code = "qb_outcome_unknown"
                    retryable = False
                    failure_message = "相同资源提交结果待核对，已阻止重复提交"
                else:
                    failure_code = "unknown_failure"
                    retryable = False
                    failure_message = "RSS 条目状态已变化，本次未提交"
                db.record_rss_entry_failure(entry_id, failure_code, retryable)
                failed += 1
                if failure_code == "qb_outcome_unknown":
                    outcome_unknown_count += 1
                db.add_download_log(
                    source="qb", title=title,
                    path=_safe_download_source_marker(torrent_url),
                    rss_item_id=int(row["rss_item_id"]), status="failed",
                    backend_task_id=infohash,
                    error=failure_message,
                )
                continue
            if not existing:
                try:
                    result = client.add_torrent_detailed(
                        urls=torrent_url,
                        save_path=str(row["qb_save_path"] or "") or default_save_path,
                        category=category,
                    )
                except Exception as exc:
                    logger.warning(
                        "Agent RSS qB 提交失败 entry_id=%s type=%s",
                        entry_id,
                        type(exc).__name__,
                    )
                    result = TorrentAddResult(False, "qb_outcome_unknown", False)

            outcome_unknown = result.failure_code == "qb_outcome_unknown"
            if claim_status == "claimed":
                finalized = db.finalize_rss_qb_download(
                    claim_key, entry_id, lease_token,
                    outcome=(
                        "submitted" if result.ok else
                        "unknown" if outcome_unknown else "failed"
                    ),
                    failure_code=result.failure_code,
                    retryable=bool(result.retryable),
                )
                if not finalized:
                    logger.error(
                        "Agent RSS qB 终态写入冲突 entry_id=%s", entry_id
                    )
                    result = TorrentAddResult(False, "qb_outcome_unknown", False)
                    outcome_unknown = True
            if result.ok:
                submitted += 1
            else:
                failed += 1
                if outcome_unknown:
                    outcome_unknown_count += 1
            db.add_download_log(
                source="qb",
                title=title,
                path=_safe_download_source_marker(torrent_url),
                rss_item_id=int(row["rss_item_id"]),
                status="existing" if existing else "success" if result.ok else "failed",
                backend_task_id=infohash,
                error=(
                    "提交结果待人工核对" if outcome_unknown else ""
                ),
            )
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
        ids = [int(row["id"]) for row in rows if int(row["id"]) not in excluded]
        result = self.download_many(ids) if ids else {
            "success_count": 0, "existing_count": 0,
            "unverified_count": 0, "failure_count": 0,
            "outcome_unknown_count": 0, "review_required": False,
        }
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
        }

    # ===== 推送实现 =====
    def _push_qb(
        self, torrent_url: str, save_path: str = "", *, client=None
    ) -> bool:
        """兼容旧调用者的布尔接口，并允许复用提交前 qB 快照客户端。"""
        return self._push_qb_detailed(
            torrent_url, save_path=save_path, client=client
        ).ok

    def _push_qb_detailed(
        self, torrent_url: str, save_path: str = "", *, client=None
    ):
        from app.clients.qbittorrent import TorrentAddResult

        c = client or self._qb_client()
        category = get("RSS_QB_CATEGORY", "")
        save_path = save_path or get("RSS_QB_SAVE_PATH", "")
        detailed = getattr(c, "add_torrent_detailed", None)
        if callable(detailed):
            return detailed(
                urls=torrent_url, save_path=save_path, category=category
            )
        ok = bool(c.add_torrent(
            urls=torrent_url, save_path=save_path, category=category
        ))
        return TorrentAddResult(ok, "" if ok else "qb_rejected", False)

    def _push_guangya(self, torrent_url: str, title: str = "",
                      target_dir_id: str = "", target_dir_name: str = "") -> dict:
        from app.modules.offline import submit_offline
        return submit_offline(
            torrent_url,
            title=title,
            target_dir_id=target_dir_id,
            target_dir_name=target_dir_name,
        )

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
