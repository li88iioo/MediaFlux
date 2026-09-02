"""Jellyfin 客户端（适配 Jellyfin 12）。

使用 API Key 鉴权。看板查询优先采用用户视图、Items/Counts 与活动日志中的
真实播放事件；旧版服务不支持活动日志时才降级到 DatePlayed 用户数据查询。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
from datetime import datetime
from urllib.parse import quote

from app.clients.base import (
    DashboardData,
    Library,
    MediaItem,
    MediaServerClient,
    close_media_server_client,
    normalize_explicit_media_user_id,
    normalize_playback_progress,
    runtime_ticks_to_minutes,
)
from app.logger import get_logger, log_throttled
from app.modules.media_server_path_mapping import MediaServerPathMapping

logger = get_logger(__name__)


class JellyfinClient(MediaServerClient):
    display_name = "Jellyfin"
    _PLAYABLE_ITEM_TYPES = "Movie,Episode,Audio,MusicVideo,Book,Video"
    _LIBRARY_TYPES = {
        "tvshows": "Series",
        "movies": "Movie",
        "music": "Audio",
        "musicvideos": "MusicVideo",
        "books": "Book",
        "homevideos": "Video",
    }
    _LIBRARY_WEB_ROUTES = {
        "tvshows": "tv",
        "movies": "movies",
        "music": "music",
        "books": "books",
        "musicvideos": "musicvideos",
        "homevideos": "homevideos",
        "mixed": "mixed",
    }

    def __init__(
        self,
        url: str,
        token: str,
        timeout: int = 10,
        *,
        path_mappings: tuple[MediaServerPathMapping, ...] = (),
        allow_global_refresh_fallback: bool = False,
    ):
        super().__init__(
            url,
            token,
            timeout,
            path_mappings=path_mappings,
            allow_global_refresh_fallback=allow_global_refresh_fallback,
        )
        self._cached_user_id = ""

    def _isolated_part(self, method_name: str, user_id: str):
        """在线程中使用独立 Session 读取一个看板分区。"""
        client = JellyfinClient(
            self.url,
            self.token,
            self.timeout,
            path_mappings=self.path_mappings,
            allow_global_refresh_fallback=self.allow_global_refresh_fallback,
        )
        try:
            client._cached_user_id = user_id
            return getattr(client, method_name)()
        finally:
            close_media_server_client(client)

    def get_dashboard(self) -> DashboardData:
        """并行读取互不依赖的 Jellyfin 看板分区。"""
        data = DashboardData(server_name=self.display_name)
        try:
            data.server_name, data.server_product, data.server_version = self._server_identity()
            user_id = self._user_id()
            data.online = True
        except Exception as exc:
            data.error = str(exc)
            return data

        parts = {
            "libraries": ("_libraries", []),
            "recent_added": ("_recent_added", []),
            "recent_played": ("_recent_played", []),
            "total_items": ("_total_items", 0),
            "media_counts": ("_media_counts", {}),
            "total_plays": ("_total_plays", 0),
        }
        with ThreadPoolExecutor(max_workers=len(parts), thread_name_prefix="jellyfin-dashboard") as pool:
            futures = {
                name: pool.submit(self._isolated_part, method_name, user_id)
                for name, (method_name, _default) in parts.items()
            }
            failure_types: list[str] = []
            for name, (method_name, default) in parts.items():
                try:
                    result = futures[name].result()
                    if name == "media_counts":
                        data.movie_count = int(result.get("movie_count", 0) or 0)
                        data.series_count = int(result.get("series_count", 0) or 0)
                        data.episode_count = int(result.get("episode_count", 0) or 0)
                    else:
                        setattr(data, name, result)
                except Exception as exc:
                    if name == "media_counts":
                        data.movie_count = 0
                        data.series_count = 0
                        data.episode_count = 0
                    else:
                        setattr(data, name, default)
                    data.partial_errors.append(name)
                    failure_types.append(f"{name}:{type(exc).__name__}")
        if failure_types:
            log_throttled(
                logger,
                logging.WARNING,
                f"dashboard:jellyfin:{','.join(failure_types)}",
                "Jellyfin 看板部分读取失败 parts=%s",
                ",".join(failure_types),
            )
        return data

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f'MediaBrowser Token="{self.token}"',
            "Accept": "application/json",
        }

    def _request(
        self,
        path: str,
        params: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> object:
        return super()._request(path, params, timeout=timeout)

    def _server_name(self) -> str:
        return self._server_identity()[0]

    def _server_identity(self) -> tuple[str, str, str]:
        info = self._request("/System/Info/Public")
        return (
            str(info.get("ServerName") or self.display_name),
            str(info.get("ProductName") or "Jellyfin"),
            str(info.get("Version") or info.get("ServerVersion") or ""),
        )

    def _user_id(self, *, deadline_at: float | None = None) -> str:
        if self._cached_user_id:
            return self._cached_user_id
        if deadline_at is None:
            users = self._request("/Users")
        else:
            users = self._request("/Users", timeout=self._remaining_timeout(deadline_at))
        if not users:
            raise RuntimeError("无可用 Jellyfin 用户")
        for user in users:
            if user.get("Policy", {}).get("IsAdministrator"):
                self._cached_user_id = user["Id"]
                return self._cached_user_id
        self._cached_user_id = users[0]["Id"]
        return self._cached_user_id

    def _image_url(self, item_id: str, image_tags: dict | None = None) -> str:
        """仅为 Jellyfin 已声明存在的主图生成代理地址。"""
        if not item_id or not isinstance(image_tags, dict):
            return ""
        image_tag = str(image_tags.get("Primary") or "").strip()
        if not image_tag:
            return ""
        return f"/media-image/jellyfin/{item_id}?tag={quote(image_tag, safe='')}"

    def _web_url(self, item_id: str) -> str:
        return f"{self.url}/web/index.html#!/details?id={item_id}" if item_id else self.url

    def _library_web_url(self, item_id: str, collection_type: str) -> str:
        if not item_id:
            return self.url
        normalized_type = (collection_type or "mixed").strip().lower()
        route = self._LIBRARY_WEB_ROUTES.get(normalized_type)
        if not route:
            return self._web_url(item_id)
        url = f"{self.url}/web/index.html#/{route}?topParentId={item_id}"
        if normalized_type != "homevideos":
            url += f"&collectionType={normalized_type}"
        return url

    def _library_count(self, user_id: str, item: dict) -> int:
        include_type = self._LIBRARY_TYPES.get(item.get("CollectionType", ""))
        params = {
            "ParentId": item.get("Id", ""),
            "Recursive": "true",
            "Limit": 0,
            "EnableImages": "false",
        }
        if include_type:
            params["IncludeItemTypes"] = include_type
        data = self._request(f"/Users/{user_id}/Items", params=params)
        return data.get("TotalRecordCount", 0)

    def _libraries(self) -> list[Library]:
        user_id = self._user_id()
        data = self._request(f"/Users/{user_id}/Views")
        libraries: list[Library] = []
        for item in data.get("Items", []):
            collection_type = item.get("CollectionType", "")
            if collection_type in {"playlists", "boxsets", "folders"}:
                continue
            item_id = item.get("Id", "")
            libraries.append(
                Library(
                    id=item_id,
                    name=item.get("Name", ""),
                    item_type=collection_type or "mixed",
                    count=self._library_count(user_id, item),
                    primary_image=self._image_url(item_id, item.get("ImageTags")),
                    web_url=self._library_web_url(item_id, collection_type),
                )
            )
        return libraries

    @staticmethod
    def _local_datetime(value: object) -> str:
        """把 Jellyfin 的 UTC 时间转换为服务所在时区，供页面直接取日期显示。"""
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return text
            return parsed.astimezone().isoformat(timespec="seconds")
        except (TypeError, ValueError):
            return text

    def _media_item(self, item: dict, *, played: bool = False) -> MediaItem:
        item_id = str(item.get("Id") or "")
        item_type = str(item.get("Type") or "")
        raw_series_id = str(item.get("SeriesId") or "")
        is_episode = item_type.casefold() == "episode"
        series_id = item_id if item_type.casefold() == "series" else raw_series_id
        series_image_tag = str(item.get("SeriesPrimaryImageTag") or "").strip()
        if is_episode and series_id and series_image_tag:
            image_id = series_id
            image_tags = {"Primary": series_image_tag}
        else:
            image_id = item_id
            image_tags = item.get("ImageTags")
        raw_user_data = item.get("UserData")
        user_data = raw_user_data if isinstance(raw_user_data, dict) else {}
        return MediaItem(
            id=item_id,
            name=item.get("Name", ""),
            type=item.get("Type", ""),
            year=str(item.get("ProductionYear", "") or ""),
            runtime=runtime_ticks_to_minutes(item.get("RunTimeTicks")),
            date_added=item.get("DateCreated", ""),
            primary_image=self._image_url(image_id, image_tags),
            overview=(item.get("Overview") or "")[:160],
            web_url=self._web_url(item_id),
            series_name=item.get("SeriesName", ""),
            series_id=series_id,
            series_web_url=self._web_url(series_id) if series_id else "",
            season_number=item.get("ParentIndexNumber"),
            episode_number=item.get("IndexNumber"),
            last_played=(
                self._local_datetime(user_data.get("LastPlayedDate")) if played else ""
            ),
            progress=(
                100.0
                if bool(user_data.get("Played"))
                else normalize_playback_progress(user_data.get("PlayedPercentage"))
            ),
        )

    def _recent_added(self, limit: int = 8) -> list[MediaItem]:
        user_id = self._user_id()
        normalized_limit = max(1, min(int(limit or 8), 200))
        data = self._request(
            f"/Users/{user_id}/Items",
            params={
                "Recursive": "true",
                "Limit": max(30, normalized_limit * 3),
                "IncludeItemTypes": "Movie,Series,Episode",
                "Fields": (
                    "DateCreated,Overview,SeriesId,SeriesName,IndexNumber,"
                    "ParentIndexNumber,ImageTags,SeriesPrimaryImageTag,"
                    "ProductionYear,UserData"
                ),
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "EnableTotalRecordCount": "false",
            },
        )
        raw_items = (data.get("Items") or []) if isinstance(data, dict) else (data or [])
        raw_items = sorted(
            raw_items,
            key=lambda raw: str(raw.get("DateCreated") or ""),
            reverse=True,
        )
        items: list[MediaItem] = []
        seen: set[str] = set()
        for raw in raw_items:
            item_id = str(raw.get("Id") or "")
            if str(raw.get("Type") or "").casefold() == "episode" and raw.get("SeriesId"):
                dedupe_key = f"episode-series:{raw['SeriesId']}"
            else:
                dedupe_key = item_id
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            items.append(self._media_item(raw))
            if len(items) >= normalized_limit:
                break
        return items

    def search_media(self, query: str, limit: int = 12) -> list[MediaItem]:
        user_id = self._user_id()
        normalized_limit = max(1, min(int(limit or 12), 50))
        data = self._request(
            f"/Users/{user_id}/Items",
            params={
                "SearchTerm": str(query or "").strip(),
                "Recursive": "true",
                "Limit": normalized_limit,
                "IncludeItemTypes": "Movie,Series,Episode",
                "Fields": (
                    "DateCreated,Overview,SeriesId,SeriesName,IndexNumber,"
                    "ParentIndexNumber,ImageTags,SeriesPrimaryImageTag,"
                    "ProductionYear,RunTimeTicks,UserData"
                ),
                "SortBy": "SortName",
                "SortOrder": "Ascending",
            },
        )
        if isinstance(data, list):
            raw_items = data
        elif isinstance(data, dict):
            raw_items = data.get("Items") or []
        else:
            raise ValueError("媒体服务器响应格式无效")
        if not isinstance(raw_items, list):
            raise ValueError("媒体服务器条目格式无效")
        return [self._media_item(item) for item in raw_items if isinstance(item, dict)]

    def _recent_played_from_activity(
        self, user_id: str, normalized_limit: int,
    ) -> list[MediaItem]:
        """从 Jellyfin 活动日志读取真实播放事件，排除批量“标记已观看”。"""
        data = self._request(
            "/System/ActivityLog/Entries",
            params={"Limit": max(200, normalized_limit * 20)},
        )
        if not isinstance(data, dict) or not isinstance(data.get("Items"), list):
            raise ValueError("Jellyfin 活动日志响应格式无效")

        activities = sorted(
            (
                item for item in data["Items"]
                if isinstance(item, dict)
                and str(item.get("UserId") or "") == user_id
                and str(item.get("Type") or "") in {
                    "VideoPlayback", "VideoPlaybackStopped",
                }
                and str(item.get("ItemId") or "")
            ),
            key=lambda item: str(item.get("Date") or ""),
            reverse=True,
        )
        activity_dates: dict[str, str] = {}
        for activity in activities:
            item_id = str(activity.get("ItemId") or "")
            if item_id in activity_dates:
                continue
            activity_dates[item_id] = str(activity.get("Date") or "")
            if len(activity_dates) >= normalized_limit:
                break
        if not activity_dates:
            return []

        details = self._request(
            "/Items",
            params={
                "UserId": user_id,
                "Ids": ",".join(activity_dates),
                "Fields": (
                    "DateCreated,Overview,SeriesId,SeriesName,IndexNumber,"
                    "ParentIndexNumber,ImageTags,SeriesPrimaryImageTag,"
                    "ProductionYear,UserData"
                ),
                "EnableUserData": "true",
                "EnableTotalRecordCount": "false",
            },
        )
        raw_items = (
            details.get("Items") or []
            if isinstance(details, dict)
            else (details or [])
        )
        if not isinstance(raw_items, list):
            raise ValueError("Jellyfin 播放条目响应格式无效")
        by_id = {
            str(item.get("Id") or ""): item
            for item in raw_items if isinstance(item, dict) and item.get("Id")
        }
        items: list[MediaItem] = []
        for item_id, played_at in activity_dates.items():
            raw = by_id.get(item_id)
            if raw is None:
                continue
            media = self._media_item(raw, played=True)
            media.last_played = self._local_datetime(played_at) or media.last_played
            items.append(media)
        return items

    def _recent_played_from_user_data(
        self, user_id: str, normalized_limit: int,
    ) -> list[MediaItem]:
        """旧版 Jellyfin 降级路径：按用户数据中的 DatePlayed 排序。"""
        data = self._request(
            "/Items",
            params={
                "UserId": user_id,
                "Recursive": "true",
                "Limit": max(36, normalized_limit * 3),
                "IncludeItemTypes": "Movie,Episode",
                "MediaTypes": "Video",
                "Fields": (
                    "DateCreated,Overview,SeriesId,SeriesName,IndexNumber,"
                    "ParentIndexNumber,ImageTags,SeriesPrimaryImageTag,"
                    "ProductionYear,UserData"
                ),
                "SortBy": "DatePlayed",
                "SortOrder": "Descending",
                "EnableUserData": "true",
                "EnableTotalRecordCount": "false",
            },
        )
        raw_items = (data.get("Items") or []) if isinstance(data, dict) else (data or [])
        raw_items = sorted(
            (
                item
                for item in raw_items
                if (item.get("UserData") or {}).get("LastPlayedDate")
            ),
            key=lambda item: str((item.get("UserData") or {}).get("LastPlayedDate") or ""),
            reverse=True,
        )
        items: list[MediaItem] = []
        seen: set[str] = set()
        for raw in raw_items:
            item_id = str(raw.get("Id") or "")
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            items.append(self._media_item(raw, played=True))
            if len(items) >= normalized_limit:
                break
        return items

    def _recent_played(self, limit: int = 12) -> list[MediaItem]:
        """读取真实播放历史；不可用时兼容旧版 DatePlayed 查询。"""
        user_id = self._user_id()
        normalized_limit = max(1, min(int(limit or 12), 50))
        try:
            return self._recent_played_from_activity(user_id, normalized_limit)
        except Exception as exc:
            logger.debug(
                "[Jellyfin] 活动日志不可用，降级 DatePlayed 查询 type=%s",
                type(exc).__name__,
            )
            return self._recent_played_from_user_data(user_id, normalized_limit)

    def _total_items(self) -> int:
        user_id = self._user_id()
        data = self._request(
            f"/Users/{user_id}/Items",
            params={
                "Recursive": "true",
                "Limit": 0,
                "EnableImages": "false",
                "IncludeItemTypes": self._PLAYABLE_ITEM_TYPES,
            },
        )
        return int(data.get("TotalRecordCount", 0) or 0)

    def get_media_counts(self) -> dict[str, int]:
        """只读取 Jellyfin 统计接口，避免为一个总数加载整张看板。"""
        counts = self._media_counts()
        return {
            "total_items": max(0, int(self._total_items() or 0)),
            "movie_count": max(0, int(counts.get("movie_count", 0) or 0)),
            "series_count": max(0, int(counts.get("series_count", 0) or 0)),
            "episode_count": max(0, int(counts.get("episode_count", 0) or 0)),
        }

    def _media_counts(self) -> dict[str, int]:
        data = self._request("/Items/Counts", params={"userId": self._user_id()})
        return {
            "movie_count": int(data.get("MovieCount", 0) or 0),
            "series_count": int(data.get("SeriesCount", 0) or 0),
            "episode_count": int(data.get("EpisodeCount", 0) or 0),
        }

    def continue_watching(self, user_id: str, *, limit: int = 12) -> list[MediaItem]:
        selected = normalize_explicit_media_user_id(user_id)
        normalized_limit = max(1, min(int(limit or 12), 20))
        data = self._request(
            f"/Users/{selected}/Items/Resume",
            params={
                "Limit": normalized_limit,
                "MediaTypes": "Video",
                "Fields": (
                    "DateCreated,Overview,SeriesId,SeriesName,IndexNumber,"
                    "ParentIndexNumber,ImageTags,ProductionYear,RunTimeTicks,UserData"
                ),
            },
        )
        items = data.get("Items", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            raise ValueError("媒体服务器继续观看响应无效")
        return [self._media_item(item) for item in items if isinstance(item, dict)]

    def _total_plays(self) -> int:
        user_id = self._user_id()
        data = self._request(
            f"/Users/{user_id}/Items/Resume",
            params={"Limit": 0, "MediaTypes": "Video", "EnableImages": "false"},
        )
        return int(data.get("TotalRecordCount", 0) or 0)

    def refresh_library(self, library_id: str) -> bool:
        """轻量刷新 Item/Folder/媒体库；自动流程不强制重抓全部元数据。"""
        try:
            resp = self._session.post(
                f"{self.url}/Items/{library_id}/Refresh",
                headers=self._headers(),
                params={
                    "metadataRefreshMode": "Default",
                    "imageRefreshMode": "None",
                    "replaceAllMetadata": "false",
                    "replaceAllImages": "false",
                    "regenerateTrickplay": "false",
                },
                timeout=15,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Jellyfin 刷新目标失败 target=%s type=%s", library_id, type(exc).__name__)
            return False

    @staticmethod
    def _normalize_path(path: str) -> str:
        return str(path or "").replace("\\", "/").rstrip("/").lower()

    def refresh_for_path(self, media_path: str) -> bool:
        """兼容单路径调用；沿用安全的批量精准刷新策略。"""
        return bool(self.refresh_for_paths([media_path]).get("ok"))

    def refresh_all(self) -> bool:
        """显式全局刷新兼容入口；自动整理链路不会调用该接口。"""
        try:
            resp = self._session.post(
                f"{self.url}/Library/Refresh",
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Jellyfin 全局刷新失败 type=%s", type(exc).__name__)
            return False
