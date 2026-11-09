"""结构化偏好的确认、持久化、单次覆盖与真实推荐消费。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from app import database as db
from app.database import media_owner_digest

from app.modules.media_preferences import owner_media_preferences
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import EffectExecutor
from app.agent.kernel.ports.sdk_effects import download_submit_tool
from app.agent.kernel.state import InMemorySessionStateStore
from tests.test_agent_kernel_existing_ports import context_for
from app.agent.models import ToolContext
from app.modules.episode_resource_ranking import rank_episode_search
from app.modules.media_preferences import (
    apply_media_preferences_change,
    effective_preferences,
    prepare_media_preferences_change,
    media_preferences_preview,
    public_media_preferences,
    resource_preference_match,
    validate_preference_updates,
)
from app.modules.media_server_profiles import MediaServerProfile
from app.modules.share_transfer import ShareTransferPreviewStore
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


def test_shared_preference_change_is_read_only_until_apply_and_preserves_explicit_fields(preference_db):
    owner = "viewer-key"
    updates = {"preferred_resolution": "2160p", "memory_notes": [" 喜欢原声 ", "喜欢原声"]}
    original = get_media_preferences(owner)
    prepared = prepare_media_preferences_change(owner, updates)
    assert json.loads(json.dumps(prepared, ensure_ascii=False)) == prepared
    assert prepared["current"] == get_media_preferences(owner) == original
    assert prepared["proposed"]["minimum_resolution"] == "any"
    assert prepared["proposed"]["memory_notes"] == ["喜欢原声"]
    with preference_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_media_preferences").fetchone()[0] == 0
    assert apply_media_preferences_change(
        owner, {"preferred_resolution": "1080p"}, expected_fingerprint=prepared["fingerprint"],
    ) is None
    assert get_media_preferences(owner) == original

    written = apply_media_preferences_change(owner, updates, expected_fingerprint=prepared["fingerprint"])
    assert written is not None and written["explicit"] and written["revision"] == 1
    assert len(written["revision_token"]) == 32
    assert not get_media_preferences(owner + " ")["explicit"]
    assert apply_media_preferences_change(
        owner + " ", updates, expected_fingerprint=prepare_media_preferences_change(owner, updates)["fingerprint"],
    ) is None

    prepared = prepare_media_preferences_change(owner, {"preferred_download_target": "qb"})
    merged = apply_media_preferences_change(
        owner, prepared["updates"], expected_fingerprint=prepared["fingerprint"],
    )
    assert merged is not None and merged["preferred_download_target"] == "qb"
    assert merged["preferred_resolution"] == "2160p" and merged["minimum_resolution"] == "any"
    assert merged["memory_notes"] == ["喜欢原声"]
    assert get_media_preferences(owner) == merged
    prepared = prepare_media_preferences_change(owner)
    assert prepared["proposed"] == default_media_preferences()
    cleared = apply_media_preferences_change(owner, expected_fingerprint=prepared["fingerprint"])
    assert cleared == {**default_media_preferences(), "revision": 0, "explicit": False}
    assert get_media_preferences(owner) == original
    assert apply_media_preferences_change(owner, expected_fingerprint=prepared["fingerprint"]) is None


def test_structured_preferences_freeze_then_persist_merge_and_owner_isolation(preference_db):
    digest = media_owner_digest("viewer")
    updates = validate_preference_updates({
        "preferred_resolution": "2160p", "minimum_resolution": "1080p",
        "preferred_hdr": "dolby_vision", "preferred_codecs": ["HEVC", "av1"],
        "preferred_subtitles": ["简中"], "preferred_audio_languages": ["国语"],
        "preferred_release_groups": ["BlackTV", "blacktv"],
        "excluded_keywords": ["枪版"], "max_episode_size_gb": 8,
        "preferred_genres": ["喜剧|Comedy"], "excluded_genres": ["恐怖"],
        "min_rating": 7.5, "exclude_played": True,
    })
    prepared = prepare_media_preferences_change(digest, updates)
    fingerprint = prepared["fingerprint"]
    preview = media_preferences_preview(prepared)
    assert not get_media_preferences(digest)["explicit"]
    assert preview["proposed"]["preferred_release_groups"] == ["BlackTV"]
    assert "首选分辨率：2160p" in preview["effects"]
    assert "首选编码：hevc、av1" in preview["effects"]
    assert "首选动态范围：杜比视界" in preview["effects"]
    assert "排除已看内容：是" in preview["effects"]
    from app.agent.public_safety import sanitize_public_text
    public_changes = sanitize_public_text("；".join(preview["effects"]), limit=2000)
    assert "首选分辨率:2160p" in public_changes and "内部状态" not in public_changes
    written = apply_media_preferences_change(digest, updates, expected_fingerprint=fingerprint)
    assert written is not None
    result = public_media_preferences(written)
    assert result["preferred_resolution"] == "2160p"
    assert result["preferred_codecs"] == ["hevc", "av1"]
    assert public_media_preferences(get_media_preferences(media_owner_digest("other")))["preferred_genres"] == []
    stored = get_media_preferences(digest)
    assert stored["revision"] == 1
    assert len(stored["revision_token"]) == 32
    assert "revision_token" not in result
    assert "revision_token" not in public_media_preferences(get_media_preferences(digest))
    assert stored["preferred_download_target"] == "guangya"
    with preference_db() as conn:
        saved = json.loads(conn.execute("SELECT profile_json FROM agent_media_preferences").fetchone()[0])
        assert saved["max_episode_size_gb"] == 8
    changed = set_media_preferences(digest, expected_revision=1, updates={"preferred_download_target": "qb"})
    assert changed is not None
    assert changed["revision"] == 2
    assert changed["revision_token"] != stored["revision_token"]
    assert changed["preferred_genres"] == ["喜剧|Comedy"]
    assert set_media_preferences(digest, expected_revision=1, updates={"preferred_resolution": "720p"}) is None
    assert apply_media_preferences_change(digest, updates, expected_fingerprint=fingerprint) is None
    clear = prepare_media_preferences_change(digest)
    assert media_preferences_preview(clear)["defaults"]["preferred_genres"] == []
    assert apply_media_preferences_change(digest, expected_fingerprint=clear["fingerprint"]) is not None
    assert get_media_preferences(digest)["preferred_resolution"] == "any"


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
    with pytest.raises(ValueError) as domain_error:
        validate_preference_updates(updates)
    assert type(domain_error.value) is ValueError


def test_defaults_not_mutable_and_corrupt_storage_has_safe_defaults(preference_db):
    first = default_media_preferences()
    first["preferred_genres"].append("污染")
    assert default_media_preferences()["preferred_genres"] == []
    digest = media_owner_digest("viewer")
    set_media_preferences(digest, expected_revision=0, updates={"preferred_genres": ["喜剧"]})
    with preference_db() as conn:
        conn.execute("UPDATE agent_media_preferences SET profile_json=?", ('{"preferred_genres": "bad"}',))
    assert owner_media_preferences("viewer")["preferred_genres"] == []
    with preference_db() as conn:
        conn.execute("UPDATE agent_media_preferences SET profile_json='not json'")
    assert get_media_preferences(digest)["preferred_genres"] == []


def test_local_recommendations_consume_saved_genre_rating_history_and_single_use_overrides(preference_db, monkeypatch):
    from unittest.mock import MagicMock

    from app import sdk
    from app.clients.base import MediaRecommendationInventory
    from app.modules import media_library

    digest = media_owner_digest("viewer")
    set_media_preferences(digest, expected_revision=0, updates={
        "preferred_server": "jellyfin", "preferred_genres": ["喜剧|Comedy"],
        "excluded_genres": ["恐怖|Horror"], "min_rating": 8, "exclude_played": True,
    })
    profile = MediaServerProfile(source="configured:jellyfin", server_type="jellyfin", label="Jellyfin", url="http://example.local", credential="test", enabled=True, user_id="viewer")
    client = MagicMock()
    client.__enter__.return_value = client
    client.enrich_media_genres.return_value = []
    client.list_recommendation_candidates.return_value = MediaRecommendationInventory()
    monkeypatch.setattr(media_library, "list_configured_profiles", lambda: [profile])
    monkeypatch.setattr(MediaServerProfile, "create_client", lambda _profile: client)
    with sdk.call_owner("viewer"):
        result = sdk.library.recommend(media_type="tv")
        assert result["criteria"]["prefer"] == ["喜剧|Comedy"]
        assert result["criteria"]["exclude"] == ["恐怖|Horror"]
        assert result["criteria"]["min_rating"] == 8
        override = {"prefer": [], "exclude": [], "min_rating": 0, "exclude_played": False}
        result = sdk.library.recommend(**override)
        assert all(result["criteria"][key] == value for key, value in override.items())
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


@pytest.mark.parametrize(("suffix", "preference", "matched"), [
    ("H264-ASDRGroup", "sdr", False), ("HDR", "hdr10", False),
    ("H264", "sdr", False), ("SDR", "sdr", True), ("HLG", "sdr", False),
    ("HDR10", "hdr10", True), ("杜比视界", "dolby_vision", True),
])
def test_resource_range_preferences_require_explicit_shared_evidence(suffix, preference, matched):
    item = _resource("a", f"Show.S01E01.1080p.WEB-DL.{suffix}")
    baseline = resource_preference_match(item, {"preferred_hdr": "any"})
    result = resource_preference_match(item, {"preferred_hdr": preference})
    assert result["score"] - baseline["score"] == (25 if matched else 0)
    assert result["eligible"] and baseline["eligible"]  # 软偏好不成为硬门槛。
    assert (f"偏好画面格式：{preference}" in result["reasons"]) is matched


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
def test_native_missing_resources_consumes_saved_owner_profile(preference_db, whole_season):
    from app import sdk
    from app.modules.episode_audit import EpisodeAuditResult
    from tests.test_agent_indexer_actions import FakeIndexerService
    from tests.test_missing_resources import _search_result

    set_media_preferences(media_owner_digest("viewer"), expected_revision=0, updates={
        "preferred_resolution": "1080p", "excluded_keywords": ["CAM"],
    })
    audit = EpisodeAuditResult(True, "updates_available", "确定缺集", data={
        "title":"Show", "tmdb_id":"12345", "as_of":"2026-08-01", "season":1,
        "missing_count":1,"missing_sample":[{"season":1,"episode":1}],
        "missing_sample_truncated":False,"target_missing":True,
    })
    source_items=[_resource("a","Show.S01E01.2160p.WEB-DL.HEVC-GroupA"),
                  _resource("b","Show.S01E01.1080p.WEB-DL.HEVC-GroupB"),
                  _resource("c","Show.S01E01.1080p.CAM-GroupC")]
    service=FakeIndexerService(private_results=False)
    service.search_media=AsyncMock(return_value=_search_result(items=source_items))
    with patch("app.modules.missing_resources.audit_series_episodes",return_value=audit), \
         patch("app.modules.missing_resources.config.get_bool",return_value=True), \
         patch("app.modules.missing_resources.get_indexer_service",return_value=service):
        for owner,overrides,expected in [("viewer",None,"b"*20),("other",None,"a"*20),
                ("viewer",{"preferred_resolution":"2160p"},"a"*20),
                ("viewer",{"excluded_keywords":[],"preferred_resolution":"2160p","max_episode_size_gb":0},"a"*20)]:
            item={"query":"Show","season":1,**({} if whole_season else {"episodes":[1]})}
            with sdk.call_owner(owner):
                result=sdk.resources.search_missing([item],as_of="2026-08-01",preference_overrides=overrides)
            assert result["ok"] and result["result_ids"]==[expected]
            search=result["groups"][0]["episodes"][0]["search"]
            assert search["recommendation"]["selected"]["result_id"]==expected
            assert len(result["items"]) == 3 and all("position" not in r for r in result["items"])
            assert set(search["result_ids"]) == set(search["quality"]) == set(search["preference_match"])
            assert set(search["result_ids"]) == {r["result_id"] for r in result["items"]}
    assert owner_media_preferences("viewer")["preferred_resolution"]=="1080p"
    assert all("quality" not in item for item in source_items)


@pytest.mark.parametrize("invalid",[{"preferred_server":"jellyfin"},{"max_episode_size_gb":-1},{"preferred_genres":["Comedy"]},"2160p"])
def test_native_missing_resource_rejects_invalid_quality_fields(invalid):
    from app import sdk
    with sdk.call_owner("viewer"),pytest.raises((TypeError,ValueError)):
        sdk.resources.search_missing([{"query":"Show","season":1}],preference_overrides=invalid)


@pytest.fixture
def download_environment(preference_db):
    with patch("app.indexers.downloads.download_target_readiness", side_effect=lambda target: {
            name: True for name in (["qb", "guangya"] if target == "both" else [target])}), \
            patch("app.indexers.downloads.create_request", return_value={"id": 42, "created": True}) as create, \
            patch("app.indexers.downloads.dispatch_request",
                  side_effect=lambda number, target: {"ok": True, "status": "submitted", "succeeded": [target], "failed": []}) as dispatch:
        yield create, dispatch



def test_native_resource_default_and_share_protocol_stay_separate():
    from app.agent.schema import validate_json_schema
    from app.agent.kernel.ports.sdk_effects import resource_download_tool, share_transfer_tool
    resource = resource_download_tool()
    share = share_transfer_tool()
    assert resource.input_schema["properties"]["target"]["default"] == "preferred"
    validate_json_schema({"result_ids": ["candidate-id"]}, resource.input_schema)
    validate_json_schema({"url": "https://www.guangyapan.com/s/example", "file_ids": ["real-id"]}, share.input_schema)
    for target in ("qb", "both", "preferred"):
        with pytest.raises(ToolPipelineError):
            validate_json_schema({"url": "https://www.guangyapan.com/s/example", "file_ids": ["real-id"], "target": target}, share.input_schema)
    for invalid in ("evil", "", 0, False, None):
        with pytest.raises(ToolPipelineError):
            validate_json_schema({"result_ids": ["candidate-id"], "target": invalid}, resource.input_schema)


def test_native_url_resolves_defaults_per_owner_and_explicit_choice_without_writes(download_environment):
    from app.agent.domains.resources.selection import _target_options

    create, dispatch = download_environment
    set_media_preferences(media_owner_digest("viewer"), expected_revision=0,
                          updates={"preferred_download_target": "qb"})
    async def check():
        state = InMemorySessionStateStore()
        effects = EffectExecutor(catalog=ToolCatalog([download_submit_tool()]), state_store=state)
        for owner, explicit, expected in (
            ("viewer", None, "qb"), ("other", None, "guangya"),
            ("viewer", "both", "both"), ("viewer", "guangya", "guangya"),
        ):
            context = await context_for(state, owner=owner, session=f"defaults-{explicit}")
            arguments = {"url": "magnet:?xt=urn:btih:" + "a" * 40}
            if explicit is not None:
                arguments["target"] = explicit
            prepared = await effects.prepare("downloads.submit", arguments, context=context)
            plan = prepared.effect_plan
            assert plan is not None and plan.arguments["target"] == expected
            assert plan.preview["data"]["target"] == expected
            assert plan.preview["data"]["backends"] == ({"qb": True, "guangya": True} if expected == "both" else {expected: True})
    asyncio.run(check())
    for owner, target, source in (("viewer", "qb", "saved_preference"), ("other", "guangya", "product_default")):
        options = _target_options(owner)
        assert (options["target"], options["target_source"]) == (target, source)
    create.assert_not_called()
    dispatch.assert_not_called()


def test_native_url_explicit_target_and_unavailable_default(download_environment):
    create, dispatch = download_environment
    set_media_preferences(media_owner_digest("viewer"), expected_revision=0,
                          updates={"preferred_download_target": "qb"})
    async def check():
        state = InMemorySessionStateStore()
        effects = EffectExecutor(catalog=ToolCatalog([download_submit_tool()]), state_store=state)
        context = await context_for(state, owner="viewer", session="explicit")
        url = "magnet:?xt=urn:btih:" + "a" * 40
        with patch("app.agent.kernel.ports.sdk_effects.resolve_download_target", side_effect=AssertionError("显式目标不得读取偏好")):
            assert (await effects.prepare("downloads.submit", {"url": url, "target": "qb"}, context=context)).effect_plan
        context = await context_for(state, owner="viewer", session="unavailable")
        with patch("app.indexers.downloads.download_target_readiness", return_value={"qb": False}):
            with pytest.raises(ToolPipelineError) as error:
                await effects.prepare("downloads.submit", {"url": url}, context=context)
        assert error.value.code == "precondition_failed"
        assert await effects.get_pending_plan(owner=context.owner, session_id=context.session_id) is None
    asyncio.run(check())
    create.assert_not_called()
    dispatch.assert_not_called()


@pytest.mark.parametrize("positions", [[1], [1, 2]])
def test_native_resource_single_and_batch_freeze_owner_default_target(preference_db, positions):
    from types import SimpleNamespace
    from app.agent.kernel.ports.sdk_effects import resource_download_tool
    from app.indexers.models import IndexerItem
    from app.indexers.result_store import IndexerResultStore
    from tests.support import _prepare_batch

    owner = media_owner_digest("viewer")
    set_media_preferences(owner, expected_revision=0, updates={"preferred_download_target": "qb"})
    ids = ["a" * 20, "b" * 20][:len(positions)]
    service = SimpleNamespace(result_store=IndexerResultStore())
    for identity in ids:
        service.result_store.restore(identity, IndexerItem(title=identity, site_id="nyaa", site_name="Nyaa",
            magnet="magnet:?xt=urn:btih:"+"a"*40, download_state="ready", download_kinds=("magnet",)))
    async def check():
        state = InMemorySessionStateStore()
        effects = EffectExecutor(catalog=ToolCatalog([resource_download_tool()]), state_store=state)
        context = await context_for(state, owner="viewer", session="resources")
        prepared = await effects.prepare("resources.download", {"result_ids": ids}, context=context)
        plan = prepared.effect_plan
        assert plan is not None and dict(plan.arguments) == {"result_ids": ids, "target": "qb"}
        assert plan.preview["data"]["target"] == "qb"
        prepare.assert_called_once_with(service, ids, "qb", scope="agent")
        submit.assert_not_called()
        set_media_preferences(owner, expected_revision=1, updates={"preferred_download_target": "both"})
        with patch("app.agent.kernel.ports.sdk_effects.resolve_download_target", side_effect=AssertionError("不得重读偏好")):
            result = await effects.execute_confirmed(plan.plan_id, context=context)
        data = result.outcome.public_content["data"]
        assert [item["result_id"] for item in data["items"]] == ids
        assert all(item["target"] == "qb" for item in data["items"])
        submit.assert_awaited_once_with(service, ids, "qb", expected_snapshot={"target": "qb", "resources": [
            {"result_id": identity, "target": "qb"} for identity in ids]},
            origin_namespace="agent", scope="agent", verification_targets=None, verification_owner="",
            check_cancelled=context.cancellation.raise_if_cancelled)
    with patch("app.agent.kernel.ports.sdk_effects.get_indexer_service", return_value=service), \
         patch("app.indexers.downloads.preview_indexer_download_batch", side_effect=_prepare_batch) as prepare, \
         patch("app.indexers.downloads.submit_indexer_download_batch", new=AsyncMock(return_value={
            "ok": True, "status": "accepted", "summary": "已提交", "error": "", "target": "qb",
            "items": [{"result_id": identity, "target": "qb", "request_id": 42, "status": "submitted"} for identity in ids],
         })) as submit:
        asyncio.run(check())


def test_native_share_uses_configured_cloud_directory_despite_saved_qb_preference(download_environment):
    from app.modules import share_transfer
    from tests.test_telegram_guangya_share import FakeShareClient, SHARE_URL, inspected_payload
    create, dispatch = download_environment
    set_media_preferences(media_owner_digest("viewer"), expected_revision=0,
                          updates={"preferred_download_target": "qb"})
    from app.clients.guangya import GuangYaClient
    client = FakeShareClient(inspected_payload(2))
    with patch.object(share_transfer, "get", side_effect=lambda key, default="": {
        "GY_SHARE_TARGET_DIR": "share-dir", "GY_SHARE_TARGET_DIR_NAME": "分享目录"}.get(key, default)), \
         patch("app.agent.kernel.ports.sdk_effects.resolve_download_target", side_effect=AssertionError("分享不应读取qB偏好")):
        prepared = share_transfer.prepare_share_transfer(SHARE_URL, ["file-2"], "viewer", "agent", client=cast(GuangYaClient, client),
                                                        store=ShareTransferPreviewStore())
    assert prepared["snapshot"]["target_id"] == "share-dir"
    assert [item["id"] for item in prepared["snapshot"]["files"]] == ["file-2"]
    assert not client.restore_calls
    create.assert_not_called()
    dispatch.assert_not_called()



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
    digest = media_owner_digest("viewer")
    set_media_preferences(digest, expected_revision=0, updates={"preferred_genres": ["A"]})
    state = _interleave_preference_writer_after_commit(
        monkeypatch, preference_db, digest=digest,
        after_sql="""UPDATE agent_media_preferences SET preferred_download_target='both',
            revision=revision+1, profile_json='{"preferred_genres":["C"]}' WHERE owner_digest=?""",
    )
    written_b = set_media_preferences(digest, expected_revision=1, updates={"preferred_download_target": "qb"})
    current_c = get_media_preferences(digest)
    assert state["fired"] and state["locked_after_reads"] == 1
    assert written_b is not None
    assert written_b["revision"] == 2
    assert written_b["preferred_download_target"] == "qb"
    assert written_b["preferred_genres"] == ["A"]
    assert current_c["revision"] == 3
    assert current_c["preferred_download_target"] == "both"
    assert current_c["preferred_genres"] == ["C"]


def test_preference_nonce_prevents_aba_after_clear_and_same_value_reinsert(preference_db):
    digest = media_owner_digest("viewer")
    updates = {"preferred_download_target": "qb"}
    original = set_media_preferences(digest, expected_revision=0, updates=updates)
    assert original is not None
    original_clear = prepare_media_preferences_change(digest)
    assert original["revision"] == 1 and original["revision_token"]
    assert apply_media_preferences_change(digest, expected_fingerprint=original_clear["fingerprint"]) is not None
    absent = get_media_preferences(digest)
    assert absent["revision_token"] == "" and not absent["explicit"]
    recreated = set_media_preferences(digest, expected_revision=0, updates=updates)
    assert recreated is not None
    assert recreated["revision"] == original["revision"] == 1
    assert public_media_preferences(recreated) == public_media_preferences(original)
    assert recreated["revision_token"] != original["revision_token"]
    assert prepare_media_preferences_change(digest)["fingerprint"] != original_clear["fingerprint"]
    assert apply_media_preferences_change(digest, expected_fingerprint=original_clear["fingerprint"]) is None
    assert get_media_preferences(digest) == recreated


def test_legacy_empty_preference_nonce_migrates_on_next_write(preference_db):
    with preference_db() as conn:
        conn.execute("""INSERT INTO agent_media_preferences(
            owner_digest, preferred_server, preferred_download_target, revision, created_at, updated_at
        ) VALUES ('legacy', 'emby', 'qb', 4, '', '')""")
    assert get_media_preferences("legacy")["revision_token"] == ""
    updated = set_media_preferences("legacy", expected_revision=4, updates={"preferred_server": "emby"})
    assert updated is not None
    assert updated["revision"] == 5 and len(updated["revision_token"]) == 32
    assert get_media_preferences("legacy") == updated


def test_preference_nonce_cas_rejects_aba_between_snapshot_check_and_write(preference_db, monkeypatch):
    original = set_media_preferences("owner", expected_revision=0, updates={"preferred_download_target": "qb"})
    assert original is not None
    updates = {"preferred_download_target": "both"}
    prepared = prepare_media_preferences_change("owner", updates)
    recreated = None

    def reinsert_before_write(owner_digest, **kwargs):
        nonlocal recreated
        assert clear_media_preferences(
            owner_digest, expected_revision=original["revision"], expected_revision_token=original["revision_token"],
        )
        recreated = set_media_preferences(owner_digest, expected_revision=0, updates={"preferred_download_target": "qb"})
        return set_media_preferences(owner_digest, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr("app.repositories.media_experience.set_media_preferences", reinsert_before_write)
        assert apply_media_preferences_change("owner", updates, expected_fingerprint=prepared["fingerprint"]) is None
    assert recreated is not None and recreated["revision_token"] != original["revision_token"]
    assert recreated["revision"] == original["revision"]
    assert not clear_media_preferences(
        "owner", expected_revision=original["revision"], expected_revision_token=original["revision_token"],
    )
    assert set_media_preferences(
        "owner", expected_revision=original["revision"], expected_revision_token=original["revision_token"],
        updates={"preferred_download_target": "both"},
    ) is None
    assert get_media_preferences("owner") == recreated
    set_media_preferences_result = set_media_preferences(
        "owner", expected_revision=recreated["revision"], expected_revision_token=recreated["revision_token"],
        updates={"preferred_download_target": "both"},
    )
    assert set_media_preferences_result is not None
    assert set_media_preferences_result["revision"] == 2
