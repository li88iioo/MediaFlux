"""资源卡片的安全快照与冷恢复；不包含模型引用工具或最近候选仓。"""
from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from app.indexers.models import IndexerItem


def capture_candidates(result: dict, arguments: dict, service, search_id: str) -> dict:
    """只记录服务实际返回的完整批次；未下载，不解析链接。"""
    from app.agent.domains.resources.selection import candidate_item
    from app.modules.episode_resource_ranking import matched_episode_items

    goals = deepcopy(result.get("verification_targets") or [])
    proof_by_id = {goal["result_id"]: goal for goal in reversed(goals)}
    candidates, display, private = [], [], []
    seen = set()
    for raw in result.get("items") or []:
        candidate = _safe_generic_candidate(raw)
        if candidate is None or candidate["result_id"] in seen:
            continue
        identity = candidate["result_id"]
        item = service.get_result(identity, scope="agent")
        seen.add(identity)
        position = len(candidates) + 1
        candidates.append({**candidate, "position": position})
        source = deepcopy(raw)
        if proof := proof_by_id.get(identity):
            source["_verification_context"] = {"title": proof["query"],
                **{key: value for key, value in proof.items() if key not in {"result_id", "query"}}}
            # 推荐核验来自 search_missing 的正式目标，不由模型提供。
            for group in result.get("groups") or []:
                for episode in group.get("episodes") or []:
                    selected = episode.get("search", {}).get("recommendation", {}).get("selected")
                    if selected and selected.get("result_id") == identity:
                        source["quality"] = selected.get("quality") or selected
                        break
        display.append(candidate_item(source, position))
        private.append(_serialize_private_item(identity, item))
    query = {key: deepcopy(value) for key, value in (result.get("request") or arguments).items() if key != "page"}
    if "aliases" in query:
        query["aliases"] = list(query["aliases"] or [])
    source = {"query": query, "result_ids": [item["result_id"] for item in candidates], "_display_items": display,
              "pages": [{"page": arguments.get("page", 1),
                **{key: deepcopy(result[key]) for key in ("partial", "truncated", "complete", "has_more", "page") if key in result}}]}
    if recommendation := result.get("recommendation"):
        query.update(recommendation["target"])
        source["matching_ids"] = [item["result_id"] for item in matched_episode_items(result)]
    return {"search_id": search_id,
            "search_status": str(result.get("status") or ("partial" if result.get("partial") else "success")),
            "candidates": candidates,
            "_sources": [source], "_verification_targets": goals,
            "_private_version": 1, "_private_items": private,
            }


def _candidate_facts(snapshot: Mapping[str, Any]) -> dict[str, dict]:
    """判断使用已有冻结原名；展示投影不能截掉季集或画质证据。"""
    titles = {item["result_id"]: item["title"] for item in snapshot.get("_private_items", [])}
    return {item["result_id"]: {**item, "title": titles.get(item["result_id"], item["title"])}
            for item in snapshot["candidates"]}


def candidate_sources(snapshot: Mapping[str, Any]) -> list[dict]:
    """历史快照读取时归一；查询来源与真实ID一起保留，不重建或猜测结果。"""
    coverage = snapshot.get("coverage") or {}
    sources = deepcopy(snapshot.get("_sources", [{"query": {key: value for key, value in snapshot.get("_query_arguments", {}).items() if key != "page"},
             "result_ids": [item["result_id"] for item in snapshot["candidates"]],
             "pages": coverage.get("pages", [coverage] if coverage else [])}]))
    for source in sources:
        if "aliases" in source["query"]:
            source["query"]["aliases"] = list(source["query"]["aliases"] or [])
        if "_display_items" not in source:
            source["_display_items"] = [deepcopy(item) for item in snapshot.get("_display_items", [])
                                        if item["result_id"] in source["result_ids"]]
        query = source["query"]
        if "matching_ids" not in source and query.get("season") is not None and query.get("episode") is not None:
            from app.agent.domains.resources.selection import candidate_item
            from app.indexers.models import IndexerMediaSearchRequest
            from app.modules.episode_resource_ranking import matched_episode_items, rank_episode_search

            media = IndexerMediaSearchRequest.create(**{key: value for key, value in query.items()
                                                       if key in IndexerMediaSearchRequest.__dataclass_fields__})
            ranked = rank_episode_search({"items": [item for item in _candidate_facts(snapshot).values() if item["result_id"] in source["result_ids"]]},
                                         season=query["season"], episode=query["episode"], media=media)
            source["matching_ids"] = [item["result_id"] for item in matched_episode_items(ranked)]
            source["_display_items"] = [candidate_item(item, position) for position, item in enumerate(ranked["items"], 1)]
    return sources


