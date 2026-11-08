from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from copy import deepcopy
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import pytest

from app import services
from app.sdk import call_owner, library
from app.clients.base import MediaIdentityCandidate, MediaIdentityInventory
from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient
from app.discovery.models import MediaCard
from app.services import (
    inspect_media_identity_batch,
    summarize_media_identity_batch,
)

_ITEMS = [
    {"tmdb_id": "77", "media_type": "movie", "title": "记忆碎片", "year": "2000"},
    {"tmdb_id": "157336", "media_type": "movie", "title": "星际穿越", "year": "2014"},
    {"tmdb_id": "872585", "media_type": "movie", "title": "奥本海默", "year": "2023"},
]


class _FakeMediaClient:
    def __init__(self):
        self.calls = 0

    def list_media_identity_inventory(self, media_type, **kwargs):
        self.calls += 1
        assert media_type == "movie"
        assert kwargs == {"max_items": 5000, "page_size": 200}
        return MediaIdentityInventory(
            candidates=[
                MediaIdentityCandidate(
                    name="记忆碎片", year="2000", tmdb_id="77", media_type="movie"
                ),
                MediaIdentityCandidate(
                    name="星际穿越", year="2014", tmdb_id="", media_type="movie"
                ),
            ],
            total=2,
            unmapped=1,
        )


def test_service_batch_presence_enumerates_library_once_per_media_type():
    client = _FakeMediaClient()
    with patch("app.services._configured_media_sources", return_value=[("jellyfin", "Jellyfin", "", client)]):
        result = inspect_media_identity_batch(_ITEMS)[0]

    assert client.calls == 1
    assert [item["status"] for item in result["items"]] == [
        "present",
        "possible",
        "missing",
    ]


@pytest.mark.parametrize("client_type", [EmbyClient, JellyfinClient])
@pytest.mark.parametrize("cancel_at", ["before", "first_page", "never"])
def test_sdk_presence_cancellation_stops_inventory_reads_without_claiming_missing(client_type, cancel_at):
    stopped = Event()
    if cancel_at == "before":
        stopped.set()
    client = client_type("http://synthetic.invalid", "synthetic")
    if cancel_at != "before":
        client._cached_user_id = "viewer"
    movies = [
        {"Id": str(number), "Name": f"电影 {number}", "ProviderIds": {"Tmdb": str(number)}}
        for number in range(1, 402)
    ]

    def request(path, *, params=None, timeout=None):
        if path == "/Users":
            return [{"Id": "viewer", "Policy": {"IsAdministrator": True}}]
        assert path == "/Users/viewer/Items"
        assert params is not None
        if params["IncludeItemTypes"] == "Series":
            return {"Items": [], "TotalRecordCount": 0}
        start = params["StartIndex"]
        if cancel_at == "first_page" and start == 0:
            stopped.set()
        return {"Items": movies[start:start + params["Limit"]], "TotalRecordCount": len(movies)}

    items = [
        {"tmdb_id": "77", "media_type": "movie"},
        {"tmdb_id": "999999", "media_type": "movie"},
        {"tmdb_id": "99", "media_type": "tv"},
    ]
    with (
        patch("socket.socket.connect", side_effect=AssertionError("禁止真实外联")),
        patch.object(client, "_request", side_effect=request) as http,
        patch.object(client, "close", wraps=client.close) as close,
        patch("app.services._configured_media_sources", return_value=[("test", "测试媒体库", "", client)]),
        call_owner("test", cancelled=stopped.is_set),
    ):
        result = library.presence(items)

    pages = [
        (call.kwargs["params"]["IncludeItemTypes"], call.kwargs["params"]["StartIndex"])
        for call in http.call_args_list if call.args[0].endswith("/Items")
    ]
    if cancel_at == "before":
        http.assert_not_called()
    elif cancel_at == "first_page":
        assert pages == [("Movie", 0)]
    else:
        assert pages == [("Movie", 0), ("Movie", 200), ("Movie", 400), ("Series", 0)]
    if cancel_at == "never":
        assert result["status"] == "success"
        assert [item["library_status"] for item in result["data"]["items"]] == ["present", "missing", "missing"]
    else:
        assert result["status"] == "partial"
        assert [item["library_status"] for item in result["data"]["items"]] == ["indeterminate"] * 3
        assert result["data"]["sources"][0]["inventories"] == {}
    close.assert_called_once_with()


