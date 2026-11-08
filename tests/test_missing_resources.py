"""缺集检索公共业务与原生SDK的事实、分页、预算和偏好回归。"""

from __future__ import annotations

import json
import unittest
from dataclasses import fields, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app import sdk
from app.indexers.models import (
    AggregatedIndexerResult,
    IndexerItem,
    IndexerProviderError,
)
from app.indexers.result_store import IndexerResultStore
from app.modules import missing_resources as actions
from app.modules.episode_audit import EpisodeAuditResult
from tests.test_agent_indexer_actions import FakeIndexerService


def _audit_result(
    *,
    status="updates_available",
    ok=True,
    missing=None,
    truncated=False,
    target_missing=None,
    library_name="",
    as_of=None,
    target=None,
):
    target = target or next(
        (item for item in (missing or []) if isinstance(item, dict)),
        {"season": 2, "episode": 3},
    )
    return EpisodeAuditResult(
        ok,
        status,
        "audit",
        data={
            "title": "示例剧",
            "tmdb_id": "12345",
            "as_of": as_of or datetime.now().astimezone().date().isoformat(),
            **(
                {"season": target["season"], "target_episode": target["episode"]}
                if target_missing is not None
                else {}
            ),
            "missing_count": len(missing or []),
            "missing_sample": list(missing or []),
            "missing_sample_truncated": truncated,
            **(
                {"target_missing": target_missing} if target_missing is not None else {}
            ),
            **({"library_name": library_name} if library_name else {}),
            "sources": [{"server_name": "secret-server", "path": "/private/media"}],
        },
        checked_at="2026-08-01T00:00:00+08:00",
        suggestions=["audit suggestion"],
        error="audit safe error" if not ok else "",
    )


def _search_result(
    *,
    ok=True,
    status="success",
    query="示例剧 S02E03",
    items: list[dict[str, Any]] | None = None,
):
    if items is None:
        items = (
            [
                {
                    "result_id": "safe-result-id-1234",
                    "site_id": "nyaa",
                    "site_name": "Nyaa",
                    "title": f"{query} 1080p",
                    "download_state": "ready",
                    "download_kinds": ["magnet"],
                }
            ]
            if ok
            else []
        )
    parsed = []
    for item in items:
        values: dict[str, Any] = {
            "site_id": "nyaa",
            "site_name": "Nyaa",
            **{
                key: value
                for key, value in item.items()
                if key in {field.name for field in fields(IndexerItem)}
            },
        }
        values["download_kinds"] = tuple(values.get("download_kinds", ()))
        parsed.append(IndexerItem(**values))
    return AggregatedIndexerResult(
        query=query,
        page=1,
        items=parsed,
        sites_attempted=("nyaa",),
        sites_succeeded=("nyaa",) if ok else (),
        errors=[],
        partial=status == "partial",
        cached=False,
        has_more=False,
    )


def audit_for(query="Show", season=1, episodes=range(1, 12), *, identity="12345"):
    episodes = list(episodes)
    value = _audit_result(
        missing=[{"season": season, "episode": e} for e in episodes],
        target_missing=True,
    )
    value.data.update(title=query, tmdb_id=identity, season=season)
    value.data.pop("target_episode", None)
    value.mapping_context = {
        "detail": {
            "name": query,
            "seasons": [
                {"season_number": season, "episode_count": max(episodes, default=0)}
            ],
        },
        "season_detail": {
            "season_number": season,
            "episodes": [
                {"episode_number": e, "air_date": "2020-01-01"}
                for e in range(1, max(episodes, default=0) + 1)
            ],
        },
        "as_of": value.data["as_of"],
    }
    return value


@pytest.fixture
def backend(monkeypatch):
    service = FakeIndexerService(result_store=IndexerResultStore())
    monkeypatch.setattr(actions, "get_indexer_service", lambda: service)
    monkeypatch.setattr(actions.config, "get_bool", lambda *_: True)
    monkeypatch.setattr(actions, "get_media_preferences", lambda _: {})
    capture = Mock(return_value="workflow-test-id")
    monkeypatch.setattr(
        actions.SQLiteMissingMediaWorkflowRepository, "capture_search", capture
    )
    audit = Mock(return_value=audit_for())
    monkeypatch.setattr(actions, "audit_series_episodes", audit)

    async def search(request, sites=None, **kwargs):
        items = []
        for n in range(1, 12):
            item = IndexerItem(
                "nyaa",
                "Nyaa",
                f"{request.title}.S{request.season:02d}E{n:02d}.1080p.WEB-DL",
                download_state="ready",
                download_kinds=("magnet",),
                magnet="magnet:?xt=urn:btih:" + f"{n:040x}",
            )
            identity = service.result_store.put(item)
            items.append({**item.to_public_dict(), "result_id": identity})
        return _search_result(query=request.title, items=items)

    service.search_media = AsyncMock(side_effect=search)
    with sdk.call_owner("viewer"):
        yield SimpleNamespace(service=service, audit=audit, capture=capture)