def merge_candidates(previous: dict | None, current: dict) -> dict:
    """保留会话实际观察与各自查询；展示和冻结仍由明确的ID选择触发。"""
    merged = deepcopy(current)
    sources = candidate_sources(previous) if previous else []
    for source in candidate_sources(current):
        same = next((item for item in sources if item["query"] == source["query"]), None)
        if same is None:
            sources.append(source)
        else:
            display = {item["result_id"]: item for item in same.get("_display_items", [])}
            display.update((item["result_id"], item) for item in source.get("_display_items", []))
            same["_display_items"] = list(display.values())
            if "matching_ids" in source:
                same["matching_ids"] = list(dict.fromkeys(
                    [identity for identity in same.get("matching_ids", []) if identity not in source["result_ids"]]
                    + source["matching_ids"]))
            same["result_ids"] = list(dict.fromkeys(same["result_ids"] + source["result_ids"]))
            same["pages"].extend(source["pages"])
    merged["_sources"] = sources
    merged.pop("_query_arguments", None)
    merged.pop("coverage", None)
    merged.pop("_display_items", None)
    if previous is None:
        return merged
    for key in ("candidates", "_private_items"):
        by_id = {item["result_id"]: deepcopy(item) for item in previous[key]}
        for item in current[key]:
            by_id[item["result_id"]] = deepcopy(item)
        merged[key] = list(by_id.values())
        if key != "_private_items":
            for position, item in enumerate(merged[key], 1):
                item["position"] = position
    goals = deepcopy(previous["_verification_targets"])
    for goal in current["_verification_targets"]:
        if goal not in goals:
            goals.append(deepcopy(goal))
    merged["_verification_targets"] = goals
    return merged


def reranked_candidates(snapshot: dict | None, result: dict) -> dict | None:
    """重排只补充已有查询的季集判断；来源覆盖、下载引用和核验目标沿用宿主事实。"""
    from app.agent.domains.resources.selection import candidate_item
    from app.modules.episode_resource_ranking import matched_episode_items

    if snapshot is None:
        return None
    query = {key: value for key, value in result["request"].items() if key != "page"}
    if "aliases" in query:
        query["aliases"] = list(query["aliases"] or [])
    known = _candidate_facts(snapshot)
    source = next((source for source in candidate_sources(snapshot)
                   if source["query"] == query), None)
    items = [item for item in result["items"] if source and item.get("result_id") in source["result_ids"]]
    if not items or any(known[item["result_id"]]["title"] != item["title"] for item in items):
        return None
    assert source is not None
    ids = {item["result_id"] for item in items}
    source["query"].update(result["recommendation"]["target"])
    source["result_ids"] = [item["result_id"] for item in items]
    source["matching_ids"] = [item["result_id"] for item in matched_episode_items({"items": items})]
    source["_display_items"] = [candidate_item({**known[item["result_id"]], "quality": item["quality"]}, position)
                                for position, item in enumerate(items, 1)]
    return {**snapshot, "_sources": [source],
            **{key: [deepcopy(item) for item in snapshot[key] if item["result_id"] in ids]
               for key in ("candidates", "_private_items", "_verification_targets")}}