def test_identity_snapshot_reuses_each_server_and_type_only_within_one_request():
    from app import services

    movie = MediaIdentityInventory(candidates=[], total=0)
    tv = MediaIdentityInventory(candidates=[], total=0)
    client = Mock()
    client.list_media_identity_inventory.side_effect = lambda media_type, **_: movie if media_type == "movie" else tv
    with patch("app.services._configured_media_sources", return_value=[("jellyfin", "Jellyfin", "", client)]):
        snapshot = services.load_media_identity_snapshot(("movie", "tv"))
        for number in (1, 2):
            identities = [{"tmdb_id": str(number), "media_type": kind, "title": "作品"} for kind in ("movie", "tv")]
            sources = services.inspect_media_identity_batch(identities, snapshot=snapshot)
            assert [item["status"] for item in sources[0]["items"]] == ["missing", "missing"]
        assert client.list_media_identity_inventory.call_count == 2
        movie.candidates.append(MediaIdentityCandidate(name="作品", tmdb_id="1", media_type="movie"))
        next_snapshot = services.load_media_identity_snapshot(("movie",))
        current = services.inspect_media_identity_batch([{"tmdb_id": "1", "media_type": "movie"}], snapshot=next_snapshot)
        assert current[0]["items"][0]["status"] == "present"
        assert client.list_media_identity_inventory.call_count == 3
        # 旧请求继续使用原快照，不能被下一次枚举的可变列表偷偷更新。
        original = services.inspect_media_identity_batch([{"tmdb_id": "1", "media_type": "movie"}], snapshot=snapshot)
        assert original[0]["items"][0]["status"] == "missing"


def test_identity_snapshot_enumerates_each_server_and_type_once_and_closes_clients():
    from app import services

    clients = [Mock(), Mock()]
    for client in clients:
        client.list_media_identity_inventory.return_value = MediaIdentityInventory(candidates=[], total=0)
    configured = [("emby", "Emby", "", clients[0]), ("jellyfin", "Jellyfin", "", clients[1])]
    with patch("app.services._configured_media_sources", return_value=configured):
        snapshot = services.load_media_identity_snapshot(("movie", "tv", "movie"))
    for number in (1, 2):
        identities = [{"tmdb_id": str(number), "media_type": kind} for kind in ("movie", "tv")]
        sources = services.inspect_media_identity_batch(identities, snapshot=snapshot)
        assert services.summarize_media_identity_batch(identities, sources)["counts"]["missing"] == 2
    for client in clients:
        assert [call.args[0] for call in client.list_media_identity_inventory.call_args_list] == ["movie", "tv"]
        client.close.assert_called_once_with()


def test_batch_presence_aggregates_all_sources_without_per_title_search():
    sources = [
        {
            "server_type": "jellyfin",
            "server_name": "Jellyfin",
            "status": "ready",
            "inventories": {"movie": {"total": 100, "truncated": False, "unmapped": 0}},
            "items": [
                {"tmdb_id": "77", "media_type": "movie", "status": "present", "match": "provider_id"},
                {"tmdb_id": "157336", "media_type": "movie", "status": "missing", "match": "none"},
                {"tmdb_id": "872585", "media_type": "movie", "status": "missing", "match": "none"},
            ],
        }
    ]
    with patch(
        "app.services.inspect_media_identity_batch",
        return_value=sources,
    ) as inspect, patch(
        "app.services.summarize_media_identity_batch",
        wraps=summarize_media_identity_batch,
    ) as summarize:
        result = library.presence(_ITEMS)

    inspect.assert_called_once_with(_ITEMS, cancelled=None)
    summarize.assert_called_once_with(_ITEMS, sources)
    assert result["ok"]
    assert result["data"]["counts"] == {
        "present": 1,
        "possible": 0,
        "missing": 2,
        "indeterminate": 0,
    }
    assert [item["library_status"] for item in result["data"]["items"]] == [
        "present",
        "missing",
        "missing",
    ]


