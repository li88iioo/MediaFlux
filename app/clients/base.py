"""媒体服务器客户端基类。

统一抽象 Emby / Jellyfin 的看板数据获取，屏蔽两者 API 差异。
子类实现 _request 及鉴权细节。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import time
from typing import Any, Optional

import requests

from app.logger import get_logger

logger = get_logger(__name__)

_RUNTIME_TICKS_PER_MINUTE = 600_000_000
_MAX_RUNTIME_MINUTES = 525_600


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
    total_plays: int = 0
    online: bool = False
    error: str = ""
    server_product: str = ""
    server_version: str = ""
    partial_errors: list[str] = field(default_factory=list)


class MediaServerClient:
    """媒体服务器客户端基类。"""

    display_name = "MediaServer"

    def __init__(self, url: str, token: str, timeout: int = 10):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session = requests.Session()

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
            logger.error(f"[{self.display_name}] 请求失败 {path}: {e}")
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
        for name, func, default in parts:
            try:
                setattr(data, name, func())
            except Exception as exc:
                logger.warning(f"[{self.display_name}] {func.__name__} 失败: {exc}")
                setattr(data, name, default)
                data.partial_errors.append(name)
        return data

    def _safe(self, func, default):
        try:
            return func()
        except Exception as e:
            logger.warning(f"[{self.display_name}] {func.__name__} 失败: {e}")
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

    def has_tmdb_media(self, tmdb_id: str, media_type: str) -> bool:
        """按 ProviderIds.Tmdb 精确判断电影或剧集是否已存在。"""
        normalized_id = self._provider_tmdb_id({"Tmdb": tmdb_id})
        normalized_type = "Series" if str(media_type).strip().lower() == "tv" else "Movie"
        if not normalized_id:
            raise ValueError("TMDB ID 无效")
        data = self._request(
            f"/Users/{self._user_id()}/Items",
            params={
                "Recursive": "true",
                "Limit": 20,
                "IncludeItemTypes": normalized_type,
                "Fields": "ProviderIds",
                "AnyProviderIdEquals": f"Tmdb.{normalized_id}",
            },
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
        return str(path or "").replace("\\", "/").rstrip("/").lower()

    def _library_items_with_paths(self, library_id: str) -> list[dict[str, Any]]:
        """列出一个媒体库内可定位的容器 Item 及其路径。"""
        data = self._request(
            "/Items",
            params={
                "ParentId": str(library_id),
                "Recursive": "true",
                "IncludeItemTypes": "Series,Season,Movie,Folder,BoxSet",
                "Fields": "Path",
                "Limit": 20000,
            },
        )
        items = data.get("Items") if isinstance(data, dict) else data
        return [item for item in items or [] if isinstance(item, dict)]

    def _deepest_item_for_path(
        self, items: list[dict[str, Any]], target: str,
    ) -> str:
        """返回覆盖目标路径的最深已存在 Item；找不到时返回空串。"""
        best_id = ""
        best_length = -1
        for item in items:
            item_path = self._normalize_media_path(item.get("Path"))
            if not item_path:
                continue
            if target == item_path or target.startswith(f"{item_path}/"):
                if len(item_path) > best_length:
                    best_length = len(item_path)
                    best_id = str(item.get("Id") or "").strip()
        return best_id

    def refresh_for_paths(self, paths: list[str]) -> dict[str, Any]:
        """按本轮真实变化路径刷新最深父 Item。

        只有完全无法定位时才允许全局刷新，且必须记录明确降级原因。
        同一 Item 每轮只刷新一次，避免同一剧集的多集触发多次刷新。
        """
        targets = [
            self._normalize_media_path(item) for item in (paths or []) if str(item or "")
        ]
        targets = [item for item in dict.fromkeys(targets) if item]
        result: dict[str, Any] = {
            "ok": False, "items": [], "libraries": [],
            "fallback": "", "requested": len(targets), "skipped": False,
        }
        if not targets:
            result["fallback"] = "本轮没有可定位的变化路径"
            result["skipped"] = True
            result["ok"] = True
            return result

        try:
            folders = self.list_virtual_folders()
        except Exception as exc:
            logger.warning(
                "[%s] 读取媒体库目录失败，回退全局刷新: %s", self.display_name, exc
            )
            result["fallback"] = "媒体库目录读取失败"
            result["ok"] = self.refresh_all()
            return result

        library_for_target: dict[str, str] = {}
        for target in targets:
            best_library_id = ""
            best_location_length = -1
            for folder in folders:
                library_id = str(folder.get("id") or "").strip()
                if not library_id:
                    continue
                locations = [
                    self._normalize_media_path(item)
                    for item in folder.get("locations", [])
                ]
                for location in locations:
                    if not location:
                        continue
                    if target != location and not target.startswith(f"{location}/"):
                        continue
                    if len(location) > best_location_length:
                        best_library_id = library_id
                        best_location_length = len(location)
            if best_library_id:
                library_for_target[target] = best_library_id

        if not library_for_target:
            logger.info(
                "[%s] 变化路径未命中任何媒体库，回退全局刷新", self.display_name
            )
            result["fallback"] = "变化路径未匹配任何媒体库"
            result["ok"] = self.refresh_all()
            return result

        items_by_library: dict[str, list[dict[str, Any]]] = {}
        item_ids: list[str] = []
        library_fallbacks: list[str] = []
        for target, library_id in library_for_target.items():
            if library_id not in items_by_library:
                try:
                    items_by_library[library_id] = self._library_items_with_paths(library_id)
                except Exception as exc:
                    logger.warning(
                        "[%s] 读取媒体库 %s 条目失败，降级为媒体库级刷新: %s",
                        self.display_name, library_id, exc,
                    )
                    items_by_library[library_id] = []
            item_id = self._deepest_item_for_path(items_by_library[library_id], target)
            if item_id:
                item_ids.append(item_id)
            else:
                library_fallbacks.append(library_id)

        refreshed = list(dict.fromkeys(item_ids))
        libraries = [
            item for item in dict.fromkeys(library_fallbacks) if item not in refreshed
        ]
        if not refreshed and not libraries:
            result["fallback"] = "无法定位任何刷新目标"
            result["ok"] = self.refresh_all()
            return result

        outcomes = [self.refresh_library(item) for item in (*refreshed, *libraries)]
        result["items"] = refreshed
        result["libraries"] = libraries
        result["ok"] = bool(outcomes) and all(outcomes)
        if libraries:
            result["fallback"] = "部分变化路径无法定位到具体 Item，已按媒体库刷新"
        return result