def test_native_unified_method_refreshes_once_and_reuses_shared_search(backend):
    result = sdk.resources.search_missing([{"query": "Show", "season": 1}])
    assert result["ok"] and result["status"] == "success"
    group = result["groups"][0]
    assert (
        group["missing_total"] == group["processed"] == 11 and group["remaining"] == 0
    )
    assert (
        len(result["items"])
        == len(result["result_ids"])
        == len(result["verification_targets"])
        == 11
    )
    identities = {item["result_id"] for item in result["items"]}
    page = group["searches"][0]
    assert "items" not in page and set(page["result_ids"]) == identities
    for episode in group["episodes"]:
        search = episode["search"]
        selected = search["recommendation"]["selected"]
        assert "items" not in search
        assert set(search["result_ids"]) == set(search["quality"]) == identities
        assert search["preference_match"] == {}
        assert search["result_ids"][0] == selected["result_id"]
        assert search["quality"][selected["result_id"]]["match"] == "exact_episode"
        assert search["recommendation"]["target"] == {
            "season": episode["season"], "episode": episode["episode"]
        }
    backend.audit.assert_called_once_with(
        {
            "query": "Show",
            "season": 1,
            "as_of": datetime.now().astimezone().date().isoformat(),
        },
        refresh=True,
    )
    assert (
        backend.service.search_media.call_count == 1
        and backend.service.search_media.call_args.args[0].episode is None
    )
    assert backend.capture.call_args.kwargs["owner"] == "viewer"
    assert len(backend.capture.call_args.kwargs["episodes"]) == 11
    assert "magnet:" not in json.dumps(result)


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{}],
        [{"query": "x", "season": True}],
        [{"query": "x", "season": 0}],
        [{"query": "x", "season": 1, "episode": 2}],
        [{"query": "x", "season": 1, "episodes": []}],
        [{"query": "x", "season": 1, "episodes": [True]}],
        [{"query": "x", "season": 1, "episodes": None}],
        [{"query": "x", "season": 1, "tmdb_id": "../1"}],
        [{"query": "x", "season": 1, "tmdb_id": ""}],
        [{"query": "x\u0000", "season": 1}],
        [{"query": "x", "season": 1}] * 13,
    ],
)
def test_invalid_targets_never_start_audit_or_indexer(backend, items):
    with pytest.raises((TypeError, ValueError)):
        sdk.resources.search_missing(items)
    backend.audit.assert_not_called()
    backend.service.search_media.assert_not_called()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_episodes": 0},
        {"max_episodes": 101},
        {"max_episodes": True},
        {"limit_per_episode": 0},
        {"page": True},
        {"page": 0},
        pytest.param({"page": 101}, id="page_above_indexer_maximum"),
        {"as_of": (datetime.now().astimezone().date() + timedelta(days=1)).isoformat()},
        {"preference_overrides": {"preferred_server": "emby"}},
        {"preference_overrides": {"max_episode_size_gb": -1}},
        {"sites": "nyaa"},
        {"sites": [1]},
    ],
)
def test_invalid_options_never_start_io(backend, kwargs):
    with patch.object(
        actions, "get_indexer_service", wraps=actions.get_indexer_service
    ) as service:
        with pytest.raises((TypeError, ValueError)):
            sdk.resources.search_missing([{"query": "Show", "season": 1}], **kwargs)
    service.assert_not_called()
    backend.audit.assert_not_called()
    backend.service.search_media.assert_not_called()


@pytest.mark.parametrize("verdict", [None, "true", 1, [], {}])
def test_exact_verdict_cannot_be_replaced_by_display_sample(backend, verdict):
    backend.audit.return_value.data["target_missing"] = verdict
    r = sdk.resources.search_missing([{"query": "Show", "season": 1, "episodes": [9]}])
    assert not r["ok"] and r["groups"][0]["status"] == "inconclusive"
    backend.service.search_media.assert_not_called()


@pytest.mark.parametrize(
    "status,ok", [("up_to_date", True), ("inconclusive", True), ("unavailable", False)]
)
def test_audit_failure_or_not_missing_never_searches(backend, status, ok):
    backend.audit.return_value = replace(
        backend.audit.return_value, status=status, ok=ok
    )
    r = sdk.resources.search_missing([{"query": "Show", "season": 1}])
    assert r["groups"][0]["status"] == (
        "not_missing" if status == "up_to_date" else status
    )
    if status == "up_to_date":
        assert r["status"] == "not_missing" and r["has_more"] is None
    backend.service.search_media.assert_not_called()


def test_exact_high_episode_uses_authoritative_verdict_beyond_truncated_sample(backend):
    a = audit_for(episodes=range(1, 101))
    a.data.update(
        target_episode=100, target_missing=True, missing_sample_truncated=True
    )
    backend.audit.return_value = a
    sdk.resources.search_missing([{"query": "Show", "season": 1, "episodes": [100]}])
    assert backend.audit.call_args.args[0]["target_episode"] == 100
    assert backend.service.search_media.call_args.args[0].episode == 100


