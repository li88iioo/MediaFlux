"""媒体服务器客户端基类。

统一抽象 Emby / Jellyfin 的看板数据获取，屏蔽两者 API 差异。
子类实现 _request 及鉴权细节。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
import re
import time
from pathlib import PurePosixPath
from typing import Any, Optional

import requests

from app.logger import get_logger, log_throttled
from app.modules.media_server_path_mapping import (
    MediaServerPathMapping,
    apply_media_server_path_mapping,
    media_server_path_is_within,
    media_server_path_key,
    normalize_media_server_path,
)

logger = get_logger(__name__)

_RUNTIME_TICKS_PER_MINUTE = 600_000_000
_MAX_RUNTIME_MINUTES = 525_600
_MEDIA_ITEM_PAGE_SIZE = 5_000
_MAX_MEDIA_ITEMS_FOR_PRECISE_REFRESH = 100_000
_MAX_PRECISE_REFRESH_TARGETS_PER_LIBRARY = 64


class MediaLibraryEnumerationTooLarge(RuntimeError):
    """媒体库过大，无法安全枚举单条 Item；调用方应优先收敛到物理根。"""


def runtime_ticks_to_minutes(value: Any) -> int:
    """将 MediaBrowser 的 100ns RunTimeTicks 转为四舍五入分钟。"""
    if isinstance(value, bool):
        return 0
    try:
        ticks = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    if ticks <= 0 or ticks > _MAX_RUNTIME_MINUTES * _RUNTIME_TICKS_PER_MINUTE:
        return 0
    return max(1, (ticks + _RUNTIME_TICKS_PER_MINUTE // 2) // _RUNTIME_TICKS_PER_MINUTE)


def normalize_explicit_media_user_id(value: Any) -> str:
    """校验可安全嵌入 MediaBrowser 路径段的显式用户标识。"""
    normalized = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", normalized):
        raise ValueError("媒体服务器用户标识无效")
    return normalized


def normalize_playback_progress(value: Any) -> float:
    """将 MediaBrowser 播放百分比归一化为有限的 0..100 数值。"""
    if isinstance(value, bool):
        return 0.0
    try:
        progress = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(progress):
        return 0.0
    return round(max(0.0, min(progress, 100.0)), 2)


@dataclass
class Library:
    name: str
    item_type: str
    count: int = 0
    primary_image: str = ""
    id: str = ""
    web_url: str = ""


@dataclass
class MediaItem:
    id: str
    name: str
    type: str
    year: str = ""
    runtime: int = 0
    date_added: str = ""
    primary_image: str = ""
    overview: str = ""
    web_url: str = ""
    series_name: str = ""
    season_number: int | None = None
    episode_number: int | None = None
    last_played: str = ""
    progress: float = 0.0

    @property
    def display_name(self) -> str:
        return self.series_name or self.name

    @property
    def episode_label(self) -> str:
        if self.episode_number is None:
            return ""
        return f"第 {self.episode_number} 集"


@dataclass(frozen=True)
class SeriesCandidate:
    """媒体服务器中的剧集候选；内部 ID 只在服务端调用链中使用。"""

    id: str
    name: str
    year: str = ""
    tmdb_id: str = ""


@dataclass
class SeriesSearchResult:
    candidates: list[SeriesCandidate] = field(default_factory=list)
    total: int = 0
    truncated: bool = False


@dataclass
class SeriesEpisodeInventory:
    episodes: list[tuple[int, int]] = field(default_factory=list)
    total: int = 0
    truncated: bool = False
    ignored_specials: int = 0
    ignored_unknown: int = 0


@dataclass
class DashboardData:
    server_name: str
    server_type: str = ""
    web_url: str = ""
    libraries: list[Library] = field(default_factory=list)
    recent_added: list[MediaItem] = field(default_factory=list)
    recent_played: list[MediaItem] = field(default_factory=list)
    total_items: int = 0
    movie_count: int = 0
    series_count: int = 0
    episode_count: int = 0
    total_plays: int = 0
    online: bool = False
    error: str = ""
    server_product: str = ""
    server_version: str = ""
    partial_errors: list[str] = field(default_factory=list)


def _dedupe_texts(values: object) -> list[str]:
    return list(dict.fromkeys(
        str(item or "").strip()
        for item in (values or ())
        if str(item or "").strip()
    ))


class MediaServerClient:
    """媒体服务器客户端基类。"""

    display_name = "MediaServer"

    def __init__(
        self,
        url: str,
        token: str,
        timeout: int = 10,
        *,
        path_mappings: tuple[MediaServerPathMapping, ...] = (),
        allow_global_refresh_fallback: bool = False,
    ):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.path_mappings = tuple(path_mappings or ())
        self.allow_global_refresh_fallback = bool(allow_global_refresh_fallback)
        self._session = requests.Session()

    def close(self) -> None:
        """显式释放底层连接池；短生命周期探测不得依赖 GC 回收 socket。"""
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    # ---- 子类实现 ----
    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _request(
        self,
        path: str,
        params: Optional[dict] = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.url}{path}"
        try:
            resp = self._session.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=self.timeout if timeout is None else timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error("媒体服务器请求失败 server=%s path=%s type=%s", self.display_name, path, type(e).__name__)
            raise

    def list_virtual_folders(self) -> list[dict[str, Any]]:
        """返回可用于整理目标绑定的媒体库目录。"""
        data = self._request("/Library/VirtualFolders")
        return self._normalize_virtual_folders(data)

    @staticmethod
    def _normalize_virtual_folders(data: Any) -> list[dict[str, Any]]:
        """兼容 Jellyfin 裸数组与 Emby QueryResult 两种响应形状。"""
        if isinstance(data, dict):
            data = data.get("Items")
        if not isinstance(data, list):
            return []
        folders: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            library_id = str(item.get("ItemId") or "").strip()
            name = str(item.get("Name") or "").strip()
            if not library_id or not name:
                continue
            raw_locations = item.get("Locations")
            locations = [
                str(value).strip() for value in raw_locations
                if isinstance(value, str) and value.strip()
            ] if isinstance(raw_locations, list) else []
            folders.append({
                "id": library_id,
                "name": name,
                "locations": locations,
                "collection_type": str(item.get("CollectionType") or "").strip(),
            })
        return folders

    # ---- 看板数据（统一返回）----
    def get_dashboard(self) -> DashboardData:
        """聚合看板数据。任一步失败不影响其余。"""
        data = DashboardData(server_name=self.display_name)
        try:
            data.server_name, data.server_product, data.server_version = self._server_identity()
            data.online = True
        except Exception as e:
            data.error = str(e)
            return data

        parts = (
            ("libraries", self._libraries, []),
            ("recent_added", self._recent_added, []),
            ("recent_played", self._recent_played, []),
            ("total_items", self._total_items, 0),
            ("total_plays", self._total_plays, 0),
        )
        failure_types: list[str] = []
        for name, func, default in parts:
            try:
                setattr(data, name, func())
            except Exception as exc:
                setattr(data, name, default)
                data.partial_errors.append(name)
                failure_types.append(f"{name}:{type(exc).__name__}")
        if failure_types:
            log_throttled(
                logger,
                logging.WARNING,
                f"dashboard:{self.display_name}:{','.join(failure_types)}",
                "媒体服务器看板部分读取失败 server=%s parts=%s",
                self.display_name,
                ",".join(failure_types),
            )
        return data

    def _safe(self, func, default):
        try:
            return func()
        except Exception as exc:
            log_throttled(
                logger,
                logging.WARNING,
                f"media-server-safe:{self.display_name}:{func.__name__}:{type(exc).__name__}",
                "媒体服务器数据读取失败 server=%s operation=%s type=%s",
                self.display_name,
                func.__name__,
                type(exc).__name__,
            )
            return default

    # ---- 子类实现的数据方法 ----
    def _server_name(self) -> str:
        raise NotImplementedError

    def _server_identity(self) -> tuple[str, str, str]:
        """返回显示名称、产品名和版本；旧客户端可只实现 _server_name。"""
        return self._server_name(), self.display_name, ""

    def _libraries(self) -> list[Library]:
        raise NotImplementedError

    def recent_media(self, limit: int = 60) -> list[MediaItem]:
        """返回最近入库媒体，供独立媒体中心页面使用。"""
        return self._recent_added(limit=max(1, min(int(limit or 60), 200)))

    def continue_watching(self, user_id: str, *, limit: int = 12) -> list[MediaItem]:
        """按显式上游用户读取继续观看；不得回退管理员用户。"""
        raise NotImplementedError

    def search_media(self, query: str, limit: int = 12) -> list[MediaItem]:
        """按标题搜索媒体服务器内容。"""
        raise NotImplementedError

    def _user_id(self, *, deadline_at: float | None = None) -> str:
        """返回当前 API 用户 ID；由 Jellyfin / Emby 客户端实现。"""
        raise NotImplementedError

    def _remaining_timeout(self, deadline_at: float | None) -> float | None:
        """将单次请求限制在巡检任务的剩余时间内。"""
        if deadline_at is None:
            return None
        remaining = float(deadline_at) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("媒体库巡检截止时间已到")
        return min(float(self.timeout), remaining)

    @staticmethod
    def _provider_tmdb_id(provider_ids: Any) -> str:
        """提取可信的数字 TMDB ID，不接受模糊或复合值。"""
        if not isinstance(provider_ids, dict):
            return ""
        raw = next(
            (value for key, value in provider_ids.items() if str(key).casefold() == "tmdb"),
            "",
        )
        value = str(raw or "").strip()
        if not value.isascii() or not value.isdigit() or not 1 <= len(value) <= 10:
            return ""
        normalized = str(int(value))
        return normalized if normalized != "0" else ""

    @staticmethod
    def _items_payload(data: Any) -> tuple[list[dict[str, Any]], int]:
        if isinstance(data, list):
            items = [item for item in data if isinstance(item, dict)]
            return items, len(items)
        if not isinstance(data, dict):
            raise ValueError("媒体服务器响应结构无效")
        raw_items = data.get("Items", [])
        if not isinstance(raw_items, list):
            raise ValueError("媒体服务器响应结构无效")
        items = [item for item in raw_items if isinstance(item, dict)]
        try:
            total = int(data.get("TotalRecordCount", len(items)) or 0)
        except (TypeError, ValueError):
            total = len(items)
        return items, max(total, len(items))

    def has_tmdb_media(
        self, tmdb_id: str, media_type: str, *, parent_id: str = ""
    ) -> bool:
        """按 ProviderIds.Tmdb 精确判断电影或剧集是否存在，可限定媒体库。"""
        normalized_id = self._provider_tmdb_id({"Tmdb": tmdb_id})
        normalized_type = "Series" if str(media_type).strip().lower() == "tv" else "Movie"
        if not normalized_id:
            raise ValueError("TMDB ID 无效")
        params = {
            "Recursive": "true",
            "Limit": 20,
            "IncludeItemTypes": normalized_type,
            "Fields": "ProviderIds",
            "AnyProviderIdEquals": f"Tmdb.{normalized_id}",
        }
        if str(parent_id or "").strip():
            params["ParentId"] = str(parent_id).strip()
        data = self._request(
            f"/Users/{self._user_id()}/Items",
            params=params,
        )
        items, _total = self._items_payload(data)
        return any(
            self._provider_tmdb_id(item.get("ProviderIds")) == normalized_id
            for item in items
        )

    def find_series_candidates_by_tmdb(
        self, tmdb_id: str, limit: int = 20, *, parent_id: str = ""
    ) -> SeriesSearchResult:
        """按 ProviderIds.Tmdb 精确返回剧集候选，避免标题别名导致漏判。"""
        normalized_id = self._provider_tmdb_id({"Tmdb": tmdb_id})
        normalized_limit = max(1, min(int(limit or 20), 20))
        if not normalized_id:
            raise ValueError("TMDB ID 无效")
        params = {
            "Recursive": "true",
            "Limit": normalized_limit,
            "IncludeItemTypes": "Series",
            "Fields": "ProviderIds,ProductionYear",
            "AnyProviderIdEquals": f"Tmdb.{normalized_id}",
        }
        if str(parent_id or "").strip():
            params["ParentId"] = str(parent_id).strip()
        data = self._request(
            f"/Users/{self._user_id()}/Items",
            params=params,
        )
        items, total = self._items_payload(data)
        candidates: list[SeriesCandidate] = []
        for item in items:
            if self._provider_tmdb_id(item.get("ProviderIds")) != normalized_id:
                continue
            item_id = str(item.get("Id") or "").strip()
            name = str(item.get("Name") or "").strip()
            if not item_id or not name:
                continue
            candidates.append(SeriesCandidate(
                id=item_id,
                name=name,
                year=str(item.get("ProductionYear") or "").strip(),
                tmdb_id=normalized_id,
            ))
        return SeriesSearchResult(
            candidates=candidates,
            total=max(len(candidates), int(total or 0)),
            truncated=int(total or 0) > len(items),
        )

    def search_series_candidates(
        self, query: str, limit: int = 6, *, parent_id: str = ""
    ) -> SeriesSearchResult:
        """返回剧集候选及可靠的 ProviderIds.Tmdb 映射。"""
        normalized_limit = max(1, min(int(limit or 6), 10))
        params = {
            "SearchTerm": str(query or "").strip(),
            "Recursive": "true",
            "Limit": normalized_limit,
            "IncludeItemTypes": "Series",
            "Fields": "ProviderIds,ProductionYear",
            "SortBy": "SortName",
            "SortOrder": "Ascending",
        }
        if str(parent_id or "").strip():
            params["ParentId"] = str(parent_id).strip()
        data = self._request(
            f"/Users/{self._user_id()}/Items",
            params=params,
        )
        items, total = self._items_payload(data)
        candidates = [
            SeriesCandidate(
                id=str(item.get("Id") or ""),
                name=str(item.get("Name") or "").strip(),
                year=str(item.get("ProductionYear") or "").strip(),
                tmdb_id=self._provider_tmdb_id(item.get("ProviderIds")),
            )
            for item in items[:normalized_limit]
            if str(item.get("Id") or "").strip() and str(item.get("Name") or "").strip()
        ]
        return SeriesSearchResult(
            candidates=candidates,
            total=total,
            truncated=total > len(candidates),
        )

    def list_library_series(
        self,
        *,
        max_series: int = 50,
        page_size: int = 100,
        deadline_at: float | None = None,
    ) -> SeriesSearchResult:
        """分页枚举媒体库剧集；内部 ID 仅供后续服务端读取清单。"""
        # 后台全库巡检需要先以轻量元数据分页建立稳定目录，再按批次读取
        # 每部剧的集号。常规调用仍由上层传入的小上限约束。
        cap = max(1, min(int(max_series or 50), 5000))
        page = max(1, min(int(page_size or 100), 100))
        offset = 0
        candidates: list[SeriesCandidate] = []
        reported_total = 0
        exhausted = False
        user_id = self._user_id(deadline_at=deadline_at)

        while offset < cap:
            limit = min(page, cap - offset)
            data = self._request(
                f"/Users/{user_id}/Items",
                params={
                    "Recursive": "true",
                    "StartIndex": offset,
                    "Limit": limit,
                    "IncludeItemTypes": "Series",
                    "Fields": "ProviderIds,ProductionYear",
                    "SortBy": "SortName",
                    "SortOrder": "Ascending",
                },
                timeout=self._remaining_timeout(deadline_at),
            )
            items, total = self._items_payload(data)
            reported_total = max(reported_total, total)
            for item in items:
                item_id = str(item.get("Id") or "").strip()
                name = str(item.get("Name") or "").strip()
                if not item_id or not name:
                    continue
                candidates.append(SeriesCandidate(
                    id=item_id,
                    name=name,
                    year=str(item.get("ProductionYear") or "").strip(),
                    tmdb_id=self._provider_tmdb_id(item.get("ProviderIds")),
                ))
                if len(candidates) >= cap:
                    break
            offset += len(items)
            if not items or len(items) < limit or offset >= total:
                exhausted = True
                break

        return SeriesSearchResult(
            candidates=candidates[:cap],
            total=reported_total or offset,
            truncated=not exhausted and (reported_total > offset or len(candidates) >= cap),
        )

    def list_series_episode_inventory(
        self,
        series_id: str,
        *,
        max_episodes: int = 2000,
        page_size: int = 200,
        deadline_at: float | None = None,
        include_specials: bool = False,
    ) -> SeriesEpisodeInventory:
        """分页枚举剧集集号；默认忽略 Season 0，显式开启时纳入特典。"""
        cap = max(1, min(int(max_episodes or 2000), 2000))
        page = max(1, min(int(page_size or 200), 200))
        offset = 0
        seen: set[tuple[int, int]] = set()
        ignored_specials = 0
        ignored_unknown = 0
        reported_total = 0
        exhausted = False
        user_id = self._user_id(deadline_at=deadline_at)

        while offset < cap:
            limit = min(page, cap - offset)
            data = self._request(
                f"/Users/{user_id}/Items",
                params={
                    "ParentId": str(series_id or "").strip(),
                    "Recursive": "true",
                    "StartIndex": offset,
                    "Limit": limit,
                    "IncludeItemTypes": "Episode",
                    "Fields": (
                        "SeriesId,SeriesName,SeasonId,ParentIndexNumber,"
                        "IndexNumber,ProviderIds"
                    ),
                    "SortBy": "ParentIndexNumber,IndexNumber",
                    "SortOrder": "Ascending",
                },
                timeout=self._remaining_timeout(deadline_at),
            )
            items, total = self._items_payload(data)
            reported_total = max(reported_total, total)
            for item in items:
                try:
                    season = int(item.get("ParentIndexNumber"))
                    episode = int(item.get("IndexNumber"))
                except (TypeError, ValueError):
                    ignored_unknown += 1
                    continue
                if season == 0 and episode > 0:
                    if include_specials:
                        seen.add((season, episode))
                    else:
                        ignored_specials += 1
                elif season > 0 and episode > 0:
                    seen.add((season, episode))
                else:
                    ignored_unknown += 1
            offset += len(items)
            if not items or len(items) < limit or offset >= total:
                exhausted = True
                break

        return SeriesEpisodeInventory(
            episodes=sorted(seen),
            total=reported_total or offset,
            truncated=not exhausted and (reported_total > offset or offset >= cap),
            ignored_specials=ignored_specials,
            ignored_unknown=ignored_unknown,
        )

    def _recent_added(self, limit: int = 8) -> list[MediaItem]:
        raise NotImplementedError

    def _recent_played(self) -> list[MediaItem]:
        return []

    def _total_items(self) -> int:
        raise NotImplementedError

    def _total_plays(self) -> int:
        raise NotImplementedError

    # ---- 局部刷新（STRM 入库后通知）----
    def refresh_library(self, library_id: str) -> bool:
        raise NotImplementedError

    def refresh_all(self) -> bool:
        raise NotImplementedError

    @staticmethod
    def _normalize_media_path(path: object) -> str:
        try:
            return normalize_media_server_path(path)
        except Exception:
            return ""

    @staticmethod
    def _media_path_key(path: object) -> str:
        try:
            return media_server_path_key(path)
        except Exception:
            return ""

    def _library_root_items_with_paths(
        self, library_id: str, *, locations: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """分页查找虚拟媒体库对应的物理根 Folder。

        Jellyfin 多路径媒体库不会把物理根作为直属子项返回，因此必须递归查询
        Folder 后按虚拟库 Location 精确过滤；找到全部目标根后立即停止。
        """
        wanted = {
            self._media_path_key(item) for item in locations
            if self._media_path_key(item)
        }
        found: dict[str, dict[str, Any]] = {}
        start_index = 0
        page_size = 5000
        cap = 20000
        while start_index < cap:
            data = self._request(
                "/Items",
                params={
                    "ParentId": str(library_id),
                    "Recursive": "true",
                    "IncludeItemTypes": "Folder",
                    "Fields": "Path",
                    "StartIndex": start_index,
                    "Limit": page_size,
                },
            )
            if not isinstance(data, dict) or not isinstance(data.get("Items"), list):
                raise RuntimeError("媒体库物理根响应格式异常")
            rows = data["Items"]
            if len(rows) > page_size or any(not isinstance(item, dict) for item in rows):
                raise RuntimeError("媒体库物理根响应无效")
            for item in rows:
                item_id = str(item.get("Id") or "").strip()
                key = self._media_path_key(item.get("Path"))
                if not item_id or not key:
                    continue
                if not wanted or key in wanted:
                    found[key] = item
            if wanted and wanted.issubset(found):
                return list(found.values())
            try:
                total = int(data.get("TotalRecordCount") or 0)
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError("媒体库物理根总数无效") from exc
            next_index = start_index + len(rows)
            if not rows or next_index >= total:
                return list(found.values())
            start_index = next_index
        raise MediaLibraryEnumerationTooLarge("媒体库 Folder 超过物理根定位安全上限")

    def _library_items_with_paths(self, library_id: str) -> list[dict[str, Any]]:
        """完整分页列出库内可定位 Item；无法证明完整时失败关闭。"""
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        start_index = 0
        expected_total: int | None = None
        while start_index < _MAX_MEDIA_ITEMS_FOR_PRECISE_REFRESH:
            data = self._request(
                "/Items",
                params={
                    "ParentId": str(library_id),
                    "Recursive": "true",
                    "IncludeItemTypes": "Series,Season,Movie,Folder,BoxSet",
                    "Fields": "Path,SeriesId",
                    "StartIndex": start_index,
                    "Limit": _MEDIA_ITEM_PAGE_SIZE,
                },
            )
            if not isinstance(data, dict) or not isinstance(data.get("Items"), list):
                raise RuntimeError("媒体库条目响应格式异常，已停止精准刷新")
            total_raw = data.get("TotalRecordCount")
            if isinstance(total_raw, bool):
                raise RuntimeError("媒体库条目总数无效，已停止精准刷新")
            try:
                total = int(total_raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError("媒体库条目总数无效，已停止精准刷新") from exc
            if total < 0:
                raise RuntimeError("媒体库条目总数无效，已停止精准刷新")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise RuntimeError("媒体库条目分页总数发生变化，已停止精准刷新")
            if total > _MAX_MEDIA_ITEMS_FOR_PRECISE_REFRESH:
                raise MediaLibraryEnumerationTooLarge(
                    f"媒体库条目超过精准刷新安全上限 "
                    f"{_MAX_MEDIA_ITEMS_FOR_PRECISE_REFRESH}"
                )

            raw_items = data["Items"]
            if len(raw_items) > _MEDIA_ITEM_PAGE_SIZE:
                raise RuntimeError("媒体库条目分页超过请求上限，已停止精准刷新")
            if any(not isinstance(item, dict) for item in raw_items):
                raise RuntimeError("媒体库条目响应包含无效项目，已停止精准刷新")
            page = list(raw_items)
            new_items = 0
            for item in page:
                identity = str(item.get("Id") or "").strip()
                if not identity:
                    raise RuntimeError("媒体库条目缺少 ID，已停止精准刷新")
                if identity in seen:
                    continue
                seen.add(identity)
                collected.append(item)
                new_items += 1

            next_index = start_index + len(page)
            if next_index > total or len(collected) > total:
                raise RuntimeError("媒体库条目分页总数不一致，已停止精准刷新")
            if not page:
                if start_index == total and len(collected) == total:
                    return collected
                raise RuntimeError("媒体库条目分页提前结束，已停止精准刷新")
            if not new_items:
                raise RuntimeError("媒体库条目分页未前进，已停止精准刷新")
            start_index = next_index
            if start_index == total:
                if len(collected) != total:
                    raise RuntimeError("媒体库条目分页存在重复或缺失，已停止精准刷新")
                return collected
            if len(page) < _MEDIA_ITEM_PAGE_SIZE:
                raise RuntimeError("媒体库条目分页提前结束，已停止精准刷新")

        raise MediaLibraryEnumerationTooLarge(
            f"媒体库条目超过精准刷新安全上限 {_MAX_MEDIA_ITEMS_FOR_PRECISE_REFRESH}"
        )

    @staticmethod
    def _media_path_parent(path: str) -> str:
        normalized = str(path or "").rstrip("/")
        if not normalized:
            return ""
        parent = str(PurePosixPath(normalized).parent)
        return "" if parent in {".", "/"} else parent

    def _preferred_item_for_path(
        self, items: list[dict[str, Any]], target: str,
    ) -> dict[str, Any] | None:
        """定位覆盖变化路径的 Item；电视剧统一提升到 Series。

        Jellyfin 的 Movie.Path 通常指向具体 ``.strm``/视频文件，而刷新计划
        使用其父目录，因此电影额外允许“目标目录包含 Movie 文件”的匹配。
        """
        candidates: list[tuple[int, dict[str, Any]]] = []
        by_id: dict[str, dict[str, Any]] = {}
        for item in items:
            item_id = str(item.get("Id") or "").strip()
            if item_id:
                by_id[item_id] = item
            item_path = self._normalize_media_path(item.get("Path"))
            if not item_path:
                continue
            item_type = str(item.get("Type") or "").strip().casefold()
            covers = media_server_path_is_within(target, item_path)
            movie_child = bool(
                item_type == "movie"
                and self._media_path_key(self._media_path_parent(item_path))
                == self._media_path_key(target)
            )
            if covers or movie_child:
                candidates.append((len(item_path), item))
        if not candidates:
            return None

        def typed_candidates(item_type: str) -> list[tuple[int, dict[str, Any]]]:
            return [
                (length, item)
                for length, item in candidates
                if str(item.get("Type") or "").strip().casefold() == item_type
            ]

        # Movie.Path 往往是目标目录下的文件；电影优先于同路径树中的 Folder。
        movie_candidates = typed_candidates("movie")
        if movie_candidates:
            return max(movie_candidates, key=lambda entry: entry[0])[1]

        # 电视剧无论变化落在剧目录还是季目录，都只刷新一次 Series。
        season_candidates = typed_candidates("season")
        series_candidates = typed_candidates("series")
        if season_candidates:
            _length, season = max(season_candidates, key=lambda entry: entry[0])
            series_id = str(season.get("SeriesId") or "").strip()
            linked = by_id.get(series_id)
            if (
                linked is not None
                and str(linked.get("Type") or "").strip().casefold() == "series"
            ):
                return linked
        if series_candidates:
            return max(series_candidates, key=lambda entry: entry[0])[1]
        if season_candidates:
            return max(season_candidates, key=lambda entry: entry[0])[1]
        return max(candidates, key=lambda entry: entry[0])[1]

    def _finish_unlocatable_refresh(
        self, result: dict[str, Any], reason: str, *, allow_global: bool = True,
        permit_global_fallback: bool = False, retryable: bool = False,
    ) -> dict[str, Any]:
        """定位失败时默认安全跳过；自动链路绝不隐式全局扫描。"""
        result["fallback"] = reason
        result["retryable"] = bool(retryable)
        if (
            allow_global
            and permit_global_fallback
            and self.allow_global_refresh_fallback
        ):
            logger.warning(
                "[%s] %s，配置允许回退全局刷新", self.display_name, reason
            )
            result["scope"] = "global"
            result["endpoints"] = ["/Library/Refresh"]
            result["ok"] = self.refresh_all()
            return result
        logger.warning(
            "[%s] %s，已安全跳过；未触发全局媒体库扫描", self.display_name, reason
        )
        result["scope"] = "skipped"
        result["skipped"] = True
        result["ok"] = False
        return result

    def refresh_for_paths(
        self,
        paths: list[str],
        *,
        allowed_library_ids: tuple[str, ...] = (),
        allow_library_fallback: bool = True,
        allow_global_fallback: bool | None = None,
        skip_item_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """按变化路径执行最小范围刷新。

        已存在剧集统一提升到 Series；全新作品优先刷新对应物理根 Folder；
        只有无法获得物理根 Item 时才退到虚拟媒体库。自动调用方默认禁止全局
        ``/Library/Refresh``。
        """
        allowed_ids = {
            str(item or "").strip() for item in allowed_library_ids or ()
            if str(item or "").strip()
        }
        skipped_ids = {
            str(item or "").strip() for item in skip_item_ids or ()
            if str(item or "").strip()
        }
        permit_global_fallback = (
            self.allow_global_refresh_fallback
            if allow_global_fallback is None
            else bool(allow_global_fallback)
        )
        mapping_rows: list[dict[str, Any]] = []
        mapped_targets: list[str] = []
        for raw_path in paths or []:
            raw = str(raw_path or "").strip()
            if not raw:
                continue
            try:
                mapped, mapping = apply_media_server_path_mapping(raw, self.path_mappings)
            except Exception:
                mapped, mapping = self._normalize_media_path(raw), None
            normalized_source = self._normalize_media_path(raw)
            normalized_target = self._normalize_media_path(mapped)
            if not normalized_target:
                continue
            mapping_rows.append({
                "source": normalized_source,
                "target": normalized_target,
                "applied": mapping is not None,
                "mode": "explicit" if mapping is not None else "none",
            })
            mapped_targets.append(normalized_target)

        targets = list(dict.fromkeys(mapped_targets))
        result: dict[str, Any] = {
            "ok": False,
            "items": [],
            "folders": [],
            "libraries": [],
            "fallback": "",
            "requested": len(targets),
            "matched": 0,
            "unmatched": 0,
            "ambiguous": 0,
            "mapped": sum(1 for row in mapping_rows if row["applied"]),
            "path_mappings": mapping_rows,
            "scope": "none",
            "endpoints": [],
            "skipped": False,
            "retryable": False,
            "deduplicated": 0,
            "succeeded_target_ids": [],
            "allowed_libraries": sorted(allowed_ids),
        }
        if not targets:
            result["fallback"] = "本轮没有可定位的变化路径"
            result["scope"] = "skipped"
            result["skipped"] = True
            result["ok"] = True
            return result

        try:
            folders = self.list_virtual_folders()
        except Exception as exc:
            logger.warning("[%s] 读取媒体库目录失败: %s", self.display_name, exc)
            return self._finish_unlocatable_refresh(
                result,
                "媒体库目录读取失败",
                allow_global=not allowed_ids,
                permit_global_fallback=permit_global_fallback,
                retryable=True,
            )
        if allowed_ids:
            folders = [
                folder for folder in folders
                if str(folder.get("id") or "").strip() in allowed_ids
            ]

        source_for_target = {row["target"]: row["source"] for row in mapping_rows}
        library_for_target: dict[str, tuple[str, str]] = {}
        unmatched_targets: list[str] = []
        ambiguous_targets = 0
        for target in targets:
            direct_matches: list[tuple[int, str, str]] = []
            for folder in folders:
                library_id = str(folder.get("id") or "").strip()
                if not library_id:
                    continue
                for raw_location in folder.get("locations", []):
                    location = self._normalize_media_path(raw_location)
                    if location and media_server_path_is_within(target, location):
                        direct_matches.append((len(location), library_id, location))
            if direct_matches:
                longest = max(length for length, _library_id, _location in direct_matches)
                winners = {
                    (library_id, location)
                    for length, library_id, location in direct_matches
                    if length == longest
                }
                if len(winners) == 1:
                    library_for_target[target] = next(iter(winners))
                    continue
                ambiguous_targets += 1
            unmatched_targets.append(source_for_target.get(target, target))

        result["matched"] = len(library_for_target)
        result["unmatched"] = len(unmatched_targets)
        result["ambiguous"] = ambiguous_targets
        if not library_for_target:
            if ambiguous_targets and ambiguous_targets == len(unmatched_targets):
                reason = "变化路径匹配多个媒体库，无法唯一定位"
            elif ambiguous_targets:
                reason = "部分变化路径匹配多个媒体库，其余路径未匹配任何媒体库"
            else:
                reason = "变化路径未匹配任何媒体库"
            if ambiguous_targets:
                result["fallback"] = reason
                result["scope"] = "skipped"
                result["skipped"] = True
                logger.warning(
                    "[%s] %s，已安全跳过；歧义目标禁止回退全局刷新",
                    self.display_name, reason,
                )
                return result
            return self._finish_unlocatable_refresh(
                result,
                reason,
                allow_global=not allowed_ids,
                permit_global_fallback=permit_global_fallback,
            )

        targets_by_library: dict[str, list[tuple[str, str]]] = {}
        for target, (library_id, library_location) in library_for_target.items():
            targets_by_library.setdefault(library_id, []).append(
                (target, library_location)
            )

        item_targets: dict[str, dict[str, Any]] = {}
        folder_targets: dict[str, dict[str, Any]] = {}
        library_targets: set[str] = set()
        failed_item_listings: set[str] = set()
        oversized_item_listings: set[str] = set()
        dense_target_libraries: set[str] = set()

        for library_id, library_targets_for_id in targets_by_library.items():
            if len(library_targets_for_id) > _MAX_PRECISE_REFRESH_TARGETS_PER_LIBRARY:
                dense_target_libraries.add(library_id)
                try:
                    root_rows = self._library_root_items_with_paths(
                        library_id,
                        locations=tuple(dict.fromkeys(
                            location for _target, location in library_targets_for_id
                        )),
                    )
                except Exception:
                    root_rows = []
                root_items = {
                    self._media_path_key(item.get("Path")): item
                    for item in root_rows
                    if self._media_path_key(item.get("Path"))
                }
                for _target, library_location in library_targets_for_id:
                    root_item = root_items.get(self._media_path_key(library_location))
                    root_id = str(
                        root_item.get("Id") if isinstance(root_item, dict) else ""
                    ).strip()
                    if root_id:
                        folder_targets[root_id] = {
                            "library_id": library_id,
                            "path": library_location,
                        }
                    else:
                        library_targets.add(library_id)
                continue
            try:
                items = self._library_items_with_paths(library_id)
            except MediaLibraryEnumerationTooLarge:
                logger.info(
                    "[%s] 媒体库 %s 超过精准枚举上限，改为定位物理根目录",
                    self.display_name, library_id,
                )
                oversized_item_listings.add(library_id)
                try:
                    root_rows = self._library_root_items_with_paths(
                        library_id,
                        locations=tuple(dict.fromkeys(
                            location for _target, location in library_targets_for_id
                        )),
                    )
                except Exception:
                    root_rows = []
                root_items = {
                    self._media_path_key(item.get("Path")): item
                    for item in root_rows
                    if self._media_path_key(item.get("Path"))
                }
                for _target, library_location in library_targets_for_id:
                    root_item = root_items.get(self._media_path_key(library_location))
                    root_id = str(
                        root_item.get("Id") if isinstance(root_item, dict) else ""
                    ).strip()
                    if root_id:
                        folder_targets[root_id] = {
                            "library_id": library_id,
                            "path": library_location,
                        }
                    else:
                        library_targets.add(library_id)
                continue
            except Exception as exc:
                logger.warning(
                    "[%s] 读取媒体库 %s 条目失败，将保留刷新请求重试: %s",
                    self.display_name, library_id, exc,
                )
                failed_item_listings.add(library_id)
                continue

            root_items: dict[str, dict[str, Any]] = {}
            for item in items:
                if str(item.get("Type") or "").strip().casefold() != "folder":
                    continue
                item_path = self._normalize_media_path(item.get("Path"))
                item_id = str(item.get("Id") or "").strip()
                if item_path and item_id:
                    root_items[self._media_path_key(item_path)] = item

            for target, library_location in library_targets_for_id:
                matched_item = self._preferred_item_for_path(items, target)
                item_id = str(
                    matched_item.get("Id") if isinstance(matched_item, dict) else ""
                ).strip()
                if not item_id:
                    root_item = root_items.get(self._media_path_key(library_location))
                    root_id = str(
                        root_item.get("Id") if isinstance(root_item, dict) else ""
                    ).strip()
                    if root_id:
                        folder_targets[root_id] = {
                            "library_id": library_id,
                            "path": library_location,
                        }
                    else:
                        library_targets.add(library_id)
                    continue
                item_path = self._normalize_media_path(matched_item.get("Path"))
                item_type = str(matched_item.get("Type") or "").strip().casefold()
                if (
                    item_type == "folder"
                    and self._media_path_key(item_path)
                    == self._media_path_key(library_location)
                ):
                    folder_targets[item_id] = {
                        "library_id": library_id,
                        "path": library_location,
                    }
                else:
                    entry = item_targets.setdefault(item_id, {
                        "library_id": library_id,
                        "paths": [],
                        "type": item_type or "item",
                    })
                    entry["paths"] = _dedupe_texts([*entry["paths"], target])

        # 虚拟库扫描覆盖该库内所有更窄目标；物理根 Folder 扫描覆盖同根下条目。
        if library_targets:
            item_targets = {
                item_id: entry for item_id, entry in item_targets.items()
                if str(entry.get("library_id") or "") not in library_targets
            }
            folder_targets = {
                item_id: entry for item_id, entry in folder_targets.items()
                if str(entry.get("library_id") or "") not in library_targets
            }
        for folder_id, folder_entry in list(folder_targets.items()):
            root_path = str(folder_entry.get("path") or "")
            library_id = str(folder_entry.get("library_id") or "")
            for item_id, item_entry in list(item_targets.items()):
                if str(item_entry.get("library_id") or "") != library_id:
                    continue
                changed = [str(path) for path in item_entry.get("paths") or []]
                if changed and all(
                    media_server_path_is_within(path, root_path) for path in changed
                ):
                    item_targets.pop(item_id, None)

        item_ids = list(item_targets)
        folder_ids = [item for item in folder_targets if item not in item_targets]
        library_ids = [
            item for item in sorted(library_targets)
            if item not in item_targets and item not in folder_targets
        ]
        broad_ids = [*folder_ids, *library_ids]
        if broad_ids and not allow_library_fallback:
            result["fallback"] = (
                f"{len(broad_ids)} 个目标只能定位到物理根或媒体库，严格模式已安全跳过"
            )
            result["scope"] = "skipped"
            result["skipped"] = True
            return result

        requested_ids = [*item_ids, *folder_ids, *library_ids]
        deduplicated_ids = [item for item in requested_ids if item in skipped_ids]
        result["deduplicated"] = len(deduplicated_ids)
        item_ids = [item for item in item_ids if item not in skipped_ids]
        folder_ids = [item for item in folder_ids if item not in skipped_ids]
        library_ids = [item for item in library_ids if item not in skipped_ids]

        if not item_ids and not folder_ids and not library_ids:
            if failed_item_listings:
                result["fallback"] = (
                    f"{len(failed_item_listings)} 个媒体库条目读取失败，刷新请求将重试"
                )
                result["scope"] = "skipped"
                result["skipped"] = True
                result["retryable"] = True
                return result
            if deduplicated_ids:
                result["fallback"] = f"{len(deduplicated_ids)} 个刷新目标处于去重窗口"
                result["scope"] = "deduplicated"
                result["skipped"] = True
                result["ok"] = not unmatched_targets
                return result
            return self._finish_unlocatable_refresh(
                result,
                "无法定位任何刷新目标",
                allow_global=not allowed_ids,
                permit_global_fallback=permit_global_fallback,
            )

        ordered_ids = [*item_ids, *folder_ids, *library_ids]
        outcomes: list[bool] = []
        succeeded_ids: list[str] = []
        for item_id in ordered_ids:
            ok = bool(self.refresh_library(item_id))
            outcomes.append(ok)
            if ok:
                succeeded_ids.append(item_id)

        result["items"] = item_ids
        result["folders"] = folder_ids
        result["libraries"] = library_ids
        result["succeeded_target_ids"] = succeeded_ids
        result["endpoints"] = [f"/Items/{item}/Refresh" for item in ordered_ids]
        active_scopes = sum(bool(group) for group in (item_ids, folder_ids, library_ids))
        if active_scopes > 1:
            result["scope"] = "mixed"
        elif item_ids:
            result["scope"] = "item"
        elif folder_ids:
            result["scope"] = "folder"
        else:
            result["scope"] = "library"

        reasons: list[str] = []
        if folder_ids:
            reasons.append("新作品已按对应物理根目录扫描")
        if library_ids:
            reasons.append("部分目标未找到物理根 Item，已按所属媒体库扫描")
        if oversized_item_listings:
            reasons.append(f"{len(oversized_item_listings)} 个大库已收敛为根目录或媒体库刷新")
        if dense_target_libraries:
            reasons.append(
                f"{len(dense_target_libraries)} 个多变更媒体库已合并为根目录扫描"
            )
        if failed_item_listings:
            reasons.append(f"{len(failed_item_listings)} 个媒体库条目读取失败，将重试")
        if deduplicated_ids:
            reasons.append(f"{len(deduplicated_ids)} 个目标已在去重窗口内跳过")
        if ambiguous_targets:
            reasons.append(f"{ambiguous_targets} 个变化路径匹配多个媒体库，已安全跳过")
        unmatched_without_ambiguity = len(unmatched_targets) - ambiguous_targets
        if unmatched_without_ambiguity:
            reasons.append(
                f"{unmatched_without_ambiguity} 个变化路径未匹配媒体库，已安全跳过"
            )
        failed_endpoints = len(outcomes) - len(succeeded_ids)
        if failed_endpoints:
            reasons.append(f"{failed_endpoints} 个刷新请求失败，将重试")
        result["fallback"] = "；".join(reasons)
        result["retryable"] = bool(failed_item_listings or failed_endpoints)
        result["ok"] = (
            bool(outcomes) and all(outcomes)
            and not unmatched_targets and not failed_item_listings
        )
        return result