@pytest.mark.parametrize("items", [
    None, [], [None],
    [{**_ITEMS[0], "tmdb_id": "0"}],
    [{**_ITEMS[0], "media_type": "series"}],
    [{**_ITEMS[0], "title": "\x00"}],
    [{**_ITEMS[0], "year": "2100"}],
    [{"provider": "bangumi", "external_id": "77", "media_type": "movie"}],
])
def test_batch_presence_rejects_invalid_identity_lists_before_reading(items):
    with patch("app.services._configured_media_sources", side_effect=AssertionError("无效参数不得读库")) as sources:
        with pytest.raises(ValueError):
            services.batch_library_presence(items)
        with pytest.raises(ValueError):
            library.presence(items)
    sources.assert_not_called()


def test_presence_accepts_complete_media_cards_but_rechecks_inventory_facts():
    items = [
        {**MediaCard.from_dict({"provider": "tmdb", "external_id": item["tmdb_id"],
                               "release_date": f'{item["year"]}-01-01', **item}).to_dict(),
         "library_status": "present", "sources": [{"status": "ready"}],
         "server": "emby", "owner": "untrusted-owner"}
        for item in _ITEMS
    ]
    before = deepcopy(items)
    client = _FakeMediaClient()
    with patch("app.services._configured_media_sources", return_value=[("jellyfin", "Jellyfin", "", client)]), patch(
        "app.services.inspect_media_identity_batch", wraps=inspect_media_identity_batch,
    ) as inspect:
        result = library.presence(items)

    inspect.assert_called_once_with(_ITEMS, cancelled=None)
    assert client.calls == 1
    assert result["status"] == "partial"
    assert [row["library_status"] for row in result["data"]["items"]] == ["present", "possible", "missing"]
    assert [source["server_type"] for source in result["data"]["sources"]] == ["jellyfin"]
    assert "untrusted-owner" not in json.dumps(result)
    assert items == before


def test_sdk_and_shared_presence_keep_the_same_normalized_facts():
    items = [{**_ITEMS[0], "title": "A" * 239 + " B"}]
    sources = [{
        "server_type": "jellyfin", "server_name": "Jellyfin", "status": "ready",
        "items": [{**_ITEMS[0], "status": "missing", "match": "none"}],
    }]
    with patch("app.services.inspect_media_identity_batch", return_value=sources):
        shared = services.batch_library_presence(items)
        native = library.presence(items)

    assert native == shared
    assert native["data"]["items"][0]["title"] == "A" * 239 + " "
    assert items[0]["title"] == "A" * 239 + " B"


def test_native_presence_reads_large_input_once_per_source_and_media_type():
    items = [
        {"tmdb_id": str(number), "media_type": kind, "title": f"作品 {number}", "year": "2026"}
        for number in range(1, 61) for kind in ("movie", "tv")
    ]
    before = deepcopy(items)
    clients = [Mock(), Mock()]
    for client in clients:
        client.list_media_identity_inventory.return_value = MediaIdentityInventory(candidates=[], total=0)
    configured = [("emby", "Emby", "", clients[0]), ("jellyfin", "Jellyfin", "", clients[1])]
    with patch("app.services._configured_media_sources", return_value=configured) as sources:
        result = library.presence(items)

    sources.assert_called_once_with()
    assert result["ok"] and result["status"] == "success"
    assert result["data"]["total"] == 120
    assert result["data"]["counts"] == {"present": 0, "possible": 0, "indeterminate": 0, "missing": 120}
    assert [(item["tmdb_id"], item["media_type"]) for item in result["data"]["items"]] == [
        (item["tmdb_id"], item["media_type"]) for item in items
    ]
    for client in clients:
        assert [call.args[0] for call in client.list_media_identity_inventory.call_args_list] == ["movie", "tv"]
        assert all(call.kwargs == {"max_items": 5000, "page_size": 200}
                   for call in client.list_media_identity_inventory.call_args_list)
        client.close.assert_called_once_with()
    assert json.loads(json.dumps(result)) == result
    assert items == before