def test_truncated_season_audit_keeps_unknown_and_no_search(backend):
    backend.audit.return_value.data["missing_sample_truncated"] = True
    r = sdk.resources.search_missing([{"query": "Show", "season": 1}])
    assert r["groups"][0]["missing_total"] is None and not r["complete"]
    backend.service.search_media.assert_not_called()


def test_only_uncovered_targets_get_precise_queries_and_no_match_is_not_exhaustion(
    backend,
):
    backend.audit.return_value = audit_for(episodes=range(1, 12))
    backend.service.search_media.side_effect = lambda *a, **k: _search_result(items=[])
    r = sdk.resources.search_missing([{"query": "Show", "season": 1}])
    g = r["groups"][0]
    assert backend.service.search_media.call_count == 6
    assert [c.args[0].episode for c in backend.service.search_media.call_args_list] == [
        None,
        1,
        2,
        3,
        4,
        5,
    ]
    assert (
        g["processed"] == 5
        and g["remaining"] == 6
        and g["stop_reason"] == "request_budget"
    )
    assert g["remaining_episodes"] == [
        {"season": 1, "episode": n} for n in range(6, 12)
    ]
    assert r["partial"] and r["status"] == "partial" and not r["result_ids"]


def test_source_pagination_and_failure_facts_survive_full_candidate_pool(backend):
    source = backend.service.search_media.side_effect

    async def paged(request, sites=None, **kwargs):
        result = await source(request, sites, **kwargs)
        result.page = request.page
        result.has_more = True
        result.partial = True
        result.sites_attempted = ("nyaa", "broken")
        result.errors = [IndexerProviderError("broken", "timeout", "来源超时")]
        return result

    backend.service.search_media.side_effect = paged
    r = sdk.resources.search_missing(
        [{"query": "Show", "season": 1}], page=3, sites=["nyaa"]
    )
    assert (
        r["partial"] and r["has_more"] and not r["complete"] and len(r["items"]) == 11
    )
    raw = r["groups"][0]["searches"][0]
    assert (
        raw["page"] == 3 and raw["has_more"] and raw["errors"][0]["site_id"] == "broken"
    )
    assert "items" not in raw
    assert set(raw["result_ids"]) == {item["result_id"] for item in r["items"]}
    for episode in r["groups"][0]["episodes"]:
        search = episode["search"]
        assert search["errors"] == raw["errors"]
        assert search["site_statuses"] == raw["site_statuses"]
        assert search["page"] == 3 and search["partial"]
        assert search["complete"] is raw["complete"]
    assert backend.service.search_media.call_args.args[0].page == 3


def test_full_candidate_facts_not_cut_by_recommendation_limit_or_old_fifty_cap(backend):
    backend.audit.return_value = audit_for(episodes=[1])
    items = [
        {
            "result_id": f"result-opaque-id-{n:04d}",
            "title": f"Show.S01E01.1080p.WEB-DL-G{n}",
            "download_state": "ready",
            "download_kinds": ["magnet"],
        }
        for n in range(75)
    ]
    backend.service.search_media.side_effect = lambda *a, **k: _search_result(
        items=items
    )
    r = sdk.resources.search_missing(
        [{"query": "Show", "season": 1, "episodes": [1]}], limit_per_episode=1,
        preference_overrides={"preferred_resolution": "1080p"},
    )
    search = r["groups"][0]["episodes"][0]["search"]
    identities = {item["result_id"] for item in r["items"]}
    assert (
        len(r["items"]) == len(search["result_ids"]) == 75
    )
    assert set(search["quality"]) == set(search["preference_match"]) == identities
    assert all(quality["eligible"] for quality in search["quality"].values())
    assert all(match["eligible"] for match in search["preference_match"].values())
    assert "items" not in search
    assert len(r["groups"][0]["episodes"][0]["recommended_result_ids"]) == 1
    assert (
        r["groups"][0]["episodes"][0]["search"]["recommendation"]["matched_count"] == 75
    )
    assert r["groups"][0]["episodes"][0]["search"]["recommendation"]["matched_counts"] == {
        "exact_episode": 75, "episode_pack": 0,
    }


