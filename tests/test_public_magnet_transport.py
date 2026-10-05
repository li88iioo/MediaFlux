from __future__ import annotations

import base64
import copy
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from app.indexers.providers.base import (
    PUBLIC_TRACKERS,
    augment_public_magnet,
    magnet_infohash,
)
from app.modules import download_dispatcher, offline


V1_HASH = "0123456789abcdef0123456789abcdef01234567"
V2_HASH = "ab" * 32


def magnet(*params: str) -> str:
    return "magnet:?" + "&".join(params)


def query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def public_row(**overrides):
    row = {
        "id": 41,
        "kind": "magnet",
        "origin": "indexer:nyaa",
        "source_value": magnet(f"xt=urn:btih:{V1_HASH}"),
        "title": "Raw title",
        "display_title": "Public Release",
        "torrent_data": None,
    }
    row.update(overrides)
    return row


def test_augment_preserves_existing_duplicate_params_and_tracker_values():
    source = magnet(
        f"xt=urn:btih:{V1_HASH}",
        "dn=Original+Name",
        "dn=Second+Name",
        "tr=udp%3A%2F%2Fexisting.invalid%2Fannounce",
        "tr=udp%3A%2F%2Fexisting.invalid%2Fannounce",
        "xs=https%3A%2F%2Fsource.invalid%2Fseed",
        "xt=urn%3Abtmh%3A1220" + V2_HASH,
        "tag=one",
        "tag=two",
    )
    existing_tracker = "udp://existing.invalid/announce"
    result = augment_public_magnet(
        source,
        "A title that must not replace dn",
        trackers=(existing_tracker, existing_tracker + "/", "udp://new.invalid/announce"),
    )

    params = query(result)
    assert result.startswith(source + "&")
    assert params["dn"] == ["Original Name", "Second Name"]
    assert params["tr"] == [existing_tracker, existing_tracker, "udp://new.invalid/announce"]
    assert params["xs"] == ["https://source.invalid/seed"]
    assert params["xt"] == [f"urn:btih:{V1_HASH}", f"urn:btmh:1220{V2_HASH}"]
    assert params["tag"] == ["one", "two"]


def test_augment_encodes_unicode_and_reserved_display_name_characters():
    title = "中文 A&B # + /?"
    result = augment_public_magnet(magnet(f"xt=urn:btih:{V1_HASH}"), title, trackers=())

    assert query(result)["dn"] == [title]
    assert "A%26B" in result
    assert "%23" in result
    assert "%2B" in result


def test_augment_is_idempotent_and_deduplicates_trackers():
    source = magnet(f"xt=urn:btih:{V1_HASH}")
    trackers = (
        "udp://one.invalid/announce",
        "udp://one.invalid/announce/",
        "https://two.invalid/announce",
    )
    once = augment_public_magnet(source, "Fixture", trackers=trackers)
    twice = augment_public_magnet(once, "Fixture", trackers=trackers)

    assert twice == once
    params = query(once)
    assert params["dn"] == ["Fixture"]
    assert params["tr"] == ["udp://one.invalid/announce", "https://two.invalid/announce"]


def test_augment_adds_at_most_three_distinct_trackers():
    trackers = tuple(f"udp://tracker-{index}.invalid/announce" for index in range(1, 6))
    result = augment_public_magnet(magnet(f"xt=urn:btih:{V1_HASH}"), trackers=trackers)

    assert query(result)["tr"] == list(trackers[:3])
    assert len(query(result)["tr"]) == len(PUBLIC_TRACKERS)


def test_augment_does_not_change_v1_v2_or_hybrid_identity():
    cases = (
        (magnet(f"xt=urn:btih:{V1_HASH}"), V1_HASH),
        (
            magnet("xt=urn:btih:" + base64.b32encode(bytes.fromhex(V1_HASH)).decode("ascii")),
            V1_HASH,
        ),
        (magnet(f"xt=urn:btmh:1220{V2_HASH}"), V2_HASH[:40]),
        (magnet(f"xt=urn:btmh:1220{V2_HASH}", f"xt=urn:btih:{V1_HASH}"), V1_HASH),
    )
    for source, expected in cases:
        assert magnet_infohash(augment_public_magnet(source, "Fixture")) == expected


