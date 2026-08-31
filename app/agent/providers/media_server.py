"""复用现有 Jellyfin/Emby client 的 Provider transport。"""
from __future__ import annotations

from typing import Any

from app.agent.provider_models import (
    ProviderGatewayError,
    ProviderPayload,
    ProviderProfileView,
)
from app.clients.base import MediaItem, normalize_explicit_media_user_id
from app.modules.media_server_profiles import (
    MediaServerProfile,
    list_configured_profiles,
)


class MediaServerProviderTransport:
    provider = "media"

    @staticmethod
    def _profile(profile_ref: str) -> MediaServerProfile:
        for profile in list_configured_profiles():
            if profile.source == profile_ref:
                return profile
        raise ProviderGatewayError(
            "媒体服务器 profile 不存在", code="provider_not_configured"
        )

    @staticmethod
    def _client(profile: MediaServerProfile):
        if profile.server_type == "jellyfin":
            from app.clients.jellyfin import JellyfinClient
            client = JellyfinClient(profile.url, profile.credential, timeout=10)
        elif profile.server_type == "emby":
            from app.clients.emby import EmbyClient
            client = EmbyClient(profile.url, profile.credential, timeout=10)
        else:
            raise ProviderGatewayError(
                "媒体服务器类型不受支持", code="provider_version_unsupported"
            )
        if profile.user_id:
            # 复用 client 已有用户缓存，不建立 Agent 专用用户解析分支。
            client._cached_user_id = normalize_explicit_media_user_id(profile.user_id)
        return client

    def profiles(self) -> list[ProviderProfileView]:
        views: list[ProviderProfileView] = []
        for profile in list_configured_profiles():
            if profile.enabled and profile.configured:
                state = "online"
            elif profile.enabled:
                state = "incomplete"
            else:
                state = "disabled"
            views.append(ProviderProfileView(
                profile_ref=profile.source,
                provider=self.provider,
                label=profile.label,
                state=state,
            ))
        return views

    @staticmethod
    def _media_item(item: MediaItem) -> dict[str, Any]:
        return {
            "__object_id": str(item.id or ""),
            "__object_kind": "media_item",
            "name": str(item.name or ""),
            "type": str(item.type or ""),
            "year": str(item.year or ""),
            "runtime_minutes": int(item.runtime or 0),
            "series_name": str(item.series_name or ""),
            "season_number": item.season_number,
            "episode_number": item.episode_number,
            "date_added": str(item.date_added or ""),
            "overview": str(item.overview or ""),
            "last_played": str(item.last_played or ""),
            "progress_percent": float(item.progress or 0),
        }

    def execute_read(
        self, profile_ref: str, operation: str, arguments: dict[str, Any]
    ) -> ProviderPayload:
        profile = self._profile(profile_ref)
        if not profile.enabled or not profile.configured:
            raise ProviderGatewayError(
                "媒体服务器尚未启用或配置不完整", code="provider_not_configured"
            )
        try:
            with self._client(profile) as client:
                if operation == "media.system.info":
                    name, product, version = client.server_identity()
                    return ProviderPayload(
                        summary=f"{profile.label} 系统信息已读取",
                        data={
                            "server": {
                                "label": profile.label,
                                "name": name,
                                "product": product,
                                "version": version,
                            }
                        },
                        source=f"{profile.server_type}_api",
                    )
                if operation == "media.libraries.list":
                    folders = client.list_virtual_folders()
                    libraries = [
                        {
                            "__object_id": str(folder.get("id") or ""),
                            "__object_kind": "media_library",
                            "name": str(folder.get("name") or ""),
                            "collection_type": str(folder.get("collection_type") or "mixed"),
                        }
                        for folder in folders
                        if str(folder.get("id") or "").strip()
                    ]
                    return ProviderPayload(
                        summary=f"{profile.label} 返回 {len(libraries)} 个媒体库",
                        data={"libraries": libraries, "count": len(libraries)},
                        source=f"{profile.server_type}_api",
                    )
                if operation == "media.items.search":
                    limit = int(arguments.get("limit", 12))
                    items = client.search_media(str(arguments["query"]), limit=limit)
                    return ProviderPayload(
                        summary=f"{profile.label} 搜索到 {len(items)} 个媒体条目",
                        data={
                            "items": [self._media_item(item) for item in items],
                            "count": len(items),
                        },
                        source=f"{profile.server_type}_api",
                    )
                if operation == "media.series.search":
                    result = client.search_series_candidates(
                        str(arguments["query"]), limit=int(arguments.get("limit", 6))
                    )
                    candidates = [
                        {
                            "__object_id": candidate.id,
                            "__object_kind": "media_series",
                            "name": candidate.name,
                            "year": candidate.year,
                            "tmdb_id": candidate.tmdb_id,
                        }
                        for candidate in result.candidates
                    ]
                    return ProviderPayload(
                        summary=f"{profile.label} 搜索到 {len(candidates)} 个剧集候选",
                        data={
                            "series": candidates,
                            "count": len(candidates),
                            "total": int(result.total or 0),
                            "truncated": bool(result.truncated),
                        },
                        source=f"{profile.server_type}_api",
                    )
                if operation == "media.series.episodes":
                    inventory = client.list_series_episode_inventory(
                        str(arguments["series_ref"]),
                        max_episodes=int(arguments.get("max_episodes", 500)),
                        include_specials=bool(arguments.get("include_specials", False)),
                    )
                    episodes = [
                        {"season": season, "episode": episode}
                        for season, episode in inventory.episodes
                    ]
                    return ProviderPayload(
                        summary=f"{profile.label} 已读取 {len(episodes)} 个本地剧集位置",
                        data={
                            "episodes": episodes,
                            "count": len(episodes),
                            "total": int(inventory.total or 0),
                            "truncated": bool(inventory.truncated),
                            "ignored_specials": int(inventory.ignored_specials or 0),
                            "ignored_unknown": int(inventory.ignored_unknown or 0),
                        },
                        source=f"{profile.server_type}_api",
                    )
        except ProviderGatewayError:
            raise
        except TimeoutError as exc:
            raise ProviderGatewayError(
                "媒体服务器请求超时", code="upstream_timeout"
            ) from exc
        except Exception as exc:
            raise ProviderGatewayError(
                "媒体服务器当前不可用", code="provider_unavailable"
            ) from exc
        raise ProviderGatewayError("媒体服务器操作未实现", code="operation_not_allowed")