def test_precise_search_references_keep_earlier_pool_and_each_episode_verdict(backend):
    backend.audit.return_value = audit_for(episodes=[1, 2])

    def search(request, *args, **kwargs):
        number = request.episode or 1
        return _search_result(items=[{
            "result_id": f"episode-candidate-{number:04d}",
            "title": f"Show.S01E{number:02d}.1080p.WEB-DL",
            "download_state": "ready",
            "download_kinds": ["magnet"],
        }])

    backend.service.search_media.side_effect = search
    result = sdk.resources.search_missing([{"query": "Show", "season": 1}])
    group = result["groups"][0]
    first, second = group["episodes"]
    first_id, second_id = (f"episode-candidate-{number:04d}" for number in (1, 2))
    assert [call.args[0].episode for call in backend.service.search_media.call_args_list] == [None, 2]
    assert [page["result_ids"] for page in group["searches"]] == [[first_id], [second_id]]
    assert {item["result_id"] for item in result["items"]} == {first_id, second_id}
    assert first["search"]["result_ids"] == [first_id]
    assert second["search"]["result_ids"] == [second_id, first_id]
    assert first["search"]["quality"][first_id]["match"] == "exact_episode"
    assert second["search"]["quality"][first_id]["match"] == "conflict"
    assert second["search"]["quality"][second_id]["match"] == "exact_episode"
    assert {goal["episode"] for goal in result["verification_targets"]} == {1, 2}


@pytest.mark.parametrize("first_status,covers_first", [("timeout", True), ("empty", True), ("timeout", False)])
def test_later_pack_covers_old_target_without_erasing_search_facts(backend, first_status, covers_first):
    backend.audit.return_value = audit_for(episodes=[1, 2])
    pack_id = "later-pack-result-0001"

    def search(request, *args, **kwargs):
        if request.episode == 1 and first_status == "timeout":
            raise TimeoutError("private upstream timeout")
        return _search_result(items=[{
            "result_id": pack_id,
            "title": f"Show.S01E{'01-02' if covers_first else '02'}.1080p.WEB-DL",
            "download_state": "ready",
            "download_kinds": ["magnet"],
        }] if request.episode == 2 else [])

    backend.service.search_media.side_effect = search
    result = sdk.resources.search_missing([
        {"query": "Show", "season": 1, "episodes": [1, 2]},
    ])
    group = result["groups"][0]
    assert [call.args[0].episode for call in backend.service.search_media.call_args_list] == [None, 1, 2]
    assert result["result_ids"] == [pack_id]
    assert {goal["episode"] for goal in result["verification_targets"]} == ({1, 2} if covers_first else {2})
    first, second = group["episodes"]
    assert first["recommended_result_ids"] == ([pack_id] if covers_first else [])
    assert first["search"]["recommendation"]["matched_count"] == int(covers_first)
    assert second["search"]["recommendation"]["selected"]["result_id"] == pack_id
    assert first["search"]["exact_search_attempted"] is True
    assert first["search"]["search_scope"] == ("season_shared" if covers_first else "episode_exact")
    assert first["status"] == first["search"]["status"] == first_status
    assert group["processed"] == 2 and group["remaining_episodes"] == []
    failed = first_status == "timeout"
    assert first["ok"] is (not failed)
    assert group["failed_episodes"] == ([{"season": 1, "episode": 1}] if failed else [])
    assert result["partial"] is failed and result["complete"] is (not failed)
    assert first["search"]["errors"] == group["searches"][1]["errors"]
    assert group["searches"][1]["status"] == first_status
    assert bool(group["searches"][1]["errors"]) is failed
    assert backend.capture.call_args.kwargs["episodes"] == [
        {"episode": 1, "has_candidate": covers_first}, {"episode": 2, "has_candidate": True},
    ]


@pytest.mark.parametrize("maximum", [None, 2])
def test_query_exception_keeps_delivered_candidates_and_remaining_scope(backend, maximum):
    backend.audit.return_value = audit_for(episodes=[1, 2, 3])

    def search(request, *args, **kwargs):
        if request.episode == 2:
            raise RuntimeError("private upstream https://fixture.invalid/?token=secret")
        number = request.episode or 1
        return _search_result(items=[{
            "result_id": f"kept-episode-result-{number:04d}",
            "title": f"Show.S01E{number:02d}.1080p.WEB-DL",
            "download_state": "ready",
            "download_kinds": ["magnet"],
        }])

    backend.service.search_media.side_effect = search
    snapshots = []
    with sdk.call_owner("viewer", read_checkpoint=snapshots.append):
        result = sdk.resources.search_missing([
            {"query": "Show", "season": 1, "episodes": [1, 2, 3]},
        ], max_episodes=maximum)
    group = result["groups"][0]
    calls = [None, 2, 3] if maximum is None else [None, 2]
    assert [call.args[0].episode for call in backend.service.search_media.call_args_list] == calls
    expected_ids = {f"kept-episode-result-{number:04d}" for number in ([1, 3] if maximum is None else [1])}
    assert set(result["result_ids"]) == {item["result_id"] for item in result["items"]} == expected_ids
    assert group["missing_total"] == 3 and group["processed"] == len(calls)
    assert group["remaining_episodes"] == ([] if maximum is None else [{"season": 1, "episode": 3}])
    assert group["remaining"] == (0 if maximum is None else 1)
    assert group["search_requests"] == len(group["searches"]) == len(calls)
    assert group["failed_episodes"] == [{"season": 1, "episode": 2}]
    assert result["ok"] and result["partial"] and not result["complete"]
    assert group["searches"][1]["status"] == "unavailable"
    assert group["searches"][1]["errors"] == [{"code": "unavailable", "message": "资源检索当前不可用"}]
    delivered = [snapshot for snapshot in snapshots if snapshot["items"]]
    assert delivered and all("kept-episode-result-0001" in snapshot["result_ids"] for snapshot in delivered)
    failed = next(snapshot["groups"][0] for snapshot in delivered if len(snapshot["groups"][0]["searches"]) == 2)
    assert failed["failed_episodes"] == [{"season": 1, "episode": 2}] and failed["status"] == "partial"
    assert snapshots[-1] == result
    assert group["workflow_id"] == "workflow-test-id" and backend.capture.call_count == 1
    assert "private upstream" not in json.dumps(result) and "token=secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "explicit,maximum,expected",
    [(None, None, 20), (list(range(1, 26)), None, 25), ([7, 4], 1, 1)],
)
def test_default_and_explicit_episode_scope_preserves_real_remaining(
    backend, explicit, maximum, expected
):
    backend.audit.return_value = audit_for(episodes=range(1, 31))
    backend.service.search_media.side_effect = lambda request, *a, **k: _search_result(
        items=[
            {
                "result_id": "opaque-pack-result-0001",
                "title": "Show.S01E01-30.1080p",
                "download_state": "ready",
                "download_kinds": ["magnet"],
            }
        ]
    )
    item = {"query": "Show", "season": 1}
    if explicit is not None:
        item["episodes"] = explicit
    r = sdk.resources.search_missing([item], max_episodes=maximum)
    g = r["groups"][0]
    assert (
        g["processed"] == expected
        and g["remaining"] == (30 if explicit is None else len(explicit)) - expected
    )
    assert len(r["result_ids"]) == 1 and len(r["verification_targets"]) == expected