@pytest.mark.parametrize("tv_first", [False, True])
@pytest.mark.parametrize("truncated", [False, True])
def test_one_media_type_failure_keeps_other_type_inventory(tv_first, truncated):
    client = Mock()
    movie = MediaIdentityInventory(
        candidates=[MediaIdentityCandidate(name="电影", tmdb_id="77", media_type="movie")],
        total=2 if truncated else 1, truncated=truncated,
    )

    def inventory(media_type, **kwargs):
        if media_type == "tv":
            raise OSError("private upstream details")
        return movie

    client.list_media_identity_inventory.side_effect = inventory
    items = [{"tmdb_id": "77", "media_type": "movie"}, {"tmdb_id": "88", "media_type": "movie"}]
    items.insert(0 if tv_first else 2, {"tmdb_id": "99", "media_type": "tv"})
    with patch("app.services._configured_media_sources", return_value=[("jellyfin", "Jellyfin", "", client)]):
        result = library.presence(items)

    assert result["status"] == "partial"
    assert {x["tmdb_id"]: x["library_status"] for x in result["data"]["items"]} == {
        "77": "present", "88": "indeterminate" if truncated else "missing", "99": "indeterminate",
    }
    source = result["data"]["sources"][0]
    assert source["status"] == "partial"
    assert source["inventories"] == {"movie": {"total": movie.total, "truncated": truncated, "unmapped": 0}}
    assert [x["tmdb_id"] for x in result["data"]["items"]] == [x["tmdb_id"] for x in items]
    assert [call.args[0] for call in client.list_media_identity_inventory.call_args_list] == (
        ["tv", "movie"] if tv_first else ["movie", "tv"]
    )
    assert "private upstream details" not in json.dumps(result)
    client.close.assert_called_once_with()


@pytest.mark.parametrize("unmapped", [False, True])
def test_presence_accepts_exact_id_without_title_and_keeps_unmapped_uncertainty(unmapped):
    client = Mock()
    candidates = [MediaIdentityCandidate(name="目标片", tmdb_id="77", media_type="movie")]
    if unmapped:
        candidates.append(MediaIdentityCandidate(name="未映射片", media_type="movie"))
    client.list_media_identity_inventory.return_value = MediaIdentityInventory(
        candidates=candidates, total=len(candidates), unmapped=int(unmapped),
    )
    items = [{"tmdb_id": "77", "media_type": "movie"}, {"tmdb_id": "88", "media_type": "movie"}]
    with patch("app.services._configured_media_sources", return_value=[("jellyfin", "Jellyfin", "", client)]):
        result = library.presence(items)
    assert [item["library_status"] for item in result["data"]["items"]] == [
        "present", "indeterminate" if unmapped else "missing",
    ]
    assert [item["title"] for item in result["data"]["items"]] == ["", ""]
    assert result["status"] == ("partial" if unmapped else "success")
    client.list_media_identity_inventory.assert_called_once()


def test_shared_batch_presence_keeps_exact_ids_and_media_types_distinct():
    items = [
        {"tmdb_id": str(number), "media_type": kind, "title": "同名", "year": "2026"}
        for kind in ("movie", "tv") for number in (1, 2)
    ]
    items[0] = {"tmdb_id": " 1 ", "media_type": " MOVIE ", "title": "  同名  ", "year": " 2026 "}
    items += [{**items[0], "title": "重复身份不得覆盖首项"},
              {"tmdb_id": "3", "media_type": "movie", "title": "未映射", "year": "2026"}]
    inventories = {
        "movie": MediaIdentityInventory(candidates=[
            MediaIdentityCandidate(name="同名", year="2026", tmdb_id="1", media_type="movie"),
            MediaIdentityCandidate(name="未映射", year="2026", tmdb_id="", media_type="movie"),
        ], total=2, unmapped=1),
        "tv": MediaIdentityInventory(candidates=[
            MediaIdentityCandidate(name="同名", year="2026", tmdb_id="2", media_type="tv"),
        ], total=1),
    }
    client = Mock()
    client.list_media_identity_inventory.side_effect = lambda kind, **_: inventories[kind]
    with patch("app.services._configured_media_sources", return_value=[("jellyfin", "Jellyfin", "", client)]):
        result = services.batch_library_presence(items)

    assert result["status"] == "partial"
    assert result["data"]["total"] == 5
    assert [item["library_status"] for item in result["data"]["items"]] == [
        "present", "missing", "missing", "present", "possible",
    ]
    assert {key: result["data"]["items"][0][key] for key in ("tmdb_id", "media_type", "title", "year")} == {
        "tmdb_id": "1", "media_type": "movie", "title": "同名", "year": "2026",
    }
    assert result["data"]["sources"][0]["inventories"]["movie"] == {
        "total": 2, "truncated": False, "unmapped": 1,
    }
    assert client.list_media_identity_inventory.call_count == 2


