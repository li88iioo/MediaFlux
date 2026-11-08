"""候选卡片的只读白名单投影与选择凭证；不执行领域工具。"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.modules.resource_candidates import (
    MAX_RESOURCE_CANDIDATES,
    validate_safe_resource_snapshot,
)
from app.agent.kernel.references import ReferenceError, ReferenceStore
from app.agent.kernel.session_state import CandidateSelectionGuard, SessionState
from app.agent.kernel.state_types import SelectionInvalidError
from app.agent.kernel.ux_display import display_text
from app.agent.public_safety import sanitize_resource_title

CANDIDATE_VIEW_KEY = "ux_candidate_view"
RESOURCE_KIND = "resource_candidates"
SELECTION_KIND = "ux_resource_selection"
_REF_RE = re.compile(r"^ref_[A-Za-z0-9_-]{16,160}$")
_TAGS = {"resolution", "media", "video_codec", "effect", "audio"}


def normalize_selection(value: Any) -> dict[str, Any]:
    """新协议只接受批次凭证、明确位置集合和目标；旧单卡协议只读。"""
    if (
        not isinstance(value, dict) or set(value) != {"ref", "positions", "target"}
        or not isinstance(value["ref"], str) or not _REF_RE.fullmatch(value["ref"])
        or not isinstance(value["positions"], list) or not 1 <= len(value["positions"]) <= MAX_RESOURCE_CANDIDATES
        or any(type(pos) is not int or not 1 <= pos <= MAX_RESOURCE_CANDIDATES for pos in value["positions"])
        or len(set(value["positions"])) != len(value["positions"])
        or value["target"] not in ("qb", "guangya", "both")
    ):
        raise SelectionInvalidError()
    return {"ref": value["ref"], "positions": sorted(value["positions"]), "target": value["target"]}


def _target_options(owner: str) -> dict[str, Any]:
    # 配置/登录状态来自领域现有契约；只用于展示，提交仍会再次预检。
    from app.indexers.downloads import download_target_readiness
    from app.modules.media_preferences import resolve_download_target

    ready = download_target_readiness("both")
    target, target_source = resolve_download_target(owner)
    return {
        "target": target,
        "target_source": target_source,
        "targets": [
            {"value": name, "label": label, "available": available}
            for name, label, available in (
                ("qb", "qBittorrent", ready.get("qb", False)),
                ("guangya", "光鸭", ready.get("guangya", False)),
                ("both", "两个目标", all(ready.values()) and len(ready) == 2),
            )
        ],
    }


def _recommend(items: list[dict[str, Any]]) -> list[int]:
    """只推荐领域已确认覆盖目标缺集的互补资源；普通搜索不推断下载意图。"""
    covered: set[tuple[str, int, int]] = set()
    selected = []
    for item in items:
        if not (item.get("media_title") and item.get("requested_episode")
                and item.get("match") in {"exact_episode", "episode_pack"}):
            continue
        title = item["media_scope"]
        season, episode = item["requested_episode"]
        if (title, season, episode) in covered:
            continue
        selected.append(item["position"])
        coverage = item.get("coverage")
        start, end = (coverage[1], coverage[2]) if coverage and coverage[0] == season and coverage[1] <= episode <= coverage[2] else (episode, episode)
        covered.update((title, season, number) for number in range(start, end + 1))
    return selected


def _snapshot(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    # 绝不调用 restore_resource_candidate_reference：它会恢复 Provider 句柄。
    snapshot = validate_safe_resource_snapshot({
        key: value.get(key) for key in ("search_id", "search_status", "candidates")
    })
    if snapshot is not None and "_verification_targets" in value:
        snapshot["_verification_targets"] = deepcopy(value["_verification_targets"])
    return snapshot


def _messages(value: Any, *, maximum: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        text for item in value[:maximum]
        if (text := display_text(item, limit=120))
    ))


def candidate_item(value: Mapping[str, Any], position: int) -> dict[str, Any]:
    from app.modules.episode_mapping import extract_release_episode_range

    title = sanitize_resource_title(value.get("title")) or f"候选 {position}"
    season, start, end = extract_release_episode_range(unicodedata.normalize("NFKC", str(value.get("title") or "")))
    coverage = [season, start, end] if start and end and end - start < 1000 else None
    verification = value.get("_verification_context")
    verification = verification if isinstance(verification, dict) else {}
    requested = [verification.get("season"), verification.get("episode")] if verification else value.get("requested_episode")
    if not (isinstance(requested, list) and len(requested) == 2
            and type(requested[0]) is int and 1 <= requested[0] <= 100
            and type(requested[1]) is int and 1 <= requested[1] <= 1000):
        requested = None
    media_title = display_text(verification.get("title") or value.get("media_title"), limit=120)
    media_scope = (f"tmdb:{verification['tmdb_id']}" if verification.get("tmdb_id")
                   else display_text(value.get("media_scope"), limit=140) or media_title)
    quality = value.get("quality")
    quality = quality if isinstance(quality, dict) else value
    tags = quality.get("tags")
    match = quality.get("match")
    if not match and "episode_conflict" in (value.get("match_reasons") or []):
        match = "conflict"
    return {
        "position": position,
        "result_id": str(value.get("result_id") or ""),
        "title": title,
        "coverage": coverage,
        "media_title": media_title,
        "media_scope": media_scope,
        "requested_episode": requested,
        "match": match if match in ("exact_episode", "episode_pack", "season_pack", "unknown", "conflict") else "",
        "site_name": display_text(value.get("site_name"), limit=80),
        "size_text": display_text(value.get("size_text"), limit=32),
        "tags": {
            key: text for key in sorted(_TAGS)
            if isinstance(tags, dict) and (text := display_text(tags.get(key), limit=64))
        },
        "reasons": _messages(quality.get("reasons"), maximum=6),
        "warnings": _messages(quality.get("warnings"), maximum=4),
    }


def _candidate_summary(items: list[dict[str, Any]], *, explicit_selection: bool = False) -> dict[str, Any] | None:
    """已核验缺集可直接展示；普通命中必须先明确挑选，不能覆盖缺集证据不足。"""
    recommended = _recommend(items)
    if not items or (not recommended and (
        not explicit_selection or any(item.get("requested_episode") for item in items)
    )):
        return None
    if not explicit_selection:
        items = [item for item in items if item["position"] in recommended]
    summary: dict[str, Any] = {"items": items, "recommended_positions": recommended}
    if explicit_selection:
        summary["explicit_selection"] = True
    return summary


async def issue_candidate_view(
    *, store: ReferenceStore, owner: str, session_id: str, generation: int,
    ref: str, items: list[dict[str, Any]], ttl_seconds: int, turn_id: str = "",
    selected_positions: list[int] | None = None,
) -> dict[str, Any] | None:
    if selected_positions is not None:
        items = [item for item in items if item["position"] in selected_positions]
    summary = _candidate_summary(items, explicit_selection=selected_positions is not None)
    if summary is None:
        return None
    ttl = max(1, min(ttl_seconds, 86_400))
    expires_at = time.time() + ttl
    selection = await store.put(
        owner=owner, session_id=session_id, kind=SELECTION_KIND, ttl_seconds=ttl,
        value={"ref": ref, "generation": generation, "expires_at": expires_at},
    )
    return {
        "ref": ref, "selection_ref": selection.ref, "expires_at": expires_at,
        "generation": generation, "turn_id": turn_id, **summary,
        **await asyncio.to_thread(_target_options, owner),
    }


def resource_download_arguments(snapshot: dict[str, Any], positions: list[int], target: str = "preferred") -> dict[str, Any]:
    """把已绑定的卡片选择转换为原生资源ID及待服务端复核的目标。"""
    candidates = {item["position"]: item for item in snapshot["candidates"]}
    selected = [candidates[position] for position in positions]
    arguments: dict[str, Any] = {"result_ids": [item["result_id"] for item in selected], "target": target}
    goals = [deepcopy(goal) for goal in snapshot.get("_verification_targets", [])
             if goal["result_id"] in arguments["result_ids"]]
    for item in selected:
        proof = item.get("_verification_context")
        if isinstance(proof, dict) and not any(goal["result_id"] == item["result_id"] for goal in goals):
            goals.append({"result_id": item["result_id"], "query": proof["title"],
                          **{key: value for key, value in proof.items() if key != "title"}})
    if goals:
        arguments["verification_targets"] = goals
    return arguments


@dataclass(frozen=True, slots=True)
class ValidatedSelection:
    guard: CandidateSelectionGuard
    arguments: Mapping[str, Any]


async def validate_selection(
    value: Any, *, state: SessionState, store: ReferenceStore, for_preview: bool = True,
) -> ValidatedSelection:
    selection = normalize_selection(value)
    view = state.metadata.get(CANDIDATE_VIEW_KEY)
    try:
        if not isinstance(view, dict):
            raise SelectionInvalidError()
        guard = CandidateSelectionGuard(
            state.generation, view["ref"], view["expires_at"], batch_generation=view["generation"],
        )
        guard.check(state)
        items = view.get("items")
        if (
            not isinstance(items, list) or selection["ref"] != view.get("selection_ref")
            or not set(selection["positions"]).issubset({item["position"] for item in items})
        ):
            raise SelectionInvalidError()
        bound = await store.resolve(
            selection["ref"], owner=state.owner, session_id=state.session_id,
            expected_kind=SELECTION_KIND,
        )
        if bound != {
            "ref": guard.ref, "generation": guard.batch_generation, "expires_at": guard.expires_at,
        }:
            raise SelectionInvalidError()
        resource = await store.resolve(
            guard.ref, owner=state.owner, session_id=state.session_id, expected_kind=RESOURCE_KIND,
        )
        snapshot = _snapshot(resource)
        if snapshot is None or not set(selection["positions"]).issubset(
            {item["position"] for item in snapshot["candidates"]}
        ):
            raise SelectionInvalidError()
        pending = state.metadata.get("ux_candidate_plan")
        if (
            for_preview and state.pending_effect_plan_id and isinstance(pending, dict)
            and pending.get("plan_id") == state.pending_effect_plan_id and pending.get("ref") == guard.ref
        ):
            raise SelectionInvalidError("已有这批资源的待确认计划，请先确认或取消后再改选。")
        guard.check(state)
        return ValidatedSelection(guard, resource_download_arguments(
            snapshot, selection["positions"], selection["target"],
        ))
    except (ReferenceError, KeyError, TypeError, ValueError) as exc:
        raise SelectionInvalidError() from exc


async def current_candidate_view(
    *, state: SessionState, store: ReferenceStore,
) -> dict[str, Any] | None:
    view = getattr(state, "metadata", {}).get(CANDIDATE_VIEW_KEY)
    if not isinstance(view, dict) or not isinstance(view.get("items"), list):
        return None
    try:
        await validate_selection({
            "ref": view.get("selection_ref"),
            "positions": [item["position"] for item in view["items"]], "target": "guangya",
        }, state=state, store=store, for_preview=False)
        public = {key: deepcopy(view[key]) for key in (
            "ref", "selection_ref", "expires_at", "turn_id", "coverage_notice",
        ) if key in view}
        summary = _candidate_summary(
            [candidate_item(item, item["position"]) for item in view["items"]],
            explicit_selection=view.get("explicit_selection") is True,
        )
        if summary is None:
            return None
        public.update(summary)
        result = state.metadata.get("ux_candidate_result")
        if isinstance(result, dict) and result.get("ref") == view["ref"]:
            public["last_result"] = {key: deepcopy(result[key]) for key in ("text", "target", "handled_positions") if key in result}
        public.update(await asyncio.to_thread(_target_options, state.owner))
        return public
    except (SelectionInvalidError, KeyError, TypeError, ValueError):
        return None


def candidate_result(public: Mapping[str, Any], view: dict[str, Any] | None) -> dict[str, Any]:
    """资源结果公开面整体使用白名单，不能在新卡旁边继续携带原始搜索 DTO。"""
    return {
        "ok": public.get("ok") is not False,
        "status": display_text(public.get("status"), limit=40) or "success",
        "summary": display_text(public.get("summary"), limit=600) or "资源候选已更新",
        "candidate_view": {key: deepcopy(value) for key, value in view.items() if key != "generation"}
        if view else None,
    }