@pytest.mark.parametrize("at", ["before", "audit", "search", "blocked_search"])
def test_cancel_never_starts_next_remote_call_and_retains_scope(backend, at):
    import asyncio
    import threading

    stopped = [at == "before"]
    finished = threading.Event()
    original = backend.service.search_media.side_effect
    if at == "audit":

        def audit(*args, **kwargs):
            stopped[0] = True
            return audit_for()

        backend.audit.side_effect = audit
    if at in {"search", "blocked_search"}:

        async def search(*args, **kwargs):
            if at == "blocked_search":
                stopped[0] = True
                try:
                    await asyncio.Event().wait()
                finally:
                    finished.set()
            value = await original(*args, **kwargs)
            stopped[0] = True
            return value

        backend.service.search_media.side_effect = search
    with sdk.call_owner("viewer", cancelled=lambda: stopped[0]):
        r = sdk.resources.search_missing([{"query": "Show", "season": 1}])
    if at == "before":
        backend.audit.assert_not_called()
    assert backend.service.search_media.call_count == (1 if at in {"search", "blocked_search"} else 0)
    if at in {"audit", "blocked_search"}:
        assert (
            r["groups"][0]["remaining"] == 11
            and r["groups"][0]["stop_reason"] == "cancelled"
        )
    if at == "search":
        assert len(r["result_ids"]) == 11
    if at == "blocked_search":
        assert finished.wait(1)
        assert r["result_ids"] == [] and r["groups"][0]["failed_episodes"] == []
        assert r["groups"][0]["searches"][0]["status"] == "cancelled"
        backend.capture.assert_not_called()


@pytest.mark.parametrize(
    "title",
    ["Other.S01E01.1080p", "Show.S02E01.1080p", "Show.Fin.1080p", "Show.Arc.1080p"],
)
def test_wrong_work_season_and_unknown_coverage_are_never_promoted(backend, title):
    backend.service.search_media.side_effect = lambda *a, **k: _search_result(
        items=[
            {
                "result_id": "unknown-result-id",
                "title": title,
                "download_state": "ready",
                "download_kinds": ["magnet"],
            }
        ]
    )
    r = sdk.resources.search_missing([{"query": "Show", "season": 1, "episodes": [1]}])
    assert (
        len(r["items"]) == 1 and not r["result_ids"] and not r["verification_targets"]
    )


def test_batch_deduplicates_input_and_preserves_every_target_of_shared_pack(backend):
    backend.audit.side_effect = lambda args, **kwargs: audit_for(
        args["query"],
        episodes=[8, 9],
        identity="1" if args["query"] == "First" else "2",
    )
    backend.service.search_media.side_effect = lambda req, *a, **k: _search_result(
        items=[
            {
                "result_id": req.title + "-opaque-pack-result",
                "title": req.title + ".S01E08-09.1080p",
                "download_state": "ready",
                "download_kinds": ["magnet"],
            }
        ]
    )
    items = [{"query": q, "season": 1} for q in ("First", "Second", "First")]
    r = sdk.resources.search_missing(items)
    assert (
        len(r["groups"])
        == backend.audit.call_count
        == backend.service.search_media.call_count
        == 2
    )
    assert len(r["result_ids"]) == 2 and len(r["verification_targets"]) == 4
    assert {(p["tmdb_id"], p["episode"]) for p in r["verification_targets"]} == {
        ("1", 8),
        ("1", 9),
        ("2", 8),
        ("2", 9),
    }


