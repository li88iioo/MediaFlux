"""Emby / Jellyfin 10.x 传统 MediaBrowser API 兼容客户端。

同时兼容 X-Emby-Token 与 MediaBrowser Token 鉴权，并根据系统信息区分
Emby 和 Jellyfin 10.x 的页面路由。
"""
from __future__ import annotations


from app.clients.base import (
    DashboardData,
    Library,
    MediaItem,
    MediaServerClient,
    normalize_explicit_media_user_id,
    normalize_playback_progress,
    runtime_ticks_to_minutes,
)
from app.logger import get_logger
from app.modules.media_server_path_mapping import MediaServerPathMapping

logger = get_logger(__name__)


class EmbyClient(MediaServerClient):
    display_name = "Emby / Jellyfin 10.x"
    _LIBRARY_TYPES = {
        "tvshows": "Series",
        "movies": "Movie",
        "music": "Audio",
        "musicvideos": "MusicVideo",
        "books": "Book",
        "homevideos": "Video",
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
        self._cached_server_info: dict | None = None
        self.product_kind = "unknown"

    def _headers(self) -> dict[str, str]:
        return {
            "X-Emby-Token": self.token,
            "Authorization": f'MediaBrowser Token="{self.token}"',
            "Accept": "application/json",
        }

    def _server_info(self) -> dict:
        if self._cached_server_info is None:
            info = self._request("/System/Info/Public")
            if not isinstance(info, dict):
                raise RuntimeError("兼容节点响应格式异常")
            self._cached_server_info = info
        return self._cached_server_info

    def _server_identity(self) -> tuple[str, str, str]:
        info = self._server_info()
        product_text = str(info.get("ProductName") or info.get("Product") or "").strip()
        fingerprint = " ".join((product_text, str(info.get("ServerName") or ""))).lower()
        self.product_kind = "jellyfin" if "jellyfin" in fingerprint else "emby"
        product = "Jellyfin" if self.product_kind == "jellyfin" else "Emby"
        return (
            str(info.get("ServerName") or info.get("Name") or product),
            product,
            str(info.get("Version") or info.get("ServerVersion") or ""),
        )

    def _server_name(self) -> str:
        return self._server_identity()[0]

    def _user_id(self, *, deadline_at: float | None = None) -> str:
        if self._cached_user_id:
            return self._cached_user_id
        users = None
        for path in ("/Users", "/Users/Public"):
            try:
                if deadline_at is None:
                    candidate = self._request(path)
                else:
                    candidate = self._request(
                        path, timeout=self._remaining_timeout(deadline_at)
                    )
            except Exception:
                continue
            if candidate:
                users = candidate
                break
        if not users:
            raise RuntimeError("无可用 Emby / Jellyfin 10.x 用户")
        # 取第一个管理员
        for u in users:
            if u.get("Policy", {}).get("IsAdministrator"):
                self._cached_user_id = u["Id"]
                return self._cached_user_id
        self._cached_user_id = users[0]["Id"]
        return self._cached_user_id

    def _image_url(self, item_id: str) -> str:
        return f"/media-image/emby/{item_id}" if item_id else ""

    def _web_url(self, item_id: str) -> str:
        if not item_id:
            return self.url
        route = "details" if self.product_kind == "jellyfin" else "item"
        return f"{self.url}/web/index.html#!/{route}?id={item_id}"

    def _prime_product_identity(self) -> None:
        if self.product_kind != "unknown":
            return
        try:
            self._server_identity()
        except Exception as exc:
            logger.debug(f"[兼容节点] 产品识别失败，使用 Emby 页面路由: {exc}")

    def recent_media(self, limit: int = 60) -> list[MediaItem]:
        self._prime_product_identity()
        return super().recent_media(limit)

    def list_virtual_folders(self) -> list[dict]:
        """兼容 Emby QueryResult 与 Jellyfin 10.x 裸数组媒体库接口。"""
        self._prime_product_identity()
        if self.product_kind == "jellyfin":
            return super().list_virtual_folders()
        try:
            data = self._request(
                "/Library/VirtualFolders/Query",
                params={"StartIndex": 0, "Limit": 1000},
            )
        except Exception as exc:
            logger.debug(
                "[Emby] VirtualFolders/Query 不可用，回退传统接口 type=%s",
                type(exc).__name__,
            )
            data = self._request("/Library/VirtualFolders")
        return self._normalize_virtual_folders(data)

    def _library_count(self, user_id: str, item: dict) -> int:
        collection_type = str(item.get("CollectionType") or "").lower()
        params = {
            "ParentId": item.get("Id", ""),
            "Recursive": "true",
            "Limit": 0,
            "EnableImages": "false",
        }
        include_type = self._LIBRARY_TYPES.get(collection_type)
        if include_type:
            params["IncludeItemTypes"] = include_type
        data = self._request(f"/Users/{user_id}/Items", params=params)
        return int(data.get("TotalRecordCount", 0) or 0)

    def _libraries(self) -> list[Library]:
        uid = self._user_id()
        data = self._request(f"/Users/{uid}/Views")
        libs: list[Library] = []
        for item in data.get("Items", []):
            collection_type = str(item.get("CollectionType") or "")
            if collection_type.lower() in {"playlists", "boxsets", "folders"}:
                continue
            item_id = str(item.get("Id") or "")
            libs.append(
                Library(
                    id=item_id,
                    name=item.get("Name", ""),
                    item_type=collection_type or "mixed",
                    count=self._library_count(uid, item),
                    primary_image=self._image_url(item_id),
                    web_url=self._web_url(item_id),
                )
            )
        return libs

    def _media_item(self, item: dict) -> MediaItem:
        item_id = str(item.get("Id") or "")
        series_id = str(item.get("SeriesId") or "")
        image_id = series_id if item.get("Type") == "Episode" and series_id else item_id
        raw_user_data = item.get("UserData")
        user_data = raw_user_data if isinstance(raw_user_data, dict) else {}
        return MediaItem(
            id=item_id,
            name=item.get("Name", ""),
            type=item.get("Type", ""),
            year=str(item.get("ProductionYear", "") or ""),
            runtime=runtime_ticks_to_minutes(item.get("RunTimeTicks")),
            date_added=item.get("DateCreated", ""),
            primary_image=self._image_url(image_id),
            overview=(item.get("Overview") or "")[:160],
            web_url=self._web_url(item_id),
            series_name=item.get("SeriesName", ""),
            season_number=item.get("ParentIndexNumber"),
            episode_number=item.get("IndexNumber"),
            last_played=user_data.get("LastPlayedDate", ""),
            progress=normalize_playback_progress(user_data.get("PlayedPercentage")),
        )

    def _recent_added(self, limit: int = 8) -> list[MediaItem]:
        uid = self._user_id()
        normalized_limit = max(1, min(int(limit or 8), 200))
        data = self._request(
            f"/Users/{uid}/Items",
            params={
                "Recursive": "true",
                "Limit": max(30, normalized_limit * 3),
                "IncludeItemTypes": "Movie,Series,Episode",
                "Fields": (
                    "DateCreated,Overview,SeriesId,SeriesName,IndexNumber,"
                    "ParentIndexNumber,ImageTags,ProductionYear,UserData"
                ),
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "EnableTotalRecordCount": "false",
            },
        )
        raw_items = data if isinstance(data, list) else (data.get("Items") or [])
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
        self._prime_product_identity()
        uid = self._user_id()
        normalized_limit = max(1, min(int(limit or 12), 50))
        data = self._request(
            f"/Users/{uid}/Items",
            params={
                "SearchTerm": str(query or "").strip(),
                "Recursive": "true",
                "Limit": normalized_limit,
                "IncludeItemTypes": "Movie,Series,Episode",
                "Fields": (
                    "DateCreated,Overview,SeriesId,SeriesName,IndexNumber,"
                    "ParentIndexNumber,ImageTags,ProductionYear,RunTimeTicks,UserData"
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

    def _total_items(self) -> int:
        uid = self._user_id()
        data = self._request(
            f"/Users/{uid}/Items",
            params={"Recursive": "true", "Limit": 0},
        )
        return int(data.get("TotalRecordCount", 0) or 0)

    def _recent_played(self) -> list[MediaItem]:
        uid = self._user_id()
        data = self._request(
            f"/Users/{uid}/Items/Resume",
            params={
                "Limit": 12,
                "MediaTypes": "Video",
                "Fields": (
                    "DateCreated,Overview,SeriesId,SeriesName,IndexNumber,"
                    "ParentIndexNumber,ImageTags,ProductionYear,UserData"
                ),
            },
        )
        return [self._media_item(item) for item in data.get("Items", [])]

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
        uid = self._user_id()
        data = self._request(
            f"/Users/{uid}/Items/Resume",
            params={"Limit": 0, "MediaTypes": "Video", "EnableImages": "false"},
        )
        return int(data.get("TotalRecordCount", 0) or 0)

    def refresh_library(self, library_id: str) -> bool:
        try:
            resp = self._session.post(
                f"{self.url}/Items/{library_id}/Refresh",
                headers=self._headers(),
                params={"Recursive": "true", "MetadataRefreshMode": "FullRefresh"},
                timeout=15,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error("Emby 刷新媒体库失败 library=%s type=%s", library_id, type(e).__name__)
            return False

    @staticmethod
    def _normalize_path(path: str) -> str:
        return str(path or "").replace("\\", "/").rstrip("/").lower()

    def refresh_for_path(self, media_path: str) -> bool:
        """兼容单路径调用；沿用安全的批量精准刷新策略。"""
        return bool(self.refresh_for_paths([media_path]).get("ok"))

    def refresh_all(self) -> bool:
        """全局刷新媒体库（POST /Library/Refresh）。整理入库后触发。"""
        try:
            resp = self._session.post(
                f"{self.url}/Library/Refresh",
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error("Emby 全局刷新失败 type=%s", type(e).__name__)
            return False
