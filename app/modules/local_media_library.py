"""从任务归档事实解析唯一媒体库绑定与配置指纹。"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from app import database as db
from app.repositories import local_media as local_media_repo
from app.modules.local_media_service import LocalMediaServiceError
from app.clients.base import close_media_server_client
from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient
from app.modules.media_server_path_mapping import (
    MediaServerPathMapping,
    MediaServerRefreshOptions,
    configured_media_server_refresh_options,
)
from app.modules.media_server_profiles import list_configured_profiles
from app.modules.web_secret import get_web_secret



def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _client_for_provider(
    provider: str, profile: Any, *, refresh_options: MediaServerRefreshOptions
) -> Any:
    if provider == "jellyfin":
        return JellyfinClient(profile.url, profile.credential, **refresh_options)
    if provider == "emby":
        return EmbyClient(profile.url, profile.credential, **refresh_options)
    raise LocalMediaServiceError("绑定的媒体服务器类型不受支持")


def _server_config_digest(
    provider: str, profile: Any, refresh_options: MediaServerRefreshOptions
) -> str:
    mappings = refresh_options.get("path_mappings") or ()
    canonical = {
        "provider": provider,
        "url": str(profile.url or "").strip().rstrip("/"),
        "credential": str(profile.credential or ""),
        "path_mappings": [
            {
                "local_prefix": str(getattr(item, "local_prefix", "")),
                "server_prefix": str(getattr(item, "server_prefix", "")),
            }
            for item in mappings
        ],
        "allow_global_refresh_fallback": (refresh_options.get("allow_global_refresh_fallback", False)),
    }
    try:
        secret = get_web_secret().encode("utf-8")
    except Exception:
        raise LocalMediaServiceError(
            "媒体服务器配置指纹暂时不可用"
        ) from None
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        secret,
        b"mediaflux-agent-media-server-config:v1\0" + payload,
        hashlib.sha256,
    ).hexdigest()


def bound_local_task_library(task: Any) -> dict[str, Any]:
    if task.status != "completed":
        raise LocalMediaServiceError(
            "只有已完成整理的任务可以刷新或核验媒体库"
        )
    items = [
        item
        for item in local_media_repo.list_local_media_task_items(task.id, owner="admin")
        if str(item["role"] or "") == "video" and str(item["target_path"] or "").strip()
    ]
    targets = local_media_repo.list_local_library_targets(task.source_id, owner="admin")
    if not items or not targets:
        raise LocalMediaServiceError(
            "任务没有可验证的已绑定媒体库目标"
        )
    resolved: list[tuple[Any, Path]] = []
    for item in items:
        target_path = Path(str(item["target_path"])).expanduser().resolve(strict=False)
        matches = [
            target for target in targets if _path_within(target_path, Path(target.path))
        ]
        if not matches:
            raise LocalMediaServiceError(
                "任务目标未绑定到明确媒体库"
            )
        longest = max(
            len(str(Path(target.path).resolve(strict=False))) for target in matches
        )
        winners = [
            target
            for target in matches
            if len(str(Path(target.path).resolve(strict=False))) == longest
        ]
        if len(winners) != 1:
            raise LocalMediaServiceError(
                "任务目标媒体库绑定存在歧义"
            )
        resolved.append((winners[0], target_path.parent))
    keys = {
        (str(target.provider).lower(), str(target.library_id), str(target.library_name))
        for target, _path in resolved
    }
    if len(keys) != 1:
        raise LocalMediaServiceError(
            "一个任务涉及多个媒体库，不能猜测刷新范围"
        )
    provider, configured_library_id, configured_library_name = next(iter(keys))
    if provider not in {"jellyfin", "emby"} or not (
        configured_library_id or configured_library_name
    ):
        raise LocalMediaServiceError(
            "任务目标没有有效的媒体服务器与媒体库绑定"
        )
    profiles = {
        item.server_type: item
        for item in list_configured_profiles()
        if item.enabled and item.configured
    }
    profile = profiles.get(provider)
    if profile is None:
        raise LocalMediaServiceError(
            "绑定的媒体服务器当前未启用或未配置"
        )
    refresh_options = configured_media_server_refresh_options(provider)
    client = _client_for_provider(provider, profile, refresh_options=refresh_options)
    try:
        try:
            folders = client.list_virtual_folders()
        except Exception:
            raise LocalMediaServiceError(
                "暂时无法读取绑定媒体库"
            ) from None
        if configured_library_id:
            matches = [
                item
                for item in folders
                if str(item.get("id") or "").strip() == configured_library_id
            ]
            if len(matches) != 1:
                raise LocalMediaServiceError(
                    "媒体库绑定已变化，请重新绑定"
                )
            folder = matches[0]
            actual_name = str(folder.get("name") or "").strip()
            if (
                configured_library_name
                and actual_name.casefold() != configured_library_name.casefold()
            ):
                raise LocalMediaServiceError(
                    "媒体库名称与绑定不一致，请重新绑定"
                )
        else:
            matches = [
                item
                for item in folders
                if str(item.get("name") or "").strip().casefold()
                == configured_library_name.casefold()
            ]
            if len(matches) != 1:
                raise LocalMediaServiceError(
                    "媒体库名称无法唯一解析，请重新绑定"
                )
            folder = matches[0]
        library_id = str(folder.get("id") or "").strip()
        if not library_id:
            raise LocalMediaServiceError("媒体库绑定缺少可用标识")
        try:
            paths = sorted(
                {
                    MediaServerPathMapping(target.path, target.server_path).apply(
                        str(path)
                    )
                    if str(getattr(target, "server_path", "") or "").strip()
                    else str(path)
                    for target, path in resolved
                }
            )
        except ValueError:
            raise LocalMediaServiceError(
                "媒体库服务端路径映射无效，请重新绑定"
            ) from None
        server_config_digest = _server_config_digest(provider, profile, refresh_options)
        binding = {
            "provider": provider,
            "server_config_digest": server_config_digest,
            "library_id": library_id,
            "library_name": str(
                folder.get("name") or configured_library_name or ""
            ).strip(),
            "paths_digest": hashlib.sha256(
                "\n".join(paths).encode("utf-8")
            ).hexdigest(),
        }
        return {
            "task": task,
            "client": client,
            "paths": paths,
            "binding": binding,
            "server_label": (profile.label),
        }
    except Exception:
        close_media_server_client(client)
        raise


def _refresh_snapshot(task_id: int) -> tuple[dict, dict]:
    from app.modules.local_media_models import LocalMediaTask
    from app.modules.local_media_tasks import _snapshot
    from app.repositories.local_media import LocalMediaTaskChanged
    snapshot = _snapshot(task_id)
    scope = bound_local_task_library(LocalMediaTask.from_row(snapshot["task"]))
    try:
        if snapshot != _snapshot(task_id):
            raise LocalMediaTaskChanged("任务或媒体库绑定已变化，请重新预检")
        return {**snapshot, "binding": scope["binding"]}, scope
    except Exception:
        close_media_server_client(scope["client"])
        raise


def prepare_local_task_refresh(task_id: int) -> dict:
    from app.modules.web_secret import signed_context_fingerprint
    snapshot, scope = _refresh_snapshot(task_id)
    try:
        return {"fingerprint": signed_context_fingerprint(snapshot, domain="local-task-refresh"),
                "preview": {"ok": True, "status": "confirmation_required", "summary": f"确认后精准刷新本地任务 {task_id} 的绑定媒体库",
                            "data": {"task_id": task_id, "server": scope["server_label"], "binding": scope["binding"],
                                     "paths": scope["paths"], "path_count": len(scope["paths"]),
                                     "effects": ["只刷新重新核验的唯一媒体库及任务路径；不回退全库刷新",
                                                 "刷新请求不等于已经可见，需另行verify_library核验"]}}}
    finally:
        close_media_server_client(scope["client"])


def refresh_local_task_library(task_id: int, *, expected_fingerprint: str, cancelled=None) -> dict:
    import secrets
    from app.modules.web_secret import signed_context_fingerprint
    from app.repositories.local_media import LocalMediaTaskChanged
    try:
        snapshot, scope = _refresh_snapshot(task_id)
    except (LocalMediaServiceError, ValueError, LookupError) as exc:
        raise LocalMediaTaskChanged(str(exc)) from exc
    try:
        if not secrets.compare_digest(signed_context_fingerprint(snapshot, domain="local-task-refresh"), expected_fingerprint):
            raise LocalMediaTaskChanged("任务、路径或媒体服务器配置已变化，请重新预检")
        data = {"operation": "precise_refresh", "task_id": task_id, "binding": scope["binding"], "paths": scope["paths"]}
        if cancelled is not None and cancelled():
            return {"ok": False, "status": "cancelled", "summary": "精准刷新已取消", "data": data}
        lease_key = hashlib.sha256(json.dumps({"operation": "local_media.precise_refresh", "task_id": task_id,
            "task_version": scope["task"].version, "server_config_digest": scope["binding"]["server_config_digest"],
            "library_id": scope["binding"]["library_id"], "paths_digest": scope["binding"]["paths_digest"]},
            sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if db.claim_agent_action_lease(lease_key, ttl_seconds=90) is None:
            return {"ok": False, "status": "conflict", "summary": "相同任务/媒体库刷新刚刚已提交，请核验可见性",
                    "data": {**data, "refreshed": 0}}
        if cancelled is not None and cancelled():
            return {"ok": False, "status": "cancelled", "summary": "精准刷新已取消", "data": data}
        try:
            outcome = scope["client"].refresh_for_paths(scope["paths"], allowed_library_ids=(scope["binding"]["library_id"],),
                                                        allow_library_fallback=False)
        except Exception as exc:
            from app.sensitive_data import redact_sensitive_text
            return {"ok": False, "status": "outcome_unknown", "summary": "精准刷新回执未知，请核验媒体库，勿重复投递",
                    "data": {**data, "refreshed": 0}, "error": redact_sensitive_text(str(exc))}
        ok = isinstance(outcome, dict) and bool(outcome.get("ok")) and not outcome.get("skipped") and outcome.get("scope") == "item"
        from app.modules.local_media_tasks import _redacted
        return {"ok": ok, "status": "completed" if ok else "unavailable", "summary": "精准刷新请求已提交，可另行核验媒体可见性" if ok else "媒体服务器未接受精准刷新",
                "data": _redacted({**data, "refreshed": int(ok), "outcome": outcome}),
                "error": "" if ok else "精准刷新未受理，不能回退为全库刷新"}
    finally:
        close_media_server_client(scope["client"])


def verify_local_task_library(task_id: int) -> dict:
    from app.modules.local_media_tasks import _redacted
    _snapshot, scope = _refresh_snapshot(task_id)
    try:
        task = scope["task"]
        identity = str(task.tmdb_id or "").strip()
        status, reason = "inconclusive", "identity_unavailable"
        if identity.isascii() and identity.isdigit() and task.media_type in {"movie", "tv"}:
            try:
                if task.media_type == "movie":
                    visible = scope["client"].has_tmdb_media(identity, "movie", parent_id=scope["binding"]["library_id"])
                    status, reason = ("visible", "movie_indexed") if visible else ("missing", "movie_not_indexed")
                else:
                    search = scope["client"].find_series_candidates_by_tmdb(identity, limit=20, parent_id=scope["binding"]["library_id"])
                    if len(search.candidates) == 1 and not search.truncated:
                        if task.season_override is None or task.episode_override is None:
                            reason = "series_indexed_episode_unverified"
                        else:
                            inventory = scope["client"].list_series_episode_inventory(search.candidates[0].id, include_specials=task.season_override == 0)
                            if (task.season_override, task.episode_override) in inventory.episodes:
                                status, reason = "visible", "episode_indexed"
                            elif inventory.truncated:
                                reason = "episode_inventory_incomplete"
                            else:
                                status, reason = "missing", "episode_not_indexed"
                    elif not search.candidates and not search.truncated:
                        status, reason = "missing", "series_not_indexed"
                    else:
                        reason = "series_mapping_ambiguous"
            except Exception as exc:
                from app.sensitive_data import redact_sensitive_text
                return {"ok": False, "status": "inconclusive", "summary": "媒体服务器读取失败，无法判断是否可见",
                        "data": {"task_id": task_id, "reason_code": "media_server_unavailable", "playback_status": "not_checked"},
                        "error": redact_sensitive_text(str(exc))}
        return {"ok": status == "visible", "status": status, "summary": "媒体库索引状态已核验；未执行播放探测",
                "data": _redacted({"task_id": task_id, "index_status": status, "reason_code": reason,
                                   "binding": scope["binding"], "paths": scope["paths"], "playback_status": "not_checked", "playback_claim": "not_probed"})}
    finally:
        close_media_server_client(scope["client"])