def select_candidates(snapshot: Mapping[str, Any] | None, result_ids: list[str], specifications: dict) -> dict:
    """选择会话已观察ID；逐来源保留规格，空列表明确撤卡。"""
    from app.indexers.ranking import filter_technical_candidates, technical_filter_arguments

    if (not isinstance(result_ids, list) or len(result_ids) > MAX_RESOURCE_CANDIDATES
            or any(not isinstance(identity, str) or not _RESULT_ID_PATTERN.fullmatch(identity) for identity in result_ids)
            or len(set(result_ids)) != len(result_ids)):
        raise ValueError(f"result_ids 必须为0到{MAX_RESOURCE_CANDIDATES}个不重复的真实资源ID")
    specifications = technical_filter_arguments(specifications)
    if not result_ids:
        return {"ok": True, "status": "empty", "summary": "本轮不展示资源候选", "result_ids": []}
    if snapshot is None:
        raise ValueError("当前会话没有有效搜索批，请先查询资源")
    by_id = _candidate_facts(snapshot)
    if not set(result_ids).issubset(by_id):
        raise ValueError("result_ids 必须全部来自当前会话实际读取的资源搜索结果")
    queries, allowed, considered = [], set(), set()
    for source in candidate_sources(snapshot):
        identities = [identity for identity in result_ids if identity in source["result_ids"]]
        if not identities:
            continue
        query = source["query"]
        inherited = technical_filter_arguments(query)
        if any(inherited.get(key) and value and inherited[key] != value for key, value in specifications.items()):
            continue
        considered.update(identities)
        quality = {**specifications, **{key: value for key, value in inherited.items() if value and not specifications.get(key)}}
        filtered, technical_filter = filter_technical_candidates([by_id[identity] for identity in identities], quality)
        if "matching_ids" in source:
            filtered = [item for item in filtered if item["result_id"] in source["matching_ids"]]
        allowed.update(item["result_id"] for item in filtered)
        queries.append({key: deepcopy(value) for key, value in source.items() if key not in {"_display_items", "matching_ids"}})
        queries[-1].update(query={**query, **quality},
                           result_ids=[item["result_id"] for item in filtered], technical_filter=technical_filter)
        if query != queries[-1]["query"]:
            queries[-1]["source_query"] = query
    if considered != set(result_ids):
        raise ValueError("展示不能替换原搜索规格；请按用户要求重新搜索")
    selected = [by_id[identity] for identity in result_ids if identity in allowed]
    pages = [page for query in queries for page in query["pages"]]
    incomplete = unread = False
    for query in queries:
        observed = {page["page"]: page for page in query["pages"] if isinstance(page.get("page"), int)}
        if observed:
            incomplete |= any(page.get("partial") or page.get("truncated") or page.get("complete") is False for page in observed.values())
            last = max(observed)
            unread |= bool(observed[last].get("has_more")) or len(observed) != last
    limitations = (["部分来源返回不完整"] if incomplete else []) + (["尚有未读取页"] if unread else [])
    notice = "；".join(limitations) + "，结果仅覆盖已返回范围。" if limitations else ""
    return {"ok": True, "status": "found" if selected else "empty",
            "summary": f"已筛选{len(selected)}项资源候选" if selected else "本轮不展示资源候选",
            "result_ids": [item["result_id"] for item in selected],
            "verification_targets": [deepcopy(goal) for goal in snapshot.get("_verification_targets", [])
                                     if goal["result_id"] in {item["result_id"] for item in selected}],
            "effective_query": queries[0]["query"] if len(queries) == 1 else {},
            **({"technical_filter": queries[0]["technical_filter"]} if len(queries) == 1 else {}),
            "queries": queries, "coverage_notice": notice,
            "excluded_count": len(result_ids) - len(selected),
            # complete 仅指所选ID覆盖；搜索覆盖逐页保留，不能由末页或选择数量推断。
            "scope": {"candidate_count": len(by_id), "considered_count": len(result_ids),
                      "pages": pages, "complete": len(result_ids) == len(by_id)}}


