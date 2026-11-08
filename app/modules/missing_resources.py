"""已播缺集的资源检索与持久进度；复用正式库存审计、索引器和季集排名。"""

from __future__ import annotations

import logging
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime
from typing import Any

from app import config
from app import database as db
from app.database import media_owner_digest
from app.indexers.models import IndexerMediaSearchRequest
from app.indexers.runtime import get_indexer_service, run_indexer_awaitable_sync
from app.indexers.service import search_payload
from app.modules.episode_audit import (
    audit_series_episodes,
    missing_episode_verification,
)
from app.modules.episode_resource_ranking import (
    matched_episode_items,
    rank_episode_search,
)
from app.modules.media_preferences import (
    RESOURCE_PREFERENCE_FIELDS,
    validate_preference_updates,
)
from app.repositories.agent_download_verification import (
    WORKFLOW_STATE_LABELS,
    SQLiteMissingMediaWorkflowRepository,
)
from app.repositories.media_experience import get_media_preferences

logger = logging.getLogger(__name__)


def _integer(value, name, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} 必须是1到{maximum}的整数")
    return value


def _text(value, name, maximum=120):
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是字符串")
    value = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(value) <= maximum or any(
        unicodedata.category(c).startswith("C") for c in value
    ):
        raise ValueError(f"{name} 必须是1到{maximum}个可见字符")
    return value


