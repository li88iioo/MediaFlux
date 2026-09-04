"""复用现有 Jellyfin/Emby client 的 Provider transport。"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.media_links import media_open_url_resolver
from app.agent.provider_models import (
    ProviderGatewayError,
    ProviderPayload,
    ProviderProfileView,
)
from app.agent.providers.media_recommendation import rank_local_recommendations
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
            views.append(
                ProviderProfileView(
                    profile_ref=profile.source,
                    provider=self.provider,
                    label=profile.label,
                    state=state,
                )
            )
        return views

    def profile_revision(self, profile_ref: str) -> str:
        profile = self._profile(profile_ref)
        return self._profile_revision_from_profile(profile)

    @staticmethod
    def _profile_revision_from_profile(profile: MediaServerProfile) -> str:
        return confirmation_context_fingerprint(
            {
                "profile_ref": profile.source,
                "server_type": profile.server_type,
                "url": profile.url.rstrip("/"),
                "credential": profile.credential,
                "user_id": profile.user_id,
                "enabled": profile.enabled,
            },
            domain="provider-profile-revision",
        )

    @staticmethod
    def _media_item(
        resolve_link: Callable[[object], str],
        item: MediaItem,
    ) -> dict[str, Any]:
        result = {
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
            "genres": [str(value) for value in item.genres[:12] if str(value).strip()],
        }
        open_url = resolve_link(item.web_url or item.series_web_url)
        if open_url:
            result["open_url"] = open_url
        return result

    @staticmethod
    def _preference_signals(items: list[MediaItem]) -> dict[str, Any]:
        """从播放历史提取有界偏好信号，供同一模型回合继续推荐。"""
        title_counts: Counter[str] = Counter()
        genre_counts: Counter[str] = Counter()
        media_type_counts: Counter[str] = Counter()
        for item in items:
            title = str(item.display_name or "").strip()
            if title:
                title_counts[title] += 1
            media_type = str(item.type or "").strip().casefold()
            if media_type:
                media_type_counts[media_type] += 1
            for genre in item.genres:
                normalized = str(genre or "").strip()
                if normalized:
                    genre_counts[normalized] += 1
        return {
            "recent_titles": [name for name, _count in title_counts.most_common(12)],
            "top_genres": [name for name, _count in genre_counts.most_common(8)],
            "media_types": [name for name, _count in media_type_counts.most_common(4)],
        }

    @staticmethod
    def _playback_user(
        profile: MediaServerProfile, client: Any
    ) -> tuple[str, str]:
        value = str(profile.user_id or "").strip()
        try:
            if value:
                return normalize_explicit_media_user_id(value), "配置用户"
            return (
                normalize_explicit_media_user_id(client._user_id()),
                "服务器默认用户",
            )
        except (ValueError, RuntimeError) as exc:
            raise ProviderGatewayError(
                "媒体服务器没有可用于观看数据的用户",
                code="provider_user_required",
            ) from exc

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
                link_resolver: Callable[[object], str] | None = None

                def resolve_link(item_url: object) -> str:
                    nonlocal link_resolver
                    if link_resolver is None:
                        link_resolver = media_open_url_resolver(
                            server_type=profile.server_type,
                            server_url=profile.url,
                        )
                    return link_resolver(item_url)

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
                if operation == "media.items.counts":
                    raw_counts = client.get_media_counts()
                    counts = {
                        key: max(0, int(raw_counts.get(key, 0) or 0))
                        for key in (
                            "total_items",
                            "movie_count",
                            "series_count",
                            "episode_count",
                        )
                    }
                    return ProviderPayload(
                        summary=(
                            f"{profile.label} 当前共有 {counts['total_items']} 个可播放媒体项"
                        ),
                        data={"server_label": profile.label, **counts},
                        source=f"{profile.server_type}_api",
                    )
                if operation == "media.items.recent_added":
                    limit = int(arguments.get("limit", 8))
                    items = client.recent_media(limit=limit)
                    return ProviderPayload(
                        summary=f"{profile.label} 返回 {len(items)} 项最近入库内容",
                        data={
                            "server_label": profile.label,
                            "count": len(items),
                            "items": [self._media_item(resolve_link, item) for item in items],
                        },
                        source=f"{profile.server_type}_api",
                    )
                if operation == "media.items.recent_played":
                    limit = int(arguments.get("limit", 8))
                    user_id, user_selection = self._playback_user(profile, client)
                    items = client.recently_played(user_id, limit=limit)
                    items = client.enrich_media_genres(user_id, items)
                    return ProviderPayload(
                        summary=f"{profile.label} 返回 {len(items)} 项最近播放记录",
                        data={
                            "server_label": profile.label,
                            "user_selection": user_selection,
                            "history_kind": "最近播放",
                            "count": len(items),
                            "items": [self._media_item(resolve_link, item) for item in items],
                            "preference_signals": self._preference_signals(items),
                        },
                        source=f"{profile.server_type}_api",
                    )
                if operation == "media.items.recommend_from_library":
                    user_id, user_selection = self._playback_user(profile, client)
                    history = client.recently_played(user_id, limit=50)
                    history = client.enrich_media_genres(user_id, history)
                    inventory = client.list_recommendation_candidates(
                        user_id,
                        media_type=str(arguments.get("media_type") or "any"),
                        max_items=5_000,
                        page_size=200,
                    )
                    ranked = rank_local_recommendations(
                        inventory.candidates,
                        history,
                        must_match=list(arguments.get("must_match") or []),
                        prefer=list(arguments.get("prefer") or []),
                        exclude=list(arguments.get("exclude") or []),
                        min_rating=float(arguments.get("min_rating", 0) or 0),
                        exclude_played=bool(arguments.get("exclude_played", True)),
                        limit=int(arguments.get("limit", 8)),
                    )
                    for item in ranked.get("items", []):
                        if not isinstance(item, dict):
                            continue
                        item_id = str(item.get("__object_id") or "").strip()
                        open_url = resolve_link(client.media_web_url(item_id))
                        if open_url:
                            item["open_url"] = open_url
                    ranked.update(
                        {
                            "server_label": profile.label,
                            "user_selection": user_selection,
                            "inventory_total": int(inventory.total or 0),
                            "inventory_truncated": bool(inventory.truncated),
                            "criteria": {
                                "media_type": str(
                                    arguments.get("media_type") or "any"
                                ),
                                "must_match": list(
                                    arguments.get("must_match") or []
                                ),
                                "prefer": list(arguments.get("prefer") or []),
                                "exclude": list(arguments.get("exclude") or []),
                                "min_rating": float(
                                    arguments.get("min_rating", 0) or 0
                                ),
                                "exclude_played": bool(
                                    arguments.get("exclude_played", True)
                                ),
                            },
                        }
                    )
                    count = int(ranked.get("count", 0) or 0)
                    return ProviderPayload(
                        summary=(
                            f"{profile.label} 从本地媒体库筛选出 {count} 部"
                            "符合条件且可直接观看的候选"
                        ),
                        data=ranked,
                        source=f"{profile.server_type}_api",
                        status="success" if count else "empty",
                        suggestions=(
                            []
                            if count
                            else ["可减少必须匹配条件或降低最低评分后重试。"]
                        ),
                    )
                if operation == "media.items.continue_watching":
                    limit = int(arguments.get("limit", 8))
                    user_id, user_selection = self._playback_user(profile, client)
                    items = client.continue_watching(user_id, limit=limit)
                    return ProviderPayload(
                        summary=f"{profile.label} 返回 {len(items)} 项继续观看内容",
                        data={
                            "server_label": profile.label,
                            "user_selection": user_selection,
                            "history_kind": "继续观看",
                            "count": len(items),
                            "items": [self._media_item(resolve_link, item) for item in items],
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
                            "collection_type": str(
                                folder.get("collection_type") or "mixed"
                            ),
                        }
                        for folder in folders
                        if str(folder.get("id") or "").strip()
                    ]
                    return ProviderPayload(
                        summary=f"{profile.label} 返回 {len(libraries)} 个媒体库",
                        data={"libraries": libraries, "count": len(libraries)},
                        source=f"{profile.server_type}_api",
                    )
                if operation == "media.library.counts":
                    raw_counts = client.get_library_media_counts(
                        str(arguments["library_ref"])
                    )
                    counts = {
                        key: max(0, int(raw_counts.get(key, 0) or 0))
                        for key in (
                            "total_items",
                            "movie_count",
                            "series_count",
                            "episode_count",
                        )
                    }
                    return ProviderPayload(
                        summary=(
                            f"{profile.label} 所选媒体库共有 "
                            f"{counts['series_count']} 部剧集、"
                            f"{counts['movie_count']} 部电影和 "
                            f"{counts['episode_count']} 集单集"
                        ),
                        data={"server_label": profile.label, **counts},
                        source=f"{profile.server_type}_api",
                    )
                if operation == "media.items.search":
                    limit = int(arguments.get("limit", 12))
                    items = client.search_media(str(arguments["query"]), limit=limit)
                    return ProviderPayload(
                        summary=f"{profile.label} 搜索到 {len(items)} 个媒体条目",
                        data={
                            "items": [self._media_item(resolve_link, item) for item in items],
                            "count": len(items),
                        },
                        source=f"{profile.server_type}_api",
                    )
                if operation == "media.series.search":
                    result = client.search_series_candidates(
                        str(arguments["query"]), limit=int(arguments.get("limit", 6))
                    )
                    candidates = []
                    for candidate in result.candidates:
                        item = {
                            "__object_id": candidate.id,
                            "__object_kind": "media_series",
                            "name": candidate.name,
                            "year": candidate.year,
                            "tmdb_id": candidate.tmdb_id,
                        }
                        open_url = resolve_link(client.media_web_url(candidate.id))
                        if open_url:
                            item["open_url"] = open_url
                        candidates.append(item)
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

    def preview_write(
        self,
        profile_ref: str,
        operation: str,
        arguments: dict[str, Any],
        target_snapshot: dict[str, Any],
    ) -> ProviderPayload:
        profile = self._profile(profile_ref)
        if not profile.enabled or not profile.configured:
            raise ProviderGatewayError(
                "媒体服务器尚未启用或配置不完整", code="provider_not_configured"
            )
        if operation == "media.library.refresh":
            target = dict(target_snapshot.get("library_ref") or {})
            label = str(target.get("name") or "选中媒体库")
            return ProviderPayload(
                summary=f"将精准刷新 {label}",
                data={
                    "target": target,
                    "scope": "library",
                    "refresh_mode": "incremental",
                },
                source=f"{profile.server_type}_api",
            )
        if operation == "media.item.refresh":
            target = dict(target_snapshot.get("item_ref") or {})
            label = str(target.get("name") or "选中媒体条目")
            return ProviderPayload(
                summary=f"将精准刷新 {label}",
                data={"target": target, "scope": "item", "refresh_mode": "incremental"},
                source=f"{profile.server_type}_api",
            )
        raise ProviderGatewayError(
            "媒体服务器写操作未实现", code="operation_not_allowed"
        )

    def execute_write(
        self,
        profile_ref: str,
        operation: str,
        arguments: dict[str, Any],
        *,
        expected_profile_revision: str,
    ) -> ProviderPayload:
        # MediaServerProfile 是 frozen 配置快照；revision 校验与 client 构造
        # 必须复用同一个对象，避免并发改配后写到另一上游。
        profile = self._profile(profile_ref)
        current_revision = self._profile_revision_from_profile(profile)
        if (
            not expected_profile_revision
            or current_revision != expected_profile_revision
        ):
            raise ProviderGatewayError(
                "媒体服务器配置已变化，请重新预检",
                code="confirmation_stale",
            )
        if not profile.enabled or not profile.configured:
            raise ProviderGatewayError(
                "媒体服务器尚未启用或配置不完整", code="provider_not_configured"
            )
        if operation == "media.library.refresh":
            target_id = str(arguments.get("library_ref") or "").strip()
            scope = "library"
        elif operation == "media.item.refresh":
            target_id = str(arguments.get("item_ref") or "").strip()
            scope = "item"
        else:
            raise ProviderGatewayError(
                "媒体服务器写操作未实现", code="operation_not_allowed"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target_id):
            raise ProviderGatewayError("刷新目标已失效", code="confirmation_stale")
        external_write_possible = False
        try:
            with self._client(profile) as client:
                # refresh_library 的客户端契约会把部分网络异常折叠为 False；
                # 进入调用后只能保守认为请求可能已被上游接收。
                external_write_possible = True
                accepted = bool(client.refresh_library(target_id))
        except ProviderGatewayError:
            raise
        except TimeoutError as exc:
            raise ProviderGatewayError(
                "媒体服务器刷新请求超时",
                code="upstream_timeout",
                external_write_possible=external_write_possible,
            ) from exc
        except Exception as exc:
            raise ProviderGatewayError(
                "媒体服务器刷新请求失败",
                code="provider_write_failed",
                external_write_possible=external_write_possible,
            ) from exc
        if not accepted:
            raise ProviderGatewayError(
                "媒体服务器未接受精准刷新请求",
                code="provider_write_failed",
                external_write_possible=True,
            )
        return ProviderPayload(
            summary="媒体服务器已接受精准刷新请求",
            data={
                "accepted": True,
                "scope": scope,
                "verification": "accepted",
                "global_refresh": False,
            },
            source=f"{profile.server_type}_api",
        )
