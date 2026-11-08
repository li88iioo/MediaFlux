"""公共工作区搜索的SQLite、分页、真实ID与来源失败回归。"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app import database as db
from app.clients.base import MediaItem
from app.modules import workspace_search as service
from tests.support import IsolatedDatabaseTestCase


class WorkspaceSearchDataTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            for table in (
                "local_media_tasks",
                "local_library_targets",
                "local_media_sources",
                "rss_entries",
                "rss_items",
                "download_log",
                "download_requests",
                "organize_log",
            ):
                conn.execute(f"DELETE FROM {table}")

    def _seed_sensitive_records(self):
        stamp = db.now()
        with db.get_conn() as conn:
            rss_id = conn.execute(
                "INSERT INTO rss_items(name,urls,exclude_keywords,created_at,updated_at) VALUES(?,?,?,?,?)",
                (
                    "PRIVATE-RSS-NAME",
                    "https://private.example/rss?token=PRIVATE-RSS",
                    "PRIVATE-RULE",
                    stamp,
                    stamp,
                ),
            ).lastrowid
            conn.execute(
                "INSERT INTO rss_entries(rss_item_id,title,status,processed,pub_date,guid,payload,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    rss_id,
                    "沙丘2 RSS 更新",
                    "pending",
                    0,
                    stamp,
                    "PRIVATE-GUID",
                    '{"token":"PRIVATE-PAYLOAD"}',
                    stamp,
                ),
            )
            conn.execute(
                "INSERT INTO download_log(source,title,path,status,backend_task_id,progress,error,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "qb",
                    "沙丘2 下载",
                    "/private/download/path",
                    "submitted",
                    "PRIVATE-TASK-ID",
                    42,
                    "token=PRIVATE-DOWNLOAD",
                    stamp,
                ),
            )
            conn.execute(
                "INSERT INTO organize_log(source,original_path,new_path,file_id,status,title,media_type,year,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "guangya",
                    "/private/original",
                    "/private/new",
                    "PRIVATE-FILE-ID",
                    "success",
                    "沙丘2",
                    "movie",
                    "2024",
                    "PRIVATE-ORGANIZE-ERROR",
                    stamp,
                ),
            )
            source_id = conn.execute(
                "INSERT INTO local_media_sources(owner,name,local_root,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("admin", "PRIVATE-SOURCE", "/private/local/root", stamp, stamp),
            ).lastrowid
            conn.execute(
                "INSERT INTO local_media_tasks(owner,source_id,qb_hash,content_path,operation_token,status,tmdb_id,media_type,title,year,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "admin",
                    source_id,
                    "PRIVATE-QB-HASH",
                    "/private/content",
                    "PRIVATE-OP-TOKEN",
                    "completed",
                    "PRIVATE-TMDB-ID",
                    "movie",
                    "沙丘2",
                    "2024",
                    "PRIVATE-LOCAL-ERROR",
                    stamp,
                    stamp,
                ),
            )

    def test_public_sources_preserve_real_identifiers_without_private_payload(self):
        self._seed_sensitive_records()
        media_item = MediaItem(
            id="media-real-id", name="沙丘2", type="Movie", year="2024"
        )
        with patch(
            "app.services._search_sources",
            return_value=[
                {
                    "server_type": "jellyfin",
                    "server_name": "Cinema",
                    "items": [media_item],
                    "total": 1,
                    "has_more": False,
                    "next_offset": None,
                }
            ],
        ):
            result = service.search("沙丘２")
        assert result["ok"] and result["status"] == "success"
        data = result["data"]
        assert data["returned"] == 5 and data["network_accessed"]
        assert data["database_accessed"] and not data["filesystem_scanned"]
        sections = {section["source"]: section for section in data["sections"]}
        assert sections["library"]["items"][0]["id"] == "media-real-id"
        assert sections["rss"]["items"][0]["id"] > 0
        assert sections["downloads"]["items"][0]["record_type"] == "log"
        assert sections["downloads"]["items"][0]["id"] > 0
        assert sections["organize"]["items"][0]["file_id"] == "PRIVATE-FILE-ID"
        assert sections["local_media"]["items"][0]["id"] > 0
        text = json.dumps(result)
        for secret in (
            "PRIVATE-RSS",
            "PRIVATE-RULE",
            "PRIVATE-GUID",
            "PRIVATE-PAYLOAD",
            "/private",
            "PRIVATE-TASK-ID",
            "PRIVATE-DOWNLOAD",
            "PRIVATE-ORGANIZE",
            "PRIVATE-OP-TOKEN",
            "PRIVATE-LOCAL-ERROR",
        ):
            assert secret not in text

    def test_sqlite_more_than_eight_rows_all_pages_ids_and_literal_wildcards(self):
        stamp = db.now()
        with db.get_conn() as conn:
            subscription = conn.execute(
                "INSERT INTO rss_items(name,urls,created_at,updated_at) VALUES(?,?,?,?)",
                ("demo", "https://example.invalid/rss", stamp, stamp),
            ).lastrowid
            ids = [
                conn.execute(
                    "INSERT INTO rss_entries(rss_item_id,title,status,created_at) VALUES(?,?,?,?)",
                    (subscription, f"Dune 100% {index}", "pending", stamp),
                ).lastrowid
                for index in range(23)
            ]
        read_ids, offset = [], 0
        while True:
            result = service.search("%", ["rss"], limit=7, offset=offset)
            data = result["data"]
            (section,) = data["sections"]
            read_ids.extend(item["id"] for item in section["items"])
            assert section["has_more"] == data["has_more"]
            if not data["has_more"]:
                assert data["next_offset"] is None
                break
            offset = data["next_offset"]
        assert read_ids == list(reversed(ids)) and len(set(read_ids)) == 23
        assert service.search("Dune", ["rss"])["data"]["returned"] == 20
        assert db.search_workspace_rss("_")["items"] == []

    def test_download_union_pagination_keeps_request_ids_distinct_from_log_ids(self):
        stamp = db.now()
        with db.get_conn() as conn:
            request, _ = db.create_download_request(
                "workspace-search", "magnet", title="Dune", origin="agent"
            )
            log = conn.execute(
                "INSERT INTO download_log(source,title,status,request_id,created_at) VALUES(?,?,?,?,?)",
                ("qb", "Dune", "submitted", request, stamp),
            ).lastrowid
        first = service.search("Dune", ["downloads"], limit=1)
        second = service.search(
            "Dune", ["downloads"], limit=1, offset=first["data"]["next_offset"]
        )
        rows = (
            first["data"]["sections"][0]["items"]
            + second["data"]["sections"][0]["items"]
        )
        assert {(row["record_type"], row["id"]) for row in rows} == {
            ("log", log),
            ("request", request),
        }
        assert all(row["request_id"] == request for row in rows)
        assert not second["data"]["has_more"]

    def test_failures_keep_other_sources_and_unknown_pagination(self):
        self._seed_sensitive_records()
        with patch.object(
            db, "search_workspace_rss", side_effect=RuntimeError("token=PRIVATE-ERROR")
        ):
            result = service.search("沙丘2", ["rss", "downloads"])
        assert result["ok"] and result["status"] == "partial"
        assert result["data"]["returned"] == 1
        assert result["data"]["has_more"] is None
        assert result["data"]["unavailable_sources"] == ["rss"]
        assert "PRIVATE-ERROR" not in json.dumps(result)
        with patch.object(db, "search_workspace_rss", side_effect=RuntimeError):
            result = service.search("沙丘2", ["rss"])
        assert not result["ok"] and result["status"] == "unavailable"
        assert result["data"]["has_more"] is None

    def test_missing_or_null_items_are_not_empty(self):
        for raw in ({}, {"items": None}):
            with patch.object(db, "search_workspace_rss", return_value=raw):
                result = service.search("沙丘", ["rss"])
            assert result["status"] == "unavailable"

    def test_media_partial_and_server_pagination_survive_with_no_flat_limit(self):
        good = {
            "server_type": "jellyfin",
            "items": [
                MediaItem(id=f"j-{i}", name="Dune", type="Movie") for i in range(2)
            ],
            "total": 3,
            "has_more": True,
            "next_offset": 2,
        }
        bad = {"server_type": "emby", "items": [], "error": "offline"}
        with patch("app.services._search_sources", return_value=[good, bad]) as reader:
            result = service.search("Dune", ["library"], limit=2)
        reader.assert_called_once_with(
            "Dune", 2, offset=0, search_by="title", media_type="all", count_only=False
        )
        assert result["status"] == "partial" and result["data"]["has_more"] is True
        (section,) = result["data"]["sections"]
        assert section["next_offset"] == 2 and len(section["sources"]) == 2
        assert [row["id"] for row in section["items"]] == ["j-0", "j-1"]
        good2 = {
            **good,
            "server_type": "emby",
            "items": [MediaItem(id="e-1", name="Dune", type="Movie")],
            "total": 1,
            "has_more": False,
            "next_offset": None,
        }
        with patch("app.services._search_sources", return_value=[good, good2]):
            result = service.search("Dune", ["library"], limit=2)
        assert result["data"]["returned"] == 3

    def test_organize_legacy_names_and_all_business_statuses(self):
        stamp = db.now()
        statuses = (
            "interrupted",
            "partial_failed",
            "deleted",
            "reorganizing",
            "returning",
            "deleting",
            "reverted",
            "revert_failed",
        )
        with db.get_conn() as conn:
            for status in statuses:
                conn.execute(
                    "INSERT INTO organize_log(source,original_path,new_path,file_id,status,title,original_name,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        "guangya",
                        "/private/old",
                        "/private/new",
                        status,
                        status,
                        "",
                        "Dune.2021.mkv",
                        stamp,
                    ),
                )
        result = service.search("Dune", ["organize"])
        assert {row["status"] for row in result["data"]["sections"][0]["items"]} == set(
            statuses
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": ""},
        {"query": "x\ny"},
        {"query": "x" * 121},
        {"query": 1},
        {"query": "Bearer PRIVATE-CREDENTIAL /private/data"},
        {"query": "www.private.example/library"},
        {"query": "ghp_abcdefghijklmnopqrstuvwxyz1234567890"},
        {"query": "tmdb_id=12345678"},
        {"query": "Dune", "sections": []},
        {"query": "Dune", "sections": "rss"},
        {"query": "Dune", "sections": ["rss", "RSS"]},
        {"query": "Dune", "sections": ["unknown"]},
        {"query": "Dune", "limit": True},
        {"query": "Dune", "limit": 0},
        {"query": "Dune", "limit": 51},
        {"query": "Dune", "offset": -1},
        {"query": "Dune", "offset": False},
    ],
)
def test_search_rejects_invalid_arguments_before_read(arguments, monkeypatch):
    monkeypatch.setattr(
        db,
        "search_workspace_rss",
        lambda *_a, **_k: pytest.fail("invalid request read DB"),
    )
    with pytest.raises(ValueError):
        service.search(**arguments)


@pytest.mark.parametrize(
    "title",
    [
        "沙丘 file://private/share",
        "沙丘 smb://private/share",
        "沙丘 ../private/file.mkv",
        "沙丘 Bearer PRIVATE-CREDENTIAL",
        "沙丘 token PRIVATE-CREDENTIAL",
        "沙丘 0123456789abcdef0123456789abcdef01234567",
        "沙丘 Downloads/Dune.mkv",
        "沙丘 Downloads\\Dune.mkv",
        "沙丘 www.private.example/library",
        "沙丘 alice:PRIVATE-PASSWORD@private.example",
        "沙丘 ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        "沙丘 tmdb_id=12345678",
    ],
)
def test_sensitive_record_text_is_redacted_but_identity_retained(title, monkeypatch):
    monkeypatch.setattr(
        db,
        "search_workspace_rss",
        lambda *_a, **_k: {
            "items": [
                {
                    "id": 41,
                    "rss_item_id": 9,
                    "title": title,
                    "status": "pending",
                    "pub_date": "token=PRIVATE-DATE",
                }
            ],
            "has_more": False,
            "next_offset": None,
        },
    )
    result = service.search("沙丘", ["rss"])
    (item,) = result["data"]["sections"][0]["items"]
    assert item["id"] == 41 and item["rss_item_id"] == 9
    assert item["title"] == "RSS 条目" and item["published_at"] == ""
    assert "PRIVATE" not in json.dumps(result)