def _targets(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 12:
        raise ValueError("items 必须包含1到12部/季")
    unique = {}
    for raw in items:
        if not isinstance(raw, dict) or set(raw) - {
            "query",
            "season",
            "tmdb_id",
            "library_name",
            "episodes",
        }:
            raise ValueError("items只接受query/season/tmdb_id/library_name/episodes")
        item: dict[str, Any] = {
            "query": _text(raw.get("query"), "query"),
            "season": _integer(raw.get("season"), "season", 100),
        }
        if "tmdb_id" in raw:
            identity = raw["tmdb_id"]
            if isinstance(identity, str):
                identity = identity.strip()
            if (
                not isinstance(identity, str)
                or not identity.isascii()
                or not identity.isdigit()
                or not 1 <= len(identity) <= 10
            ):
                raise ValueError("tmdb_id 必须是1到10位数字字符串")
            item["tmdb_id"] = identity
        if raw.get("library_name") not in (None, ""):
            item["library_name"] = _text(raw["library_name"], "library_name", 80)
        if "episodes" in raw:
            episodes = raw["episodes"]
            if not isinstance(episodes, list) or not 1 <= len(episodes) <= 100:
                raise ValueError("episodes 必须包含1到100个集号")
            item["episodes"] = sorted({_integer(n, "episode", 1000) for n in episodes})
        key = (
            item["query"].casefold(),
            item.get("tmdb_id"),
            item["season"],
            item.get("library_name"),
            tuple(item.get("episodes", [])),
        )
        unique.setdefault(key, item)
    return list(unique.values())


class _Budget:
    def __init__(self, count):
        self.deadline = time.monotonic() + 45
        self.remaining = min(24, 6 * count)
        self.lock = threading.Lock()

    def reserve(self, cancelled, used):
        if cancelled():
            return 0, "cancelled"
        with self.lock:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                return 0, "time_budget"
            if self.remaining == 0 or used >= 6:
                return 0, "request_budget"
            self.remaining -= 1
        return min(30, remaining), ""


def _group(item, status, reason):
    return {
        **item,
        "ok": False,
        "status": status,
        "verification": {"verified_missing": False, "resource_search_attempted": False},
        "episodes": [],
        "searches": [],
        "missing_total": None,
        "processed": 0,
        "remaining": None,
        "remaining_episodes": [],
        "failed_episodes": [],
        "search_requests": 0,
        "stop_reason": reason,
    }


def _season(
    item,
    *,
    service,
    as_of,
    sites,
    page,
    maximum,
    per_episode,
    preferences,
    overrides,
    budget,
    cancelled,
    progress,
):
    group = _group(
        item,
        "cancelled" if cancelled() else "timeout",
        "cancelled" if cancelled() else "time_budget",
    )
    if cancelled() or time.monotonic() >= budget.deadline:
        return group
    arguments = {key: value for key, value in item.items() if key != "episodes"}
    arguments["as_of"] = as_of
    if len(item.get("episodes", [])) == 1:
        arguments["target_episode"] = item["episodes"][0]
    audit = audit_series_episodes(arguments, refresh=True)
    group.update(audit=asdict(audit), status=audit.status, stop_reason="")
    data = audit.data
    group["verification"] = {
        "title": data.get("title") or item["query"],
        "tmdb_id": data.get("tmdb_id") or item.get("tmdb_id", ""),
        "season": item["season"],
        "as_of": as_of,
        "library_name": data.get("library_name") or item.get("library_name", ""),
        "verified_missing": False,
        "resource_search_attempted": False,
        "audit_status": audit.status,
    }
    if not audit.ok or audit.status != "updates_available":
        group["ok"] = audit.ok and audit.status == "up_to_date"
        if group["ok"]:
            group.update(missing_total=0, remaining=0, status="not_missing")
        return group
    missing: list[dict[str, int]]
    requested = item.get("episodes")
    # A single exact verdict is authoritative even when the display sample is bounded.
    if requested and len(requested) == 1:
        if data.get("target_missing") is False:
            group.update(ok=True, status="not_missing", missing_total=0, remaining=0)
            return group
        missing = (
            [{"season": item["season"], "episode": requested[0]}]
            if data.get("target_missing") is True
            else []
        )
    elif not data.get("missing_sample_truncated"):
        positions = sorted(
            {
                (r["season"], r["episode"])
                for r in data.get("missing_sample", [])
                if isinstance(r, dict)
                and type(r.get("season")) is int
                and r["season"] == item["season"]
                and type(r.get("episode")) is int
                and 1 <= r["episode"] <= 1000
                and (requested is None or r["episode"] in requested)
            }
        )
        missing = [{"season": s, "episode": e} for s, e in positions]
    else:
        missing = []
    if not missing:
        group["status"] = (
            "inconclusive"
            if data.get("missing_sample_truncated") or requested and len(requested) == 1
            else "not_missing"
        )
        group["ok"] = group["status"] == "not_missing"
        if group["ok"]:
            group.update(missing_total=0, remaining=0)
        return group
    proof = missing_episode_verification(audit, **missing[0])
    if proof is None:
        group["status"] = "inconclusive"
        return group
    group["verification"].update(
        {key: value for key, value in proof.items() if key != "episode"},
        verified_missing=True,
    )
    group.update(
        missing_total=len(missing), remaining=len(missing), remaining_episodes=missing
    )
    selected = missing[
        : maximum if maximum is not None else max(20, len(requested or []))
    ]
    detail = (audit.mapping_context or {}).get("detail") or {}
    request = IndexerMediaSearchRequest.create(
        title=proof["title"],
        original_title=" ".join(str(detail.get("original_name") or "").split())[:120],
        aliases=[item["query"]],
        media_type="tv",
        season=item["season"],
        page=page,
    )
    pool = {}
    processed = {}
    progress(group)

    def search(episode):
        timeout, reason = budget.reserve(cancelled, group["search_requests"])
        if not timeout:
            group["stop_reason"] = reason
            return False
        group["search_requests"] += 1
        group["verification"]["resource_search_attempted"] = True
        current = replace(request, episode=episode)
        raw = {
            "request": asdict(current),
            "items": [],
            "ok": False,
            "status": "disabled",
            "partial": True,
            "has_more": None,
            "complete": False,
            "site_statuses": [],
            "errors": [],
            "truncated": False,
        }
        if config.get_bool("INDEXER_SEARCH_ENABLED"):
            try:
                result = run_indexer_awaitable_sync(
                    service.search_media(
                        current,
                        sites,
                        scope="agent",
                        timeout_seconds=max(0.001, timeout - min(0.25, timeout / 10)),
                    ),
                    timeout_seconds=timeout,
                    cancelled=cancelled,
                )
                raw.update(
                    search_payload(service, result), ok=bool(result.sites_succeeded)
                )
                raw["status"] = (
                    "partial"
                    if raw["partial"]
                    else "success"
                    if raw["items"]
                    else "empty"
                    if raw["ok"]
                    else "unavailable"
                )
            except InterruptedError:
                raw["status"] = "cancelled"
                group["stop_reason"] = "cancelled"
            except Exception as exc:  # noqa: BLE001 - 单次外部查询失败不能清空此前结果。
                logger.warning("缺集资源单次检索失败 type=%s", type(exc).__name__)
                raw.update(
                    status=getattr(
                        exc,
                        "code",
                        "timeout" if isinstance(exc, TimeoutError) else "unavailable",
                    ),
                    errors=[
                        {
                            "code": getattr(exc, "code", "unavailable"),
                            "message": getattr(
                                exc, "public_message", "资源检索当前不可用"
                            ),
                        }
                    ],
                )
        group["searches"].append(raw)
        if group["stop_reason"] == "cancelled":
            progress(group)
            return False
        for candidate in raw["items"]:
            pool.setdefault(candidate["result_id"], candidate)
        for target in selected:
            n = target["episode"]
            previous = processed.get(n)
            if previous and previous["recommended_result_ids"]:
                continue
            # 后续合包可补齐旧空/失败目标，但不能覆盖原查询的失败事实。
            source = previous["search"] if previous else raw
            ranked = rank_episode_search(
                {
                    **source,
                    "items": list(pool.values()),
                    "search_scope": "episode_exact"
                    if n == episode
                    else "season_shared",
                    "exact_search_attempted": n == episode or source.get("exact_search_attempted", False),
                },
                **target,
                preferences=preferences,
                preference_overrides=overrides,
                mapping_context=audit.mapping_context,
                media=request,
            )
            matching = matched_episode_items(ranked)
            if n != episode and not matching:
                continue
            processed[n] = {
                **target,
                "ok": source["ok"],
                "status": source["status"],
                "search": ranked,
                "recommended_result_ids": [
                    r["result_id"] for r in matching[:per_episode]
                ],
            }
        group["episodes"] = [processed[n] for n in sorted(processed)]
        group["processed"] = len(processed)
        group["remaining_episodes"] = [
            r for r in missing if r["episode"] not in processed
        ]
        group["remaining"] = len(group["remaining_episodes"])
        group["failed_episodes"] = [
            {"season": r["season"], "episode": r["episode"]}
            for r in group["episodes"]
            if not r["ok"]
        ]
        group["ok"] = any(r["ok"] for r in group["episodes"])
        group["status"] = "partial"
        progress(group)
        return True

    if len(selected) == 1 or search(None):
        for target in selected:
            if target["episode"] not in processed and not search(target["episode"]):
                break
    group["stop_reason"] = group["stop_reason"] or (
        "max_episodes" if group["remaining"] else ""
    )
    partial = bool(
        group["remaining"]
        or group["failed_episodes"]
        or any(
            r["partial"] or r["truncated"] or not r["complete"]
            for r in group["searches"]
        )
    )
    group["status"] = (
        "partial"
        if partial
        else "success"
        if any(r["recommended_result_ids"] for r in group["episodes"])
        else "empty"
    )
    return group


def _assemble(groups, *, page, items):
    candidates = {}
    chosen = {}
    verifications = []
    referenced_groups = []
    for group in groups:
        referenced = {**group, "searches": [], "episodes": []}
        for search in group["searches"]:
            for item in search["items"]:
                candidates.setdefault(item["result_id"], item)
            referenced["searches"].append({
                **{key: value for key, value in search.items() if key != "items"},
                "result_ids": [item["result_id"] for item in search["items"]],
            })
        for episode in group["episodes"]:
            search = episode["search"]
            # 候选正文只在根 items 保存；逐集排名、偏好和完整来源覆盖独立保留。
            referenced["episodes"].append({
                **episode,
                "search": {
                    **{key: value for key, value in search.items() if key != "items"},
                    "result_ids": [item["result_id"] for item in search["items"]],
                    "quality": {item["result_id"]: item["quality"] for item in search["items"]},
                    "preference_match": {
                        item["result_id"]: item["preference_match"]
                        for item in search["items"] if "preference_match" in item
                    },
                },
            })
            selected = search["recommendation"]["selected"]
            if selected is None:
                continue
            identity = selected["result_id"]
            proof = group["verification"]
            key = proof["tmdb_id"]
            if identity in chosen and chosen[identity] != key:
                continue
            chosen[identity] = key
            verifications.append(
                {
                    "result_id": identity,
                    "query": proof["title"],
                    "tmdb_id": key,
                    "season": episode["season"],
                    "episode": episode["episode"],
                    "as_of": proof["as_of"],
                    "library_name": proof.get("library_name", ""),
                }
            )
        referenced_groups.append(referenced)
    searches = [r for g in groups for r in g["searches"]]
    partial = len(groups) < len(items) or any(
        g["status"] not in {"success", "empty", "not_missing"} for g in groups
    )
    has_more = (
        True
        if any(r["has_more"] is True for r in searches)
        else None
        if not searches or any(r["has_more"] is None for r in searches)
        else False
    )
    status = (
        "cancelled"
        if groups and all(g["stop_reason"] == "cancelled" for g in groups)
        else "partial"
        if partial
        else "not_missing"
        if groups and all(g["status"] == "not_missing" for g in groups)
        else "success"
        if chosen
        else "empty"
    )
    return {
        "ok": any(g["ok"] for g in groups),
        "status": status,
        "summary": f"已核对{len(groups)}部/季，其中{sum(g['verification'].get('resource_search_attempted') is True for g in groups)}部执行资源检索，得到{len(chosen)}项匹配候选",
        "scope": {"owner": "current", "source": "configured_indexers"},
        "page": page,
        "partial": partial,
        "has_more": has_more,
        "complete": not partial and all(r["complete"] for r in searches),
        "truncated": any(
            g["remaining"] or any(r["truncated"] for r in g["searches"]) for g in groups
        ),
        "items": list(candidates.values()),
        "groups": referenced_groups,
        "result_ids": list(chosen),
        "verification_targets": verifications,
    }


def search_missing(
    items: list[dict],
    *,
    owner: str,
    as_of: str | None = None,
    sites: list[str] | None = None,
    page: int = 1,
    max_episodes: int | None = None,
    limit_per_episode: int = 8,
    preference_overrides: dict | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    checkpoint: Callable[[dict], None] | None = None,
) -> dict:
    targets = _targets(items)
    when = (
        datetime.now().astimezone().date()
        if as_of is None
        else date.fromisoformat(_text(as_of, "as_of", 10))
    )
    if when > datetime.now().astimezone().date():
        raise ValueError("as_of 不能晚于今天")
    _integer(page, "page", 1000)
    _integer(limit_per_episode, "limit_per_episode", 10)
    if max_episodes is not None:
        _integer(max_episodes, "max_episodes", 100)
    if preference_overrides is not None and (
        not isinstance(preference_overrides, dict)
        or set(preference_overrides) - set(RESOURCE_PREFERENCE_FIELDS)
    ):
        raise ValueError("preference_overrides只接受资源质量偏好")
    overrides = (
        validate_preference_updates(preference_overrides)
        if preference_overrides is not None
        else None
    )
    if sites is not None and (
        not isinstance(sites, list)
        or len(sites) > 16
        or any(not isinstance(s, str) for s in sites)
    ):
        raise ValueError("sites 必须是最多16个站点ID的数组")
    service = get_indexer_service()
    selected_sites = service.select_sites(sites or None, scope="agent")
    preferences = get_media_preferences(media_owner_digest(owner))
    repo = SQLiteMissingMediaWorkflowRepository()
    budget = _Budget(len(targets))

    snapshots = [None] * len(targets)
    progress_lock = threading.Lock()

    def publish(index, group):
        if checkpoint is not None:
            with progress_lock:
                snapshots[index] = deepcopy(group)
                checkpoint(
                    _assemble(
                        [g for g in snapshots if g is not None],
                        page=page,
                        items=targets,
                    )
                )

    def run(indexed):
        index, item = indexed
        try:
            group = _season(
                item,
                service=service,
                as_of=when.isoformat(),
                sites=selected_sites,
                page=page,
                maximum=max_episodes,
                per_episode=limit_per_episode,
                preferences=preferences,
                overrides=overrides,
                budget=budget,
                cancelled=cancelled,
                progress=lambda group: publish(index, group),
            )
        except Exception as exc:  # noqa: BLE001 - 隔离单部外部失败，保留其余作品及未知范围。
            logger.warning("缺集资源查询未完成 type=%s", type(exc).__name__)
            group = _group(item, "unavailable", "upstream_failure")
        proof = group["verification"]
        if proof.get("verified_missing") and not cancelled():
            episodes = [
                {
                    "episode": e["episode"],
                    "has_candidate": bool(e["search"]["recommendation"]["selected"]),
                }
                for e in group["episodes"]
            ]
            episodes += [
                {"episode": e["episode"], "has_candidate": False}
                for e in group["remaining_episodes"]
            ]
            try:
                group["workflow_id"] = repo.capture_search(
                    owner=owner, verification=proof, episodes=episodes
                )
            except Exception as exc:  # noqa: BLE001 - 进度镜像故障不得覆盖真实检索结果。
                logger.warning("补库检索进度记录失败 type=%s", type(exc).__name__)
        return group

    groups = []
    with ThreadPoolExecutor(
        max_workers=min(3, len(targets)), thread_name_prefix="missing-resources"
    ) as pool:
        for index, group in enumerate(pool.map(run, enumerate(targets))):
            groups.append(group)
            publish(index, group)
    return _assemble(groups, page=page, items=targets)


def missing_workflows(*, owner: str, limit: int = 10, offset: int = 0) -> dict:
    _integer(limit, "limit", 30)
    if type(offset) is not int or offset < 0:
        raise ValueError("offset必须是非负整数")
    repo = SQLiteMissingMediaWorkflowRepository()
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        total = repo.count_for_owner(owner=owner, conn=conn)
        rows = repo.list_for_owner(owner=owner, limit=limit, offset=offset, conn=conn)
    grouped = {}
    for row in rows:
        workflow = grouped.setdefault(
            row["workflow_id"],
            {
                key: row[key]
                for key in (
                    "workflow_id",
                    "source_tool",
                    "title",
                    "tmdb_id",
                    "season",
                    "as_of",
                    "state",
                    "revision",
                    "created_at",
                    "updated_at",
                )
            },
        )
        workflow["state_label"] = WORKFLOW_STATE_LABELS.get(
            row["state"], "需要人工关注"
        )
        workflow.setdefault("items", []).append(
            {
                "state": row["item_state"],
                "state_label": WORKFLOW_STATE_LABELS.get(
                    row["item_state"], "需要人工关注"
                ),
                **{
                    key: row[key]
                    for key in (
                        "item_id",
                        "episode",
                        "item_state",
                        "target",
                        "download_request_id",
                        "last_error_code",
                        "item_updated_at",
                    )
                },
            }
        )
    values = list(grouped.values())
    has_more = offset + len(values) < total
    attention = sum(
        w["state"]
        in {
            "attention",
            "search_ready",
            "selection_required",
            "confirmation_required",
            "stale",
        }
        for w in values
    )
    return {
        "ok": True,
        "status": "attention" if attention else "healthy" if values else "empty",
        "attention": attention,
        "scope": {"owner": "current"},
        "total": total,
        "returned": len(values),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "next_offset": offset + len(values) if has_more else None,
        "complete": not has_more,
        "workflows": values,
    }