def test_shared_batch_presence_preserves_matches_when_another_source_fails():
    available, failed = _FakeMediaClient(), Mock()
    failed.list_media_identity_inventory.side_effect = OSError("synthetic private upstream error")
    with patch("app.services._configured_media_sources", return_value=[
        ("jellyfin", "Jellyfin", "", available), ("emby", "Emby", "", failed),
    ]):
        result = services.batch_library_presence(_ITEMS)

    assert result["ok"] and result["status"] == "partial"
    assert [item["library_status"] for item in result["data"]["items"]] == ["present", "possible", "indeterminate"]
    assert result["data"]["counts"] == {"present": 1, "possible": 1, "indeterminate": 1, "missing": 0}
    assert [source["status"] for source in result["data"]["sources"]] == ["ready", "unavailable"]
    assert available.calls == failed.list_media_identity_inventory.call_count == 1
    failed.close.assert_called_once_with()
    assert "private upstream" not in json.dumps(result)


def test_shared_batch_presence_runs_without_agent_runtime():
    script = textwrap.dedent('''
        import json
        import sys
        import tests
        from unittest.mock import patch

        blocked = ("app.routes", "fastapi", "app.agent.domains", "app.agent.kernel")
        with patch.dict(sys.modules, dict.fromkeys(blocked)), patch(
            "socket.socket.connect", side_effect=AssertionError("禁止真实外联"),
        ):
            from app.services import batch_library_presence

            with patch("app.services._configured_media_sources", return_value=[]):
                result = batch_library_presence(json.load(sys.stdin))
            assert not any(module is not None and (name == prefix or name.startswith(prefix + "."))
                           for name, module in sys.modules.items() for prefix in blocked)
            print(json.dumps(result))
    ''')
    completed = subprocess.run(
        [sys.executable, "-c", script], input=json.dumps(_ITEMS),
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert not result["ok"] and result["status"] == "not_configured"
    assert result["data"] == {"total": 3, "items": [], "sources": []}
    assert "evidence" not in result


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("present", "present"), "present"),
        (("present", "possible"), "present"),
        (("present", "indeterminate"), "present"),
        (("present", "missing"), "present"),
        (("possible", "possible"), "possible"),
        (("possible", "indeterminate"), "possible"),
        (("possible", "missing"), "possible"),
        (("indeterminate", "indeterminate"), "indeterminate"),
        (("indeterminate", "missing"), "indeterminate"),
        (("missing", "missing"), "missing"),
    ],
)
@pytest.mark.parametrize("reverse_sources", [False, True])
def test_summarize_batch_status_priority(statuses, expected, reverse_sources):
    if reverse_sources:
        statuses = statuses[::-1]
    sources = [
        {
            "server_type": "jellyfin",
            "server_name": str(index),
            "status": "ready",
            "items": [{**_ITEMS[0], "status": status}],
        }
        for index, status in enumerate(statuses)
    ]

    result = summarize_media_identity_batch(_ITEMS[:1], sources)

    assert result["counts"] == {
        "present": int(expected == "present"),
        "possible": int(expected == "possible"),
        "indeterminate": int(expected == "indeterminate"),
        "missing": int(expected == "missing"),
    }
    assert result["items"][0]["library_status"] == expected
    assert [source["status"] for source in result["items"][0]["sources"]] == list(statuses)


def test_summarize_batch_preserves_public_payload_without_fetching_or_mutating():
    identities = [
        _ITEMS[2],
        {"tmdb_id": "77", "media_type": "tv", "title": "同 ID 剧集", "year": "2000"},
        _ITEMS[0],
        {**_ITEMS[1], "note": "保留身份字段"},
    ]
    inventories = {
        "movie": {"total": 100, "truncated": False, "unmapped": 1},
        "tv": {"total": 20, "truncated": True, "unmapped": 0},
    }
    sources = [{
        "server_type": "jellyfin",
        "server_name": 123,
        "status": "ready",
        "inventories": inventories,
        "web_url": "https://synthetic.invalid",
        "error": "synthetic private detail",
        "items": [
            None,
            "ignored",
            {**_ITEMS[0], "status": "present", "match": "provider_id", "local_id": "private"},
            {**_ITEMS[1], "status": "possible", "match": "title_year"},
            {**_ITEMS[2], "status": "missing", "match": ""},
            {"tmdb_id": "77", "media_type": "tv", "status": "unknown", "match": None},
        ],
    }]
    before = deepcopy((identities, sources))
    with patch(
        "app.services.inspect_media_identity_batch", side_effect=AssertionError("不应核对")
    ), patch(
        "app.services._configured_media_sources", side_effect=AssertionError("不应扫描")
    ):
        result = summarize_media_identity_batch(identities, sources)

    assert result == {
        "total": 4,
        "counts": {"present": 1, "possible": 1, "indeterminate": 1, "missing": 1},
        "items": [
            {
                **identity,
                "library_status": status,
                "sources": [{
                    "server_type": "jellyfin",
                    "server_name": "123",
                    "status": status,
                    "match": match,
                }],
            }
            for identity, status, match in zip(
                identities,
                ("missing", "indeterminate", "present", "possible"),
                ("none", "none", "provider_id", "title_year"),
            )
        ],
        "sources": [{
            "server_type": "jellyfin",
            "server_name": "123",
            "status": "ready",
            "inventories": inventories,
        }],
    }
    assert (identities, sources) == before
    assert result["sources"][0]["inventories"] is inventories