def test_invalid_or_unidentified_magnets_are_returned_unchanged():
    values = (
        "magnet:?xt=urn:btih:not-a-valid-hash",
        "magnet:?dn=missing-identity",
        "https://fixture.invalid/resource",
        "not a magnet",
    )
    for source in values:
        assert augment_public_magnet(source, "Fixture") == source


def test_qb_transport_enriches_known_indexer_and_agent_without_mutating_rows():
    for origin in ("indexer:nyaa", "agent:nyaa"):
        row = public_row(origin=origin)
        before = copy.deepcopy(row)
        captured = {}

        class FixtureQB:
            def add_torrent_detailed(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(ok=True, failure_code="", task_ids=["fixture-task"], retryable=False)

        with (
            patch.object(download_dispatcher, "QBittorrentClient", return_value=FixtureQB()),
            patch.object(download_dispatcher, "close_qbittorrent_client"),
        ):
            result = download_dispatcher._submit_qb(row, runtime_config={"url": "http://qb.fixture"})

        assert result["ok"] is True
        assert query(captured["urls"])["dn"] == ["Public Release"]
        assert query(captured["urls"])["tr"] == list(PUBLIC_TRACKERS)
        assert row == before
        assert magnet_infohash(captured["urls"]) == magnet_infohash(row["source_value"])


def test_qb_transport_does_not_enrich_unknown_source_provenance():
    for origin in ("telegram", "rss:fixture", "pt:fixture", "custom:fixture"):
        row = public_row(origin=origin)
        source = row["source_value"]
        captured = {}

        class FixtureQB:
            def add_torrent_detailed(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(ok=True, failure_code="", task_ids=[], retryable=False)

        with (
            patch.object(download_dispatcher, "QBittorrentClient", return_value=FixtureQB()),
            patch.object(download_dispatcher, "close_qbittorrent_client"),
        ):
            result = download_dispatcher._submit_qb(row, runtime_config={"url": "http://qb.fixture"})

        assert result["ok"] is True
        assert captured["urls"] == source
        assert query(captured["urls"]).get("dn") is None
        assert query(captured["urls"]).get("tr") is None
        assert download_dispatcher._public_magnet_title(row) is None


def test_qb_transport_passes_torrent_bytes_verbatim_without_public_magnet_rewrite():
    private_torrent = b"d4:infod6:privatei1e4:name4:test6:lengthi0eee"
    row = public_row(kind="torrent", torrent_data=private_torrent)
    captured = {}

    class FixtureQB:
        def add_torrent_detailed(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(ok=True, failure_code="", task_ids=["fixture-torrent"], retryable=False)

    with (
        patch.object(download_dispatcher, "QBittorrentClient", return_value=FixtureQB()),
        patch.object(download_dispatcher, "close_qbittorrent_client"),
    ):
        result = download_dispatcher._submit_qb(row, runtime_config={"url": "http://qb.fixture"})

    assert result["ok"] is True
    assert captured["torrents"] is private_torrent
    assert captured["urls"] == ""
    assert row["torrent_data"] == private_torrent
    assert download_dispatcher._public_magnet_title(row) is None


def fixture_rules():
    return offline.OfflineRules(
        magnet_enabled=True,
        ed2k_enabled=True,
        http_enabled=False,
        target_dir_id="root-dir",
        target_dir_name="Root",
        secondary_enabled=True,
        secondary_dir_id="secondary-dir",
        secondary_dir_name="Secondary",
        secondary_keywords=("RouteOnly",),
        exclude_keywords=(),
        min_file_mb=0,
        allowed_exts=("mkv",),
    )


class FixtureGuangYa:
    logged_in = True

    def __init__(self):
        self.events = []
        self.resolved_urls = []
        self.resolved_torrents = []
        self.selections = []
        self.created_dirs = []

    def resolve_url(self, url):
        self.events.append("resolve")
        self.resolved_urls.append(url)
        return {
            "code": 0,
            "data": {
                "subfiles": [
                    {"fileIndex": 4, "name": "Episode.mkv", "size": 1000},
                    {"fileIndex": 11, "name": "readme.txt", "size": 100},
                ]
            },
        }

    def resolve_torrent(self, data):
        self.events.append("resolve-torrent")
        self.resolved_torrents.append(data)
        return {
            "code": 0,
            "data": {"subfiles": [{"fileIndex": 4, "name": "Episode.mkv", "size": 1000}]},
        }

    def create_dir(self, name, parent_id):
        self.events.append("create-dir")
        self.created_dirs.append((name, parent_id))
        return "fixture-staging"

    def add_offline_selection(self, url, target_dir_id, file_indexes):
        self.events.append("submit-selection")
        self.selections.append((url, target_dir_id, list(file_indexes)))
        return {"ok": True, "task_ids": ["fixture-offline-task"], "batch_count": 1}


def test_guangya_submit_routes_from_raw_input_but_resolves_and_submits_enriched_url():
    row = public_row(title="Raw title", display_title="RouteOnly")
    before = copy.deepcopy(row)
    source = row["source_value"]
    client = FixtureGuangYa()
    rules = fixture_rules()

    def submit_with_fixture_client(url, **kwargs):
        return offline.submit_offline(url, client=client, **kwargs)

    original_analyze = offline.analyze_offline_url
    decisions = []

    def capture_analyze(*args, **kwargs):
        decision = original_analyze(*args, **kwargs)
        decisions.append(decision)
        return decision

    with (
        patch.object(offline.OfflineRules, "from_config", return_value=rules),
        patch.object(offline, "analyze_offline_url", side_effect=capture_analyze) as analyze,
        patch.object(download_dispatcher, "submit_offline", side_effect=submit_with_fixture_client),
        patch.object(download_dispatcher, "_recover_guangya_magnet_torrent", return_value=None),
        patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=[]),
        patch.object(download_dispatcher.db, "bind_download_request_guangya_staging", return_value=True),
    ):
        result = download_dispatcher._submit_guangya(row)

    assert result["ok"] is True, result
    assert analyze.call_args.args[0] == source
    assert analyze.call_args.kwargs["title"] == "Raw title"
    assert decisions[0].target_dir_id == "root-dir"
    assert decisions[0].matched_keyword == ""

    enriched = client.resolved_urls[0]
    assert query(enriched)["dn"] == ["RouteOnly"]
    assert query(enriched)["tr"] == list(PUBLIC_TRACKERS)
    assert client.created_dirs and client.created_dirs[0][1] == "root-dir"
    assert client.selections == [(enriched, "fixture-staging", [4])]
    assert client.events == ["resolve", "create-dir", "submit-selection"]
    assert result["selection_mode"] == "files"
    assert result["staging"]["isolated"] is True
    assert result["staging"]["id"] == "fixture-staging"
    assert row == before
    assert row["source_value"] == source


def test_offline_private_torrent_bytes_bypass_public_magnet_enrichment():
    private_torrent = b"d4:infod6:privatei1e4:name4:test6:lengthi0eee"
    source = magnet(f"xt=urn:btih:{V1_HASH}")
    client = FixtureGuangYa()
    rules = replace(fixture_rules(), secondary_enabled=False)

    with (
        patch.object(offline.OfflineRules, "from_config", return_value=rules),
        patch.object(offline, "analyze_offline_url", wraps=offline.analyze_offline_url) as analyze,
    ):
        result = offline.submit_offline(
            source,
            title="Raw title",
            client=client,
            torrent_data=private_torrent,
            public_magnet_title="Should not be injected",
        )

    assert result["ok"] is True, result
    assert analyze.call_args.args[0] == source
    assert client.resolved_torrents == [private_torrent]
    assert client.resolved_urls == []
    assert client.selections == [(source, "root-dir", [4])]
    assert "dn" not in query(client.selections[0][0])
    assert "tr" not in query(client.selections[0][0])
