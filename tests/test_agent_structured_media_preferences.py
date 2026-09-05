"""结构化偏好的确认、持久化、单次覆盖与真实推荐消费。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from unittest.mock import Mock, patch

import pytest

from app import database as db
from app.agent.action_history import action_history_owner_digest
from app.agent.errors import AgentToolError
from app.agent.library_recommendation_actions import (
    get_library_recommendations,
    library_recommendation_arguments,
)
from app.agent.media_consumption_actions import (
    clear_preferences_confirmed,
    get_preferences,
    preferences_update_arguments,
    prepare_clear_preferences,
    prepare_set_preferences,
    set_preferences_confirmed,
)
from app.agent.media_preference_policy import (
    effective_preferences,
    owner_media_preferences,
    resource_preference_match,
)
from app.agent.models import ToolContext, ToolResult
from app.agent.resource_recommendation import rank_episode_search
from app.modules.media_server_profiles import MediaServerProfile
from app.repositories.media_experience import (
    clear_media_preferences,
    default_media_preferences,
    get_media_preferences,
    set_media_preferences,
)


@pytest.fixture
def preference_db(tmp_path, monkeypatch):
    path = tmp_path / "preferences.sqlite"

    @contextmanager
    def connection():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    monkeypatch.setattr(db, "get_conn", connection)
    with connection() as conn:
        conn.execute("""CREATE TABLE agent_media_preferences (
            owner_digest TEXT PRIMARY KEY,
            preferred_server TEXT NOT NULL DEFAULT 'any',
            preferred_download_target TEXT NOT NULL DEFAULT 'guangya',
            profile_json TEXT NOT NULL DEFAULT '{}',
            revision_token TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    return connection


def _context(owner="viewer"):
    return ToolContext(owner=owner, session_id="preferences-test")


def test_structured_preferences_freeze_then_persist_merge_and_owner_isolation(preference_db):
    context = _context()
    updates = preferences_update_arguments({
        "preferred_resolution": "2160p", "minimum_resolution": "1080p",
        "preferred_hdr": "dolby_vision", "preferred_codecs": ["HEVC", "av1"],
        "preferred_subtitles": ["简中"], "preferred_audio_languages": ["国语"],
        "preferred_release_groups": ["BlackTV", "blacktv"],
        "excluded_keywords": ["枪版"], "max_episode_size_gb": 8,
        "preferred_genres": ["喜剧|Comedy"], "excluded_genres": ["恐怖"],
        "min_rating": 7.5, "exclude_played": True,
    })
    preview, fingerprint = prepare_set_preferences(updates, context)
    assert preview.status == "confirmation_required"
    assert not get_preferences({}, context).data["explicit"]
    assert preview.data["proposed"]["preferred_release_groups"] == ["BlackTV"]
    result = set_preferences_confirmed(updates, fingerprint, context)
    assert result.ok and result.data["preferred_resolution"] == "2160p"
    assert result.data["preferred_codecs"] == ["hevc", "av1"]
    assert get_preferences({}, _context("other")).data["preferred_genres"] == []
    digest = action_history_owner_digest("viewer")
    stored = get_media_preferences(digest)
    assert stored["revision"] == 1
    assert len(stored["revision_token"]) == 32
    assert "revision_token" not in result.data
    assert "revision_token" not in get_preferences({}, context).data
    assert stored["preferred_download_target"] == "guangya"
    with preference_db() as conn:
        saved = json.loads(conn.execute("SELECT profile_json FROM agent_media_preferences").fetchone()[0])
        assert saved["max_episode_size_gb"] == 8
    changed = set_media_preferences(digest, expected_revision=1, updates={"preferred_download_target": "qb"})
    assert changed["revision"] == 2
    assert changed["revision_token"] != stored["revision_token"]
    assert changed["preferred_genres"] == ["喜剧|Comedy"]
    assert set_media_preferences(digest, expected_revision=1, updates={"preferred_resolution": "720p"}) is None
    with pytest.raises(AgentToolError, match="已变化"):
        set_preferences_confirmed(updates, fingerprint, context)
    clear_preview, clear_fingerprint = prepare_clear_preferences({}, context)
    assert clear_preview.data["defaults"]["preferred_genres"] == []
    assert clear_preferences_confirmed({}, clear_fingerprint, context).ok
    assert get_preferences({}, context).data["preferred_resolution"] == "any"


def test_preference_confirmation_detects_changed_args(preference_db):
    _, fingerprint = prepare_set_preferences({"preferred_resolution": "2160p"}, _context())
    with pytest.raises(AgentToolError) as error:
        set_preferences_confirmed({"preferred_resolution": "720p"}, fingerprint, _context())
    assert error.value.code == "confirmation_stale"
    assert not get_preferences({}, _context()).data["explicit"]


@pytest.mark.parametrize("updates", [
    {}, {"unknown": True}, {"preferred_codecs": ["bad"]},
    {"max_episode_size_gb": float("nan")}, {"max_episode_size_gb": True},
    {"max_episode_size_gb": 201}, {"min_rating": float("inf")},
    {"max_episode_size_gb": 10**1000},
    {"exclude_played": "false"}, {"preferred_genres": ["a"] * 13},
    {"preferred_genres": ["a\nb"]}, {"preferred_genres": [123]},
    {"excluded_keywords": "枪版"}, {"preferred_resolution": "8k"},
    {"preferred_genres": ["喜剧|"]},
])
def test_preference_validation_is_bounded(updates):
    with pytest.raises(AgentToolError):
        preferences_update_arguments(updates)


def test_defaults_not_mutable_and_corrupt_storage_has_safe_defaults(preference_db):
    first = default_media_preferences()
    first["preferred_genres"].append("污染")
    assert default_media_preferences()["preferred_genres"] == []
    digest = action_history_owner_digest("viewer")
    set_media_preferences(digest, expected_revision=0, updates={"preferred_genres": ["喜剧"]})
    with preference_db() as conn:
        conn.execute("UPDATE agent_media_preferences SET profile_json=?", ('{"preferred_genres": "bad"}',))
    assert owner_media_preferences("viewer")["preferred_genres"] == []
    with preference_db() as conn:
        conn.execute("UPDATE agent_media_preferences SET profile_json='not json'")
    assert get_media_preferences(digest)["preferred_genres"] == []


def test_local_recommendations_consume_saved_genre_rating_history_and_single_use_overrides(preference_db):
    digest = action_history_owner_digest("viewer")
    set_media_preferences(digest, expected_revision=0, updates={
        "preferred_server": "jellyfin", "preferred_genres": ["喜剧|Comedy"],
        "excluded_genres": ["恐怖|Horror"], "min_rating": 8, "exclude_played": True,
    })
    profile = MediaServerProfile(source="configured:jellyfin", server_type="jellyfin", label="Jellyfin", url="http://example.local", credential="test", enabled=True, user_id="viewer")
    gateway = Mock()
    gateway.query.return_value = ToolResult(True, "completed", "推荐完成", data={"items": []})
    with patch("app.agent.media_consumption_actions.list_configured_profiles", return_value=[profile]), patch("app.agent.library_recommendation_actions.get_provider_gateway", return_value=gateway):
        normalized = library_recommendation_arguments({"media_type": "tv"})
        assert "prefer" not in normalized
        assert get_library_recommendations(normalized, _context()).ok
        arguments = gateway.query.call_args.kwargs["arguments"]
        assert arguments["prefer"] == ["喜剧|Comedy"]
        assert arguments["exclude"] == ["恐怖|Horror"]
        assert arguments["min_rating"] == 8
        override = {"prefer": [], "exclude": [], "min_rating": 0, "exclude_played": False}
        get_library_recommendations(library_recommendation_arguments(override), _context())
        assert all(gateway.query.call_args.kwargs["arguments"][key] == value for key, value in override.items())
    assert owner_media_preferences("viewer")["min_rating"] == 8


def _resource(identifier, title, **kwargs):
    return {"result_id": identifier * 20, "title": title, "download_state": "ready", "download_kinds": ["magnet"], "seeders": 5, **kwargs}


def test_episode_resource_ranking_consumes_profile_and_single_request_wins():
    items = [
        _resource("a", "Show.S01E01.2160p.WEB-DL.HEVC-BlackTV"),
        _resource("b", "Show.S01E01.1080p.WEB-DL.HEVC-GroupB"),
        _resource("c", "Show.S01E02.1080p.WEB-DL.HEVC-GroupB"),
    ]
    preferred = rank_episode_search({"items": items}, season=1, episode=1, preferences={"preferred_resolution": "1080p"})
    assert preferred["items"][0]["result_id"] == "b" * 20
    overridden = rank_episode_search({"items": items}, season=1, episode=1, preferences={"preferred_resolution": "1080p"}, preference_overrides={"preferred_resolution": "2160p"})
    assert overridden["items"][0]["result_id"] == "a" * 20
    assert overridden["download_plan"]["requires_confirmation"] is True
    assert overridden["items"][-1]["quality"]["eligible"] is False
    assert all("quality" not in item for item in items)


def test_resource_hard_limits_size_unknown_and_exclusion_do_not_become_recommended():
    profile = {"minimum_resolution": "1080p", "max_episode_size_gb": 8, "excluded_keywords": ["CAM"]}
    assert not resource_preference_match(_resource("a", "Show.S01E01.720p", size_bytes=1024), profile, single_episode=True)["eligible"]
    assert not resource_preference_match(_resource("a", "Show.S01E01.1080p", size_bytes=9 * 1024**3), profile, single_episode=True)["eligible"]
    assert not resource_preference_match(_resource("a", "Show.S01E01.1080p.CAM", size_bytes=1024), profile, single_episode=True)["eligible"]
    assert not resource_preference_match(_resource("a", "Show.S01E01.1080p"), profile, single_episode=True)["eligible"]
    assert resource_preference_match(_resource("a", "Show.S01.1080p.Complete", size_bytes=100 * 1024**3), profile, single_episode=False)["eligible"]


def test_resource_language_hdr_codec_and_group_markers_add_explainable_bonus():
    match = resource_preference_match(_resource("a", "Show.S01E01.2160p.WEB-DL.DV.H265.简繁英字幕.国语-BlackTV"), {
        "preferred_hdr": "dolby_vision", "preferred_codecs": ["hevc"],
        "preferred_subtitles": ["简体中文"], "preferred_audio_languages": ["国语"],
        "preferred_release_groups": ["BlackTV"],
    })
    assert match["eligible"] and match["score"] > 50
    assert len(match["reasons"]) == 5
    assert effective_preferences({"excluded_keywords": ["CAM"]}, {"excluded_keywords": []})["excluded_keywords"] == []


def test_old_preference_schema_can_be_read_without_runtime_schema_mutation(preference_db):
    with preference_db() as conn:
        conn.execute("DROP TABLE agent_media_preferences")
        conn.execute("""CREATE TABLE agent_media_preferences (
            owner_digest TEXT PRIMARY KEY, preferred_server TEXT,
            preferred_download_target TEXT, revision INTEGER,
            created_at TEXT, updated_at TEXT
        )""")
        conn.execute("INSERT INTO agent_media_preferences VALUES ('owner', 'emby', 'qb', 3, '', '')")
    value = get_media_preferences("owner")
    assert value["preferred_server"] == "emby"
    assert value["revision"] == 3
    assert value["revision_token"] == ""
    assert value["preferred_genres"] == []
    with preference_db() as conn:
        assert "profile_json" not in [row[1] for row in conn.execute("PRAGMA table_info(agent_media_preferences)")]


@pytest.mark.parametrize("whole_season", [False, True])
def test_missing_resource_domain_wrapper_consumes_owner_profile_before_reference_creation(
    preference_db, whole_season,
):
    from app.agent.domain_catalog.library import register_specs

    digest = action_history_owner_digest("viewer")
    set_media_preferences(digest, expected_revision=0, updates={
        "preferred_resolution": "1080p", "excluded_keywords": ["CAM"],
    })
    registry = Mock()
    runtime = Mock()
    register_specs(registry, resource_store=None, active_ingest_store=None,
                   ingest_actions=None, missing_media_runtime=runtime)
    specs = {call.args[0].name: call.args[0] for call in registry.register.call_args_list}
    tool = specs["library.search_missing_season_resources" if whole_season
                 else "library.search_missing_episode_resources"]
    assert "preference_overrides" in tool.parameters["properties"]
    assert "preference_overrides" not in specs["library.search"].parameters["properties"]
    arguments = {"query": "Show", "season": 1, "as_of": "2026-08-01"}
    if not whole_season:
        arguments["episode"] = 1
    audit = ToolResult(True, "updates_available", "确定缺集", data={
        "title": "Show", "tmdb_id": "12345", "missing_count": 1,
        "missing_sample": [{"season": 1, "episode": 1}],
        "missing_sample_truncated": False, "target_missing": True,
    })
    source_items = [
        _resource("a", "Show.S01E01.2160p.WEB-DL.HEVC-GroupA"),
        _resource("b", "Show.S01E01.1080p.WEB-DL.HEVC-GroupB"),
        _resource("c", "Show.S01E01.1080p.CAM-GroupC"),
    ]
    searched = ToolResult(True, "success", "找到三项", data={"items": source_items})
    service = Mock()
    service.result_store.get.return_value = None
    with patch("app.agent.episode_resource_actions.audit_series_episodes", return_value=audit), \
            patch("app.agent.episode_resource_actions.search_resources", return_value=searched), \
            patch("app.agent.episode_resource_actions.get_indexer_service", return_value=service):
        for owner, overrides, expected in (
            ("viewer", None, "b" * 20),
            ("other", None, "a" * 20),
            ("viewer", {"preferred_resolution": "2160p"}, "a" * 20),
        ):
            requested = dict(arguments)
            if overrides is not None:
                requested["preference_overrides"] = overrides
            result = tool.context_handler(tool.validator(requested), _context(owner))
            assert result.ok
            search = (result.data["episodes"][0]["search"] if whole_season
                      else result.data["search"])
            assert search["recommendation"]["selected"]["result_id"] == expected
            snapshot = next(ref.value for ref in result.references if ref.kind == "resource_candidates")
            assert snapshot["candidates"][0]["result_id"] == expected
            assert snapshot["candidates"][0]["position"] == 1
            assert search["items"][0]["position"] == 1
            assert runtime.capture_search.call_args.kwargs["owner"] == owner
            assert runtime.capture_search.call_args.kwargs["result"] is result
            if owner == "viewer":
                assert "c" * 20 not in [candidate["result_id"] for candidate in snapshot["candidates"]]
    assert owner_media_preferences("viewer")["preferred_resolution"] == "1080p"
    assert all("quality" not in item for item in source_items)


@pytest.mark.parametrize("invalid", [
    {"preferred_server": "jellyfin"}, {"max_episode_size_gb": -1},
    {"preferred_genres": ["Comedy"]}, "2160p", None,
])
def test_missing_resource_override_validator_rejects_irrelevant_or_invalid_fields(invalid):
    from app.agent.episode_resource_actions import (
        missing_episode_resource_arguments,
        missing_season_resource_arguments,
    )

    for validator, extra in (
        (missing_episode_resource_arguments, {"episode": 1}),
        (missing_season_resource_arguments, {}),
    ):
        with pytest.raises(AgentToolError):
            validator({"query": "Show", "season": 1,
                       "preference_overrides": invalid, **extra})


@pytest.fixture
def ingest_environment(preference_db):
    from app.agent.ingest_actions import AgentIngestSessionStore, IngestActions
    from app.agent.recent_resource_candidates import RecentResourceCandidateStore

    actions = IngestActions(store=AgentIngestSessionStore(), recent_resource_store=RecentResourceCandidateStore())
    with patch("app.agent.ingest_actions.download_target_readiness",
               side_effect=lambda target: {name: True for name in (["qb", "guangya"] if target == "both" else [target])}), \
            patch("app.agent.ingest_actions.create_request", return_value={"id": 42, "created": True}) as create, \
            patch("app.agent.ingest_actions.dispatch_request",
                  side_effect=lambda number, target: {"ok": True, "status": "submitted", "succeeded": [target], "failed": []}) as dispatch, \
            patch("app.agent.ingest_actions.db.get_download_request", return_value=None):
        yield actions, create, dispatch


def _inspect_direct(actions, owner="viewer"):
    return actions.inspect({"source_type": "direct_url", "input": "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Show.S01E01"}, _context(owner))


def test_ingest_default_target_validator_and_share_protocol_stay_separate():
    from app.agent.ingest_actions import ingest_submit_arguments

    assert ingest_submit_arguments({"source_type": "direct_url"})["target"] == "preferred"
    assert ingest_submit_arguments({"source_type": "resource_candidates", "positions": [1]})["target"] == "preferred"
    assert ingest_submit_arguments({"source_type": "guangya_share", "target": "preferred"})["target"] == "guangya"
    for target in ("qb", "both"):
        with pytest.raises(AgentToolError, match="只能转存"):
            ingest_submit_arguments({"source_type": "guangya_share", "target": target})
    for invalid in ("evil", "", 0, False, None):
        with pytest.raises(AgentToolError):
            ingest_submit_arguments({"source_type": "direct_url", "target": invalid})


def test_ingest_resolves_defaults_per_owner_and_explicit_choice_without_writes(ingest_environment):
    from app.agent.ingest_actions import ingest_submit_arguments

    actions, create, dispatch = ingest_environment
    set_media_preferences(action_history_owner_digest("viewer"), expected_revision=0,
                          updates={"preferred_download_target": "qb"})
    for owner, explicit, expected in (
        ("viewer", None, "qb"), ("other", None, "guangya"),
        ("viewer", "both", "both"), ("viewer", "guangya", "guangya"),
    ):
        _inspect_direct(actions, owner)
        raw = {"source_type": "direct_url"}
        if explicit is not None:
            raw["target"] = explicit
        arguments = ingest_submit_arguments(raw)
        preview, fingerprint = actions.prepare_submit(arguments, _context(owner))
        assert preview.ok and preview.data["target"] == expected
        assert preview.data["backends"] == ({"qb": True, "guangya": True} if expected == "both" else {expected: True})
        assert fingerprint.startswith("ingest-target-v1:") is (explicit is None)
        assert arguments["target"] == (explicit or "preferred")
    create.assert_not_called()
    dispatch.assert_not_called()


def test_ingest_confirm_restores_frozen_target_after_preferences_change_and_service_restart(ingest_environment):
    from app.agent.ingest_actions import (
        AgentIngestSessionStore,
        IngestActions,
        ingest_submit_arguments,
    )
    from app.agent.recent_resource_candidates import RecentResourceCandidateStore

    actions, create, dispatch = ingest_environment
    owner = action_history_owner_digest("viewer")
    set_media_preferences(owner, expected_revision=0, updates={"preferred_download_target": "qb"})
    inspected = _inspect_direct(actions)
    arguments = ingest_submit_arguments({"source_type": "direct_url"})
    arguments["ingest_snapshot"] = inspected.references[0].value
    preview, fingerprint = actions.prepare_submit(arguments, _context())
    assert preview.data["target"] == "qb"
    # 仅模拟领域进程对象重建；资源来自原 Kernel 持久 opaque ref。
    restarted = IngestActions(store=AgentIngestSessionStore(), recent_resource_store=RecentResourceCandidateStore())
    set_media_preferences(owner, expected_revision=1, updates={"preferred_download_target": "guangya"})
    with patch("app.agent.ingest_actions.explicit_preferred_download_target", side_effect=AssertionError("确认阶段不得重读偏好")):
        result = restarted.execute_submit(arguments, fingerprint, _context())
    assert result.ok and result.data["target"] == "qb"
    dispatch.assert_called_once_with(42, "qb")
    create.assert_called_once()


def test_ingest_default_plan_rejects_tampered_target_or_missing_freeze(ingest_environment):
    from app.agent.ingest_actions import ingest_submit_arguments

    actions, create, dispatch = ingest_environment
    _inspect_direct(actions)
    arguments = ingest_submit_arguments({"source_type": "direct_url"})
    _preview, fingerprint = actions.prepare_submit(arguments, _context())
    corrupted = [
        fingerprint.replace(":guangya:", ":qb:"),
        "ingest-target-v1:qb:broken", "ingest-target-v1:other:" + "a" * 64,
        "a" * 64,
    ]
    for modified in corrupted:
        with pytest.raises(AgentToolError) as error:
            actions.execute_submit(arguments, modified, _context())
        assert error.value.code == "confirmation_stale"
    with pytest.raises(AgentToolError):
        actions.execute_submit({**arguments, "target": "qb"}, fingerprint, _context())
    create.assert_not_called()
    dispatch.assert_not_called()


def test_ingest_legacy_explicit_hash_plan_and_unavailable_default_do_not_regress(ingest_environment):
    from app.agent.ingest_actions import ingest_submit_arguments

    actions, create, dispatch = ingest_environment
    _inspect_direct(actions)
    explicit = ingest_submit_arguments({"source_type": "direct_url", "target": "qb"})
    _preview, old_hash = actions.prepare_submit(explicit, _context())
    assert len(old_hash) == 64 and ":" not in old_hash
    with patch("app.agent.ingest_actions.explicit_preferred_download_target", side_effect=AssertionError("显式目标不得读取偏好")):
        assert actions.execute_submit(explicit, old_hash, _context()).ok
    dispatch.assert_called_once_with(42, "qb")
    create.reset_mock()
    dispatch.reset_mock()
    set_media_preferences(action_history_owner_digest("viewer"), expected_revision=0,
                          updates={"preferred_download_target": "qb"})
    with patch("app.agent.ingest_actions.download_target_readiness", return_value={"qb": False}):
        preview, fingerprint = actions.prepare_submit(ingest_submit_arguments({"source_type": "direct_url"}), _context())
    assert not preview.ok and fingerprint == ""
    create.assert_not_called()
    dispatch.assert_not_called()


@pytest.mark.parametrize("positions", [[1], [1, 2]])
def test_ingest_default_resource_single_and_batch_freeze_target_without_ref_position_drift(
    preference_db, positions,
):
    from app.agent.ingest_actions import (
        AgentIngestSessionStore,
        IngestActions,
        ingest_submit_arguments,
    )
    from app.agent.recent_resource_candidates import (
        RecentResourceCandidateStore,
        new_resource_search_id,
        safe_resource_snapshot,
    )

    owner = action_history_owner_digest("viewer")
    set_media_preferences(owner, expected_revision=0, updates={"preferred_download_target": "qb"})
    actions = IngestActions(store=AgentIngestSessionStore(), recent_resource_store=RecentResourceCandidateStore())
    snapshot = safe_resource_snapshot(ToolResult(True, "success", "搜索完成", data={"items": [
        _resource("a", "Show.S01E01.2160p", site_id="nyaa", site_name="Nyaa"),
        _resource("b", "Show.S01E02.2160p", site_id="nyaa", site_name="Nyaa"),
    ]}), search_id=new_resource_search_id())
    arguments = ingest_submit_arguments({"source_type": "resource_candidates", "positions": positions})
    arguments["resource_candidates"] = snapshot
    with patch("app.agent.indexer_candidate_actions.prepare_submit_resource", side_effect=lambda args: (
        ToolResult(True, "confirmation_required", "预检完成", data={"resource": {"title": args["result_id"]}}),
        args["result_id"] + ":" + args["target"],
    )), patch("app.agent.indexer_candidate_actions.submit_resource_confirmed", side_effect=lambda args, expected: ToolResult(
        True, "accepted", "已提交", data={"result_id": args["result_id"], "request_id": 42, "target": args["target"]},
    )) as submit, patch("app.agent.indexer_candidate_actions.prepare_submit_resource_batch", side_effect=lambda args: (
        ToolResult(True, "confirmation_required", "批量预检", data={"resources": [
            {"title": result_id} for result_id in args["result_ids"]
        ]}), ":".join(args["result_ids"]) + ":" + args["target"],
    )), patch("app.agent.indexer_candidate_actions.submit_resource_batch_confirmed", side_effect=lambda args, expected: ToolResult(
        True, "completed", "已批量提交", data={"items": [
            {"result_id": result_id, "target": args["target"], "request_id": 42}
            for result_id in args["result_ids"]
        ]},
    )) as submit_batch:
        preview, frozen = actions.prepare_submit(arguments, _context())
        assert preview.data["target"] == "qb"
        assert arguments["search_id"] == snapshot["search_id"]
        assert arguments["positions"] == positions
        set_media_preferences(owner, expected_revision=1, updates={"preferred_download_target": "both"})
        with patch("app.agent.ingest_actions.explicit_preferred_download_target", side_effect=AssertionError("不得重读偏好")):
            result = actions.execute_submit(arguments, frozen, _context())
        assert result.ok
        if len(positions) == 1:
            submit.assert_called_once_with({"result_id": "a" * 20, "target": "qb"}, "a" * 20 + ":qb")
            submit_batch.assert_not_called()
        else:
            assert submit_batch.call_args.args[0] == {"result_ids": ["a" * 20, "b" * 20], "target": "qb"}
            submit.assert_not_called()


def test_ingest_share_preview_stays_cloud_even_when_saved_target_is_qb(ingest_environment):
    from app.agent.ingest_actions import ingest_submit_arguments

    actions, create, dispatch = ingest_environment
    set_media_preferences(action_history_owner_digest("viewer"), expected_revision=0,
                          updates={"preferred_download_target": "qb"})
    actions.store.capture(
        owner="viewer", conversation_session_id=_context().session_id,
        source_type="guangya_share", public={"items": [{"position": 1, "name": "Show.mkv"}], "target_name": "默认云盘目录"},
        private={"file_ids": ["private-file"], "target_id": "private-directory", "preview_id": "private-preview"},
        identity="share-test",
    )
    with patch("app.agent.ingest_actions.explicit_preferred_download_target", side_effect=AssertionError("分享不应读取qB偏好")):
        arguments = ingest_submit_arguments({"source_type": "guangya_share", "target": "preferred"})
        preview, fingerprint = actions.prepare_submit(arguments, _context())
    assert preview.ok and preview.data["target"] == "guangya"
    assert len(fingerprint) == 64
    create.assert_not_called()
    dispatch.assert_not_called()


def test_kernel_default_target_is_prepared_and_confirmed_without_preferences_tool_call():
    from tests.agent_kernel_test_harness import KernelDomainTestHarness
    from tests.support import isolated_test_database

    with isolated_test_database("structured-default-target-kernel.sqlite"), \
            patch("app.agent.ingest_actions.download_target_readiness", side_effect=lambda target: {target: True}), \
            patch("app.agent.ingest_actions.create_request", return_value={"id": 42, "created": True}) as create, \
            patch("app.agent.ingest_actions.dispatch_request", return_value={"ok": True, "status": "submitted", "succeeded": ["qb"], "failed": []}) as dispatch, \
            patch("app.agent.ingest_actions.db.get_download_request", return_value=None):
        owner = "kernel-preference-viewer"
        digest = action_history_owner_digest(owner)
        set_media_preferences(digest, expected_revision=0, updates={"preferred_download_target": "qb"})
        service = KernelDomainTestHarness()
        inspected = service.invoke("ingest.inspect", {
            "source_type": "direct_url", "input": "magnet:?xt=urn:btih:" + "a" * 40,
        }, owner=owner)
        reference = inspected["result"]["reference_arguments"]["ingest_snapshot_ref"]
        prepared = service.prepare("ingest.submit", {
            "source_type": "direct_url", "ingest_snapshot_ref": reference,
        }, owner=owner)
        assert prepared["mode"] == "confirmation_required"
        assert prepared["result"]["data"]["target"] == "qb"
        plan_id = prepared["action_plan"]["plan_id"]
        plan = service._prepared[plan_id].result.effect_plan
        assert plan.arguments["target"] == "preferred"
        assert "ingest-target-v1:qb:" in plan.snapshot_fingerprint
        create.assert_not_called()
        dispatch.assert_not_called()
        set_media_preferences(digest, expected_revision=1, updates={"preferred_download_target": "guangya"})
        with pytest.raises(AgentToolError):
            service.confirm(plan_id, owner="different-owner")
        with patch("app.agent.ingest_actions.explicit_preferred_download_target", side_effect=AssertionError("确认不得查询偏好")):
            confirmed = service.confirm(plan_id, owner=owner)
        assert confirmed["result"]["ok"]
        assert confirmed["result"]["data"]["target"] == "qb"
        dispatch.assert_called_once_with(42, "qb")
        with pytest.raises(AgentToolError):
            service.confirm(plan_id, owner=owner)
        dispatch.assert_called_once()


def test_resource_preference_tags_support_underscore_delimiters_and_huge_sizes():
    match = resource_preference_match(_resource("a", "Show_S01E01_2160p_DV_AV1_CHS", size_bytes=2**200), {
        "preferred_resolution": "2160p", "minimum_resolution": "1080p",
        "preferred_hdr": "dolby_vision", "preferred_codecs": ["av1"],
        "max_episode_size_gb": 8,
    }, single_episode=True)
    assert match["score"] == 45 + 25 + 18
    assert not match["eligible"]
    assert "超过偏好中的单集大小限制" in match["warnings"]


def _interleave_preference_writer_after_commit(monkeypatch, connection, *, digest, after_sql):
    """确定性调度 B 提交 -> C 独立提交 -> B 返回，复现真实并发交错。"""
    state = {"fired": False, "locked_after_reads": 0}

    @contextmanager
    def interleaved_connection():
        changed = False
        with connection() as conn:
            def trace(sql):
                if sql.startswith("SELECT * FROM agent_media_preferences") and conn.in_transaction and conn.total_changes:
                    state["locked_after_reads"] += 1
            conn.set_trace_callback(trace)
            yield conn
            changed = conn.total_changes > 0
        # connection() 已完成 commit/close，因此 C 是独立事务而非同一写入的一部分。
        if changed and not state["fired"]:
            state["fired"] = True
            with connection() as concurrent:
                concurrent.execute(after_sql, (digest,))

    monkeypatch.setattr(db, "get_conn", interleaved_connection)
    return state


def test_preference_repository_returns_its_own_committed_row_when_another_writer_wins_after_commit(
    preference_db, monkeypatch,
):
    digest = action_history_owner_digest("viewer")
    set_media_preferences(digest, expected_revision=0, updates={"preferred_genres": ["A"]})
    state = _interleave_preference_writer_after_commit(
        monkeypatch, preference_db, digest=digest,
        after_sql="""UPDATE agent_media_preferences SET preferred_download_target='both',
            revision=revision+1, profile_json='{"preferred_genres":["C"]}' WHERE owner_digest=?""",
    )
    written_b = set_media_preferences(digest, expected_revision=1, updates={"preferred_download_target": "qb"})
    current_c = get_media_preferences(digest)
    assert state["fired"] and state["locked_after_reads"] == 1
    assert written_b["revision"] == 2
    assert written_b["preferred_download_target"] == "qb"
    assert written_b["preferred_genres"] == ["A"]
    assert current_c["revision"] == 3
    assert current_c["preferred_download_target"] == "both"
    assert current_c["preferred_genres"] == ["C"]


def test_preference_effect_metadata_freezes_exact_own_revision_not_later_writer(
    preference_db, monkeypatch,
):
    digest = action_history_owner_digest("viewer")
    set_media_preferences(digest, expected_revision=0, updates={"preferred_genres": ["A"]})
    arguments = {"preferred_download_target": "qb"}
    _preview, fingerprint = prepare_set_preferences(arguments, _context())
    _interleave_preference_writer_after_commit(
        monkeypatch, preference_db, digest=digest,
        after_sql="""UPDATE agent_media_preferences SET preferred_download_target='both',
            revision=revision+1 WHERE owner_digest=?""",
    )
    result = set_preferences_confirmed(arguments, fingerprint, _context())
    after = result.effect_metadata["compensation_after"]
    assert result.ok and result.data["preferred_download_target"] == "qb"
    assert after["preferred_download_target"] == "qb" and after["revision"] == 2
    assert after["explicit"]
    assert get_media_preferences(digest)["revision"] == 3
    assert "owner_digest" not in after and "owner" not in after
    assert digest not in repr(result.to_dict())
    assert "compensation_after" not in result.to_dict()
    assert "effect_metadata" not in result.to_model_dict()
    result.data["preferred_genres"].append("UI-only mutation")
    assert after["preferred_genres"] == ["A"]


def test_clear_preferences_metadata_is_known_missing_state_even_if_new_record_is_created(
    preference_db, monkeypatch,
):
    digest = action_history_owner_digest("viewer")
    set_media_preferences(digest, expected_revision=0, updates={"preferred_genres": ["A"]})
    _preview, fingerprint = prepare_clear_preferences({}, _context())
    _interleave_preference_writer_after_commit(
        monkeypatch, preference_db, digest=digest,
        after_sql="""INSERT INTO agent_media_preferences(
            owner_digest,preferred_server,preferred_download_target,profile_json,revision,created_at,updated_at
        ) VALUES(?,'emby','qb','{"preferred_genres":["C"]}',1,'','')""",
    )
    result = clear_preferences_confirmed({}, fingerprint, _context())
    assert result.ok
    after = result.effect_metadata["compensation_after"]
    assert after == {**default_media_preferences(), "revision": 0, "explicit": False}
    current = get_media_preferences(digest)
    assert current["explicit"] and current["preferred_genres"] == ["C"]
    assert "effect_metadata" not in result.to_dict()


def test_preference_nonce_prevents_aba_after_clear_and_same_value_reinsert(preference_db):
    context = _context()
    arguments = {"preferred_download_target": "qb"}
    _, expected = prepare_set_preferences(arguments, context)
    original = set_preferences_confirmed(arguments, expected, context)
    original_after = original.effect_metadata["compensation_after"]
    _, original_clear_expected = prepare_clear_preferences({}, context)
    assert original_after["revision"] == 1 and original_after["revision_token"]

    assert clear_preferences_confirmed({}, original_clear_expected, context).ok
    absent = get_media_preferences(action_history_owner_digest(context.owner))
    assert absent["revision_token"] == "" and not absent["explicit"]
    _, recreated_expected = prepare_set_preferences(arguments, context)
    recreated = set_preferences_confirmed(arguments, recreated_expected, context)
    recreated_after = recreated.effect_metadata["compensation_after"]
    assert recreated_after["revision"] == original_after["revision"] == 1
    assert recreated.data == original.data
    assert recreated_after["revision_token"] != original_after["revision_token"]
    assert recreated_after != original_after
    _, current_clear_expected = prepare_clear_preferences({}, context)
    assert current_clear_expected != original_clear_expected
    with pytest.raises(AgentToolError) as error:
        clear_preferences_confirmed({}, original_clear_expected, context)
    assert error.value.code == "confirmation_stale"
    assert get_media_preferences(action_history_owner_digest(context.owner)) == recreated_after


def test_legacy_empty_preference_nonce_migrates_on_next_write(preference_db):
    with preference_db() as conn:
        conn.execute("""INSERT INTO agent_media_preferences(
            owner_digest, preferred_server, preferred_download_target, revision, created_at, updated_at
        ) VALUES ('legacy', 'emby', 'qb', 4, '', '')""")
    assert get_media_preferences("legacy")["revision_token"] == ""
    updated = set_media_preferences("legacy", expected_revision=4, updates={"preferred_server": "emby"})
    assert updated["revision"] == 5 and len(updated["revision_token"]) == 32
    assert get_media_preferences("legacy") == updated


def test_preference_nonce_cas_rejects_aba_between_snapshot_check_and_write(preference_db):
    original = set_media_preferences("owner", expected_revision=0, updates={"preferred_download_target": "qb"})
    assert clear_media_preferences(
        "owner", expected_revision=original["revision"], expected_revision_token=original["revision_token"],
    )
    recreated = set_media_preferences("owner", expected_revision=0, updates={"preferred_download_target": "qb"})
    assert recreated["revision"] == original["revision"]
    assert not clear_media_preferences(
        "owner", expected_revision=original["revision"], expected_revision_token=original["revision_token"],
    )
    assert set_media_preferences(
        "owner", expected_revision=original["revision"], expected_revision_token=original["revision_token"],
        updates={"preferred_download_target": "both"},
    ) is None
    assert get_media_preferences("owner") == recreated
    assert set_media_preferences(
        "owner", expected_revision=recreated["revision"], expected_revision_token=recreated["revision_token"],
        updates={"preferred_download_target": "both"},
    )["revision"] == 2