def presentation_snapshot(scope: dict, selection: dict) -> dict:
    """每张卡只冻结明确选中的ID，并给它们独立、连续的展示编号。"""
    identities = selection["result_ids"]
    order = {identity: position for position, identity in enumerate(identities, 1)}
    snapshot = {key: deepcopy(value) for key, value in scope.items() if key not in {"coverage", "_sources"}}
    snapshot["candidates"] = [{**deepcopy(item), "position": order[item["result_id"]]}
        for item in scope["candidates"] if item["result_id"] in order]
    snapshot["candidates"].sort(key=lambda item: item["position"])
    display = {}
    sources = candidate_sources(scope)
    snapshot["_sources"] = []
    for query in selection["queries"]:
        for source in sources:
            if source["query"] == query.get("source_query", query["query"]):
                chosen = [{**deepcopy(item), "position": order[item["result_id"]]}
                          for item in source["_display_items"] if item["result_id"] in query["result_ids"]]
                display.update((item["result_id"], item) for item in chosen)
                frozen = {**source, "result_ids": list(query["result_ids"]), "_display_items": chosen}
                if "matching_ids" in frozen:
                    frozen["matching_ids"] = [identity for identity in frozen["matching_ids"] if identity in query["result_ids"]]
                snapshot["_sources"].append(frozen)
    snapshot["_display_items"] = list(display.values())
    snapshot["_display_items"].sort(key=lambda item: item["position"])
    private = {item["result_id"]: item for item in scope["_private_items"]}
    snapshot["_private_items"] = [deepcopy(private[identity]) for identity in identities]
    snapshot["_verification_targets"] = [deepcopy(goal) for goal in scope["_verification_targets"] if goal["result_id"] in order]
    snapshot["_query_arguments"] = deepcopy(selection["effective_query"])
    return snapshot


def restore_resource_candidate_reference(value: Any) -> dict | None:
    """原选择凭证冷恢复资源句柄；新旧已保存卡片均可读取。"""
    if not isinstance(value, dict):
        return None
    snapshot = validate_safe_resource_snapshot({key: value.get(key) for key in ("search_id", "search_status", "candidates")})
    if snapshot is None:
        return None
    if "_private_version" in value:
        raw = value.get("_private_items")
        if value["_private_version"] != 1 or not isinstance(raw, list) or len(raw) != len(snapshot["candidates"]):
            return None
        restored = [(candidate["result_id"], _deserialize_private_item(item, expected_result_id=candidate["result_id"]))
                    for candidate, item in zip(snapshot["candidates"], raw, strict=True)]
        if any(item is None for _, item in restored):
            return None
        from app.indexers.runtime import get_indexer_service
        store = get_indexer_service().result_store
        for identity, item in restored:
            assert item is not None
            store.restore(identity, item)
    if "_verification_targets" in value:
        snapshot["_verification_targets"] = deepcopy(value["_verification_targets"])
    return snapshot

_RESULT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


_SEARCH_ID_PATTERN = re.compile(r"^rs_[A-Za-z0-9_-]{16,64}$")


_ALLOWED_CONFIDENCE = {"high", "medium", "low"}


_ALLOWED_MATCH = {"exact_episode", "episode_pack", "season_pack", "unknown"}


_ALLOWED_DOWNLOAD_STATE = {"ready", "resolvable"}


_ALLOWED_DOWNLOAD_KINDS = {"magnet", "torrent"}


MAX_RESOURCE_CANDIDATES = 50


_PRIVATE_ITEM_KEYS = frozenset(
    {
        "result_id",
        "site_id",
        "site_name",
        "title",
        "detail_url",
        "category",
        "size_text",
        "size_bytes",
        "seeders",
        "leechers",
        "downloads",
        "published_at",
        "download_state",
        "download_kinds",
        "magnet",
        "torrent_url",
        "relevance_score",
        "match_reasons",
        "cluster_id",
        "cluster_size",
    }
)