class MissingResourceInventoryFreshnessTests(unittest.TestCase):
    """资源入口运行真实审计/缓存/媒体清单链；仅外部客户端为替身。"""

    def setUp(self):
        from tests.support import isolated_test_database

        self.enterContext(isolated_test_database())
        from app.clients.base import (
            SeriesCandidate,
            SeriesEpisodeInventory,
            SeriesSearchResult,
        )
        from app.modules import episode_audit

        self.audit = episode_audit
        self.audit.reset_episode_audit_cache_for_tests()
        self.addCleanup(self.audit.reset_episode_audit_cache_for_tests)
        self.inventory = []
        self.identity = {"query": "Fixture Show", "tmdb_id": "12345", "season": 1}
        self.as_of = "2020-02-01"
        self.media = Mock()
        self.media.search_series_candidates.return_value = SeriesSearchResult(
            candidates=[
                SeriesCandidate(
                    id="fixture", name="Fixture Show", year="2020", tmdb_id="12345"
                )
            ],
            total=1,
        )
        self.media.find_series_candidates_by_tmdb.return_value = (
            self.media.search_series_candidates.return_value
        )
        self.media.list_series_episode_inventory.side_effect = lambda *_a, **_kw: (
            SeriesEpisodeInventory(
                episodes=list(self.inventory),
                total=len(self.inventory),
            )
        )
        self.sources = [("jellyfin", "Jellyfin", "https://fixture.invalid", self.media)]
        tmdb = Mock()
        tmdb.detail.return_value = {
            "name": "Fixture Show",
            "first_air_date": "2020-01-01",
            "seasons": [{"season_number": 1, "episode_count": 3}],
        }
        tmdb.tv_season_detail.return_value = {
            "season_number": 1,
            "episodes": [
                {"episode_number": episode, "air_date": "2020-01-01"}
                for episode in range(1, 4)
            ],
        }
        for patcher in (
            patch(
                "app.services._configured_media_sources",
                side_effect=lambda: list(self.sources),
            ),
            patch("app.modules.episode_audit.TMDBClient", return_value=tmdb),
            patch(
                "app.modules.missing_resources.get_indexer_service",
                return_value=FakeIndexerService(private_results=False),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(
            actions.get_indexer_service(),
            "search_media",
            new_callable=AsyncMock,
            side_effect=lambda *_a, **_kw: _search_result(status="empty", items=[]),
        )
        self.search = patcher.start()
        self.addCleanup(patcher.stop)

    def _warm_cache(self, mode, inventory):
        self.audit.reset_episode_audit_cache_for_tests()
        self.inventory[:] = inventory
        self.search.reset_mock()
        self.media.list_series_episode_inventory.reset_mock()
        arguments = {**self.identity, "as_of": self.as_of}
        if mode == "single":
            arguments["target_episode"] = 2
        return self.audit.audit_series_episodes(arguments)

    def _search(self, mode):
        items = [{**self.identity, **({"episodes": [2]} if mode == "single" else {})}]
        if mode == "batch":
            items *= 2
        return actions.search_missing(items, owner="freshness-owner", as_of=self.as_of)

    def _assert_freshness(self, mode):
        complete = [(1, episode) for episode in range(1, 4)]
        for before, after in (([], complete), (complete, [])):
            with self.subTest(mode=mode, before=before):
                cached = self._warm_cache(mode, before)
                self.assertEqual(
                    cached.status, "up_to_date" if before else "updates_available"
                )
                self.inventory[:] = after
                result = self._search(mode)
                self.assertEqual(self.media.list_series_episode_inventory.call_count, 2)
                self.assertEqual(
                    self.search.call_count,
                    0 if after else (1 if mode == "single" else 4),
                )
                self.assertEqual(len(result["groups"]), 1)
                group = result["groups"][0]
                self.assertEqual(group["status"], "not_missing" if after else "empty")
                self.assertEqual(
                    group["verification"]["verified_missing"], not bool(after)
                )
                if not after:
                    self.assertEqual(
                        (group["missing_total"], group["processed"]),
                        (1, 1) if mode == "single" else (3, 3),
                    )

    def test_single_search_refreshes_both_cached_inventory_verdicts(self):
        self._assert_freshness("single")

    def test_season_search_refreshes_once_for_all_missing_episodes(self):
        self._assert_freshness("season")

    def test_batch_search_refreshes_once_for_duplicate_series_and_all_episodes(self):
        self._assert_freshness("batch")

    def test_partial_media_source_failure_cannot_reuse_cached_up_to_date(self):
        for mode in ("single", "season", "batch"):
            with self.subTest(mode=mode):
                self.sources[:] = self.sources[:1]
                self._warm_cache(mode, [(1, episode) for episode in range(1, 4)])
                self.inventory.clear()
                failed = Mock()
                failed.search_series_candidates.side_effect = OSError(
                    "fixture unavailable"
                )
                self.sources.append(("emby", "Emby", "https://failed.invalid", failed))
                result = self._search(mode)
                status = result["groups"][0]["status"]
                self.assertEqual(status, "inconclusive")
                self.assertFalse(result["ok"])
                self.search.assert_not_called()
                self.assertEqual(self.media.list_series_episode_inventory.call_count, 2)


def test_successful_requested_page_is_complete_even_when_sources_have_next_page(
    backend,
):
    original = backend.service.search_media.side_effect

    async def paged(*args, **kwargs):
        r = await original(*args, **kwargs)
        r.has_more = True
        return r

    backend.service.search_media.side_effect = paged
    r = sdk.resources.search_missing([{"query": "Show", "season": 1}])
    assert (
        r["status"] == "success"
        and not r["partial"]
        and r["complete"]
        and r["has_more"]
    )


def test_batch_keeps_successful_work_when_another_audit_raises(backend):
    def audit(arguments, **kwargs):
        if arguments["query"] == "Failed":
            raise RuntimeError("secret upstream address")
        return audit_for()

    backend.audit.side_effect = audit
    r = sdk.resources.search_missing(
        [{"query": q, "season": 1} for q in ("Show", "Failed")]
    )
    assert (
        r["ok"]
        and r["partial"]
        and len(r["groups"]) == 2
        and len(r["result_ids"]) == 11
    )
    assert (
        r["groups"][1]["status"] == "unavailable"
        and r["groups"][1]["remaining"] is None
    )
    assert "secret upstream address" not in json.dumps(r)


def test_whole_batch_shares_request_limit_and_audit_time_is_in_deadline(
    backend, monkeypatch
):
    backend.service.search_media.side_effect = lambda *a, **k: _search_result(items=[])
    backend.audit.side_effect = lambda a, **kw: audit_for(
        a["query"], episodes=range(1, 11)
    )
    r = sdk.resources.search_missing(
        [{"query": f"Show-{n}", "season": 1} for n in range(8)]
    )
    assert (
        sum(g["search_requests"] for g in r["groups"])
        == backend.service.search_media.call_count
        == 24
    )
    assert all(
        g["search_requests"] <= 6 and g["remaining"] + g["processed"] == 10
        for g in r["groups"]
    )
    now = [0.0]
    monkeypatch.setattr(actions.time, "monotonic", lambda: now[0])

    def slow_audit(*a, **kw):
        now[0] = 46
        return audit_for()

    backend.audit.side_effect = slow_audit
    backend.service.search_media.reset_mock()
    r = sdk.resources.search_missing([{"query": "Show", "season": 1}])
    assert (
        r["groups"][0]["stop_reason"] == "time_budget"
        and r["groups"][0]["remaining"] == 11
    )
    backend.service.search_media.assert_not_called()


def test_sdk_checkpoints_preserve_complete_delivered_facts_before_next_request(backend):
    snapshots = []
    with sdk.call_owner("viewer", read_checkpoint=snapshots.append):
        r = sdk.resources.search_missing([{"query": "Show", "season": 1}])
    assert snapshots and snapshots[0]["groups"][0]["verification"]["verified_missing"]
    assert snapshots[0]["groups"][0]["remaining"] == 11
    assert (
        snapshots[-1]["items"] == r["items"]
        and snapshots[-1]["verification_targets"] == r["verification_targets"]
    )
    assert snapshots[0]["items"] == []
    for snapshot in snapshots:
        identities = {item["result_id"] for item in snapshot["items"]}
        for group in snapshot["groups"]:
            assert all("items" not in search for search in group["searches"])
            for episode in group["episodes"]:
                search = episode["search"]
                assert "items" not in search
                assert set(search["result_ids"]) == set(search["quality"])
                assert set(search["result_ids"]) <= identities


def test_native_workflow_pages_return_real_ids_and_all_members_without_crossing_owner():
    from app.repositories.agent_download_verification import (
        SQLiteMissingMediaWorkflowRepository,
    )
    from tests.support import isolated_test_database

    with isolated_test_database():
        repository = SQLiteMissingMediaWorkflowRepository()
        for n in range(35):
            proof = {
                "title": f"Show {n}",
                "tmdb_id": str(n + 1),
                "season": 1,
                "as_of": "2026-08-01",
                "verified_missing": True,
            }
            assert repository.capture_search(
                owner="viewer",
                verification=proof,
                episodes=[{"episode": e, "has_candidate": True} for e in range(1, 13)],
            )
        with sdk.call_owner("viewer"):
            first = sdk.resources.missing_workflows(limit=30)
            second = sdk.resources.missing_workflows(
                limit=30, offset=first["next_offset"]
            )
        with sdk.call_owner("other"):
            empty = sdk.resources.missing_workflows()
        assert (
            first["total"] == second["total"] == 35
            and first["returned"] == 30
            and second["returned"] == 5
        )
        assert (
            first["has_more"]
            and not second["has_more"]
            and second["next_offset"] is None
        )
        assert {r["workflow_id"] for r in first["workflows"]}.isdisjoint(
            r["workflow_id"] for r in second["workflows"]
        )
        assert all(
            len(r["items"]) == 12 for r in first["workflows"] + second["workflows"]
        )
        assert all(
            r["items"][0]["item_id"] and r["revision"] == 1 for r in first["workflows"]
        )
        assert empty["status"] == "empty" and empty["total"] == 0


def test_real_indexer_service_keeps_site_page_cursors_when_continuing_native_search(
    backend, monkeypatch
):
    from typing import cast

    from app.indexers.models import IndexerPage
    from app.indexers.providers.base import IndexerAdapter
    from app.indexers.registry import IndexerRegistry
    from app.indexers.service import IndexerService
    from tests.test_indexer_service import FakeAdapter

    provider = FakeAdapter("nyaa")
    seen = []

    async def search(request):
        seen.append(request.page)
        return IndexerPage(
            items=[
                IndexerItem(
                    "nyaa",
                    "Nyaa",
                    f"Show.S01E01.1080p.Page{request.page}",
                    magnet="magnet:?xt=urn:btih:" + f"{request.page:040x}",
                    download_state="ready",
                    download_kinds=("magnet",),
                )
            ],
            page=request.page,
            has_more=request.page == 1,
            pagination_supported=True,
        )

    provider.search = search
    service = IndexerService(
        registry=IndexerRegistry(cast(dict[str, IndexerAdapter], {"nyaa": provider})),
        result_store=IndexerResultStore(),
    )
    monkeypatch.setattr(actions, "get_indexer_service", lambda: service)
    backend.audit.return_value = audit_for(episodes=[1])
    first = sdk.resources.search_missing(
        [{"query": "Show", "season": 1, "episodes": [1]}], sites=["nyaa"]
    )
    status = first["groups"][0]["searches"][0]["site_statuses"][0]
    assert status["has_more"] and status["next_page"] == 2 and first["complete"]
    second = sdk.resources.search_missing(
        [{"query": "Show", "season": 1, "episodes": [1]}],
        sites=["nyaa"],
        page=status["next_page"],
    )
    assert second["groups"][0]["searches"][0]["site_statuses"][0]["next_page"] is None
    assert not second["has_more"] and second["complete"]
    assert {r["result_id"] for r in first["items"]}.isdisjoint(
        r["result_id"] for r in second["items"]
    )
    last = sdk.resources.search_missing(
        [{"query": "Show", "season": 1, "episodes": [1]}],
        sites=["nyaa"], page=100,
    )
    assert last["ok"] and last["status"] == "success" and last["complete"]
    assert last["page"] == last["groups"][0]["searches"][0]["page"] == 100
    assert set(seen) == {1, 2, 100}


@pytest.mark.parametrize("library_name", ["", None])
def test_default_library_scope_and_real_numeric_identity_normalize_before_audit(
    backend, library_name
):
    sdk.resources.search_missing(
        [
            {
                "query": "  Show  ",
                "season": 1,
                "tmdb_id": " 12345 ",
                "library_name": library_name,
                "episodes": [1],
            }
        ]
    )
    args = backend.audit.call_args.args[0]
    assert (
        args["query"] == "Show"
        and args["tmdb_id"] == "12345"
        and "library_name" not in args
    )


def test_workflow_count_and_members_share_one_snapshot_during_concurrent_search(
    monkeypatch,
):
    from app.repositories.agent_download_verification import (
        SQLiteMissingMediaWorkflowRepository,
    )
    from tests.support import isolated_test_database

    repo = SQLiteMissingMediaWorkflowRepository()
    proof = {
        "title": "First",
        "tmdb_id": "1",
        "season": 1,
        "as_of": "2026-08-01",
        "verified_missing": True,
    }
    with isolated_test_database():
        repo.capture_search(
            owner="viewer",
            verification=proof,
            episodes=[{"episode": 1, "has_candidate": True}],
        )
        original = repo.count_for_owner

        def count(*, owner, conn=None):
            total = original(owner=owner, conn=conn)
            # A separate real writer commits after COUNT; the page must stay on the same snapshot.
            repo.capture_search(
                owner=owner,
                verification={**proof, "title": "Second", "tmdb_id": "2"},
                episodes=[{"episode": 2, "has_candidate": True}],
            )
            return total

        monkeypatch.setattr(repo, "count_for_owner", count)
        monkeypatch.setattr(
            actions, "SQLiteMissingMediaWorkflowRepository", lambda: repo
        )
        with sdk.call_owner("viewer"):
            result = sdk.resources.missing_workflows()
        assert result["total"] == result["returned"] == 1 and not result["has_more"]
        assert len(repo.list_for_owner(owner="viewer")) == 2