def test_summarize_batch_keeps_last_duplicate_source_and_item_and_inventory_defaults():
    sources = [
        {"items": [{**_ITEMS[0], "status": "present"}]},
        {
            "inventories": None,
            "items": [
                {**_ITEMS[0], "status": "possible"},
                {**_ITEMS[0], "status": "missing"},
            ],
        },
    ]

    result = summarize_media_identity_batch(_ITEMS[:1], sources)

    assert result == {
        "total": 1,
        "counts": {"present": 0, "possible": 0, "indeterminate": 0, "missing": 1},
        "items": [{
            **_ITEMS[0],
            "library_status": "missing",
            "sources": [
                {"server_type": "", "server_name": "", "status": "missing", "match": "none"},
                {"server_type": "", "server_name": "", "status": "missing", "match": "none"},
            ],
        }],
        "sources": [
            {"server_type": "", "server_name": "", "status": "unavailable", "inventories": {}},
            {"server_type": "", "server_name": "", "status": "unavailable", "inventories": None},
        ],
    }


@pytest.mark.parametrize("items", [[], [None, "ignored"], [{**_ITEMS[0]}], [
    {**_ITEMS[0], "status": "unknown"},
]])
def test_summarize_batch_missing_or_unknown_items_are_indeterminate(items):
    result = summarize_media_identity_batch(_ITEMS[:1], [{"items": items}])

    assert result["counts"]["indeterminate"] == 1
    assert result["items"][0]["library_status"] == "indeterminate"
    assert result["items"][0]["sources"] == [{
        "server_type": "", "server_name": "", "status": "indeterminate", "match": "none",
    }]


@pytest.mark.parametrize("identities", [[], _ITEMS])
def test_summarize_batch_without_sources_keeps_indeterminate_identities(identities):
    assert summarize_media_identity_batch(identities, []) == {
        "total": len(identities),
        "counts": {
            "present": 0, "possible": 0, "indeterminate": len(identities), "missing": 0,
        },
        "items": [
            {**identity, "library_status": "indeterminate", "sources": []}
            for identity in identities
        ],
        "sources": [],
    }


def test_batch_presence_without_sources_keeps_existing_early_return():
    with patch(
        "app.services.inspect_media_identity_batch", return_value=[]
    ) as inspect, patch(
        "app.services.summarize_media_identity_batch",
        side_effect=AssertionError("无媒体源时不应聚合"),
    ) as summarize:
        result = library.presence(_ITEMS)

    inspect.assert_called_once_with(_ITEMS, cancelled=None)
    summarize.assert_not_called()
    assert not result["ok"]
    assert result["status"] == "not_configured"
    assert result["summary"] == "没有可核对的媒体服务器"
    assert result["data"] == {"total": 3, "items": [], "sources": []}
    assert result["suggestions"] == ["请先在设置中启用并配置 Jellyfin 或 Emby。"]
    assert result["error"] == "没有可核对的媒体服务器。"


@pytest.mark.parametrize("status", ["possible", "indeterminate"])
def test_batch_presence_keeps_partial_status_for_uncertain_items(status):
    with patch(
        "app.services.inspect_media_identity_batch",
        return_value=[{"status": "ready", "items": [{**_ITEMS[0], "status": status}]}],
    ):
        result = library.presence(_ITEMS[:1])

    assert result["ok"]
    assert result["status"] == "partial"
    assert result["data"]["counts"][status] == 1