def normalize_resource_search_id(value: Any) -> str:
    """仅接受本服务签发的搜索快照标识；空值表示未指定。"""
    search_id = str(value or "").strip()
    return search_id if _SEARCH_ID_PATTERN.fullmatch(search_id) else ""


def _serialize_private_item(result_id: str, item: IndexerItem) -> dict[str, Any]:
    return {
        "result_id": result_id,
        "site_id": item.site_id,
        "site_name": item.site_name,
        "title": item.title,
        "detail_url": item.detail_url,
        "category": item.category,
        "size_text": item.size_text,
        "size_bytes": item.size_bytes,
        "seeders": item.seeders,
        "leechers": item.leechers,
        "downloads": item.downloads,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "download_state": item.download_state,
        "download_kinds": list(item.download_kinds),
        "magnet": item.magnet,
        "torrent_url": item.torrent_url,
        "relevance_score": item.relevance_score,
        "match_reasons": list(item.match_reasons),
        "cluster_id": item.cluster_id,
        "cluster_size": item.cluster_size,
    }


def _deserialize_private_item(
    value: Any, *, expected_result_id: str
) -> IndexerItem | None:
    if not isinstance(value, dict) or set(value) != _PRIVATE_ITEM_KEYS:
        return None
    if str(value.get("result_id") or "") != expected_result_id:
        return None
    published_at = value.get("published_at")
    if published_at is not None:
        if not isinstance(published_at, str) or len(published_at) > 64:
            return None
        try:
            published_at = datetime.fromisoformat(published_at)
        except ValueError:
            return None
    raw_download_kinds = value.get("download_kinds")
    raw_match_reasons = value.get("match_reasons")
    if not isinstance(raw_download_kinds, list) or not isinstance(
        raw_match_reasons, list
    ):
        return None
    try:
        return IndexerItem(
            site_id=str(value.get("site_id") or ""),
            site_name=str(value.get("site_name") or ""),
            title=str(value.get("title") or ""),
            detail_url=_optional_private_text(value.get("detail_url"), 8192),
            category=_optional_private_text(value.get("category"), 240),
            size_text=_optional_private_text(value.get("size_text"), 64),
            size_bytes=_optional_int(value.get("size_bytes"), minimum=0),
            seeders=_optional_int(value.get("seeders"), minimum=0),
            leechers=_optional_int(value.get("leechers"), minimum=0),
            downloads=_optional_int(value.get("downloads"), minimum=0),
            published_at=published_at,
            download_state=str(value.get("download_state") or ""),
            download_kinds=tuple(str(item) for item in raw_download_kinds),
            magnet=_optional_private_text(value.get("magnet"), 65_536),
            torrent_url=_optional_private_text(value.get("torrent_url"), 8192),
            relevance_score=_optional_int(
                value.get("relevance_score"), minimum=0, maximum=100
            ),
            match_reasons=tuple(str(item)[:240] for item in raw_match_reasons[:16]),
            cluster_id=_optional_private_text(value.get("cluster_id"), 160),
            cluster_size=_optional_int(
                value.get("cluster_size"), minimum=1, maximum=10_000
            )
            or 1,
        )
    except (TypeError, ValueError):
        return None


def _optional_private_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("invalid private text")
    return value


def _optional_int(
    value: Any, *, minimum: int, maximum: int = 2**63 - 1
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("invalid private integer")
    if not minimum <= value <= maximum:
        raise ValueError("private integer out of range")
    return value


def _safe_generic_candidate(
    value: Any, *, require_download_kinds: bool = True
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result_id = str(value.get("result_id") or "").strip()
    title = _safe_text(value.get("title"), 300)
    download_state = str(value.get("download_state") or "").strip().lower()
    raw_kinds = value.get("download_kinds")
    download_kinds = (
        sorted(
            {
                str(item or "").strip().lower()
                for item in raw_kinds
                if str(item or "").strip().lower() in _ALLOWED_DOWNLOAD_KINDS
            }
        )
        if isinstance(raw_kinds, list)
        else []
    )
    if (
        not _RESULT_ID_PATTERN.fullmatch(result_id)
        or not title
        or download_state not in _ALLOWED_DOWNLOAD_STATE
        or (require_download_kinds and not download_kinds)
    ):
        return None
    subscription_number = _safe_positive_int(
        value.get("subscription_number"), maximum=2_147_483_647
    )
    return {
        "result_id": result_id,
        "title": title,
        "site_id": _safe_text(value.get("site_id"), 32),
        "site_name": _safe_text(value.get("site_name"), 80),
        "size_text": _safe_text(value.get("size_text"), 32),
        "download_state": download_state,
        "download_kinds": download_kinds,
        "media_title": _safe_text(value.get("media_title"), 160),
        "episode_label": _safe_text(value.get("episode_label"), 24),
        "subscription_number": subscription_number,
        "_verification_context": None,
    }


_ALLOWED_QUALITY_TAGS = frozenset(
    {
        "resolution",
        "media",
        "video_codec",
        "effect",
        "audio",
    }
)


def _safe_quality_messages(value: Any, *, maximum: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value[:maximum]:
        text = _safe_text(raw, 120)
        if text and text not in result:
            result.append(text)
    return result


def _safe_quality_tags(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key in sorted(_ALLOWED_QUALITY_TAGS):
        text = _safe_text(value.get(key), 64)
        if text:
            result[key] = text
    return result


def _safe_candidate(
    value: Any,
    *,
    season: int,
    episode: int,
    episode_label: str,
    verification_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result_id = str(value.get("result_id") or "").strip()
    title = _safe_text(value.get("title"), 300)
    download_state = str(value.get("download_state") or "").strip()
    confidence = str(value.get("confidence") or "").strip()
    match = str(value.get("match") or "").strip()
    rank = _safe_positive_int(value.get("rank"), maximum=50)
    score = _safe_int(value.get("score"), minimum=-1000, maximum=1000)
    if (
        not _RESULT_ID_PATTERN.fullmatch(result_id)
        or not title
        or download_state not in _ALLOWED_DOWNLOAD_STATE
        or confidence not in _ALLOWED_CONFIDENCE
        or match not in _ALLOWED_MATCH
        or rank is None
        or score is None
    ):
        return None
    return {
        "season": season,
        "episode": episode,
        "episode_label": episode_label,
        "result_id": result_id,
        "title": title,
        "site_id": _safe_text(value.get("site_id"), 32),
        "site_name": _safe_text(value.get("site_name"), 80),
        "rank": rank,
        "score": score,
        "confidence": confidence,
        "match": match,
        "download_state": download_state,
        "reasons": _safe_quality_messages(value.get("reasons"), maximum=6),
        "warnings": _safe_quality_messages(value.get("warnings"), maximum=4),
        "tags": _safe_quality_tags(value.get("tags")),
        "_verification_context": verification_context,
    }


_GENERIC_PERSISTED_KEYS = frozenset(
    {
        "position",
        "result_id",
        "title",
        "site_id",
        "site_name",
        "size_text",
        "download_state",
        "_verification_context",
        "download_kinds",
        "media_title",
        "episode_label",
        "subscription_number",
    }
)


_EPISODIC_PERSISTED_KEYS = frozenset(
    {
        "position",
        "season",
        "episode",
        "episode_label",
        "result_id",
        "title",
        "site_id",
        "site_name",
        "rank",
        "score",
        "confidence",
        "match",
        "download_state",
        "_verification_context",
        "reasons",
        "warnings",
        "tags",
    }
)


def validate_safe_resource_snapshot(value: Any) -> dict[str, Any] | None:
    """严格验证持久化资源候选投影，拒绝额外字段或被篡改的句柄。"""
    if not isinstance(value, dict) or set(value) != {
        "search_id",
        "search_status",
        "candidates",
    }:
        return None
    search_id = normalize_resource_search_id(value.get("search_id"))
    if not search_id:
        return None
    search_status = _safe_text(value.get("search_status"), 40)
    raw_candidates = value.get("candidates")
    if (
        search_status != value.get("search_status")
        or not isinstance(raw_candidates, list)
        or len(raw_candidates) > MAX_RESOURCE_CANDIDATES
    ):
        return None
    candidates: list[dict[str, Any]] = []
    seen_result_ids: set[str] = set()
    for expected_position, raw in enumerate(raw_candidates, start=1):
        if not isinstance(raw, dict):
            return None
        keys = frozenset(raw)
        if keys == _GENERIC_PERSISTED_KEYS:
            result_id = str(raw.get("result_id") or "").strip()
            projected = _safe_generic_candidate(
                raw,
                require_download_kinds=True,
            )
            if projected is None:
                return None
            projected["position"] = expected_position
            if raw != projected:
                return None
        elif keys == _EPISODIC_PERSISTED_KEYS:
            season = _safe_positive_int(raw.get("season"), maximum=100)
            episode = _safe_positive_int(raw.get("episode"), maximum=1000)
            if season is None or episode is None:
                return None
            verification = raw.get("_verification_context")
            if verification is not None:
                if not isinstance(verification, dict) or set(verification) not in (
                    {"title", "tmdb_id", "season", "episode", "as_of"},
                    {"title", "tmdb_id", "season", "episode", "as_of", "library_name"},
                ):
                    return None
                safe_verification = _safe_verification_context(
                    verification,
                    season=season,
                    episode=episode,
                    require_verified_missing=False,
                )
                if safe_verification != verification:
                    return None
            else:
                safe_verification = None
            projected = _safe_candidate(
                raw,
                season=season,
                episode=episode,
                episode_label=_safe_text(raw.get("episode_label"), 24),
                verification_context=safe_verification,
            )
            if projected is None:
                return None
            projected["position"] = expected_position
            if raw != projected:
                return None
            result_id = projected["result_id"]
        else:
            return None
        if result_id in seen_result_ids:
            return None
        seen_result_ids.add(result_id)
        candidates.append(projected)
    return {
        "search_id": search_id,
        "search_status": search_status,
        "candidates": candidates,
    }


def _safe_verification_context(
    value: Any,
    *,
    season: int,
    episode: int,
    require_verified_missing: bool = True,
) -> dict[str, Any] | None:
    if not isinstance(value, dict) or (
        require_verified_missing and value.get("verified_missing") is not True
    ):
        return None
    title = _safe_text(value.get("title"), 120)
    tmdb_id = str(value.get("tmdb_id") or "").strip()
    as_of = str(value.get("as_of") or "").strip()
    if (
        not title
        or not tmdb_id.isascii()
        or not tmdb_id.isdigit()
        or not 1 <= len(tmdb_id) <= 10
    ):
        return None
    try:
        parsed_as_of = date.fromisoformat(as_of)
    except ValueError:
        return None
    if parsed_as_of > datetime.now().astimezone().date():
        return None
    context = {
        "title": title,
        "tmdb_id": tmdb_id,
        "season": season,
        "episode": episode,
        "as_of": parsed_as_of.isoformat(),
    }
    library_name = _safe_text(value.get("library_name"), 80)
    if library_name:
        context["library_name"] = library_name
    return context


def _safe_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(unicodedata.normalize("NFKC", value).split())
    if not text or any(unicodedata.category(char).startswith("C") for char in text):
        return ""
    return text[:maximum]


def _safe_positive_int(value: Any, *, maximum: int) -> int | None:
    parsed = _safe_int(value, minimum=1, maximum=maximum)
    return parsed if parsed is not None else None


def _safe_int(value: Any, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if minimum <= parsed <= maximum else None
