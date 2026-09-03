"""Media Agent 工作区标题搜索的安全、路由与 API 回归测试。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.workspace_actions import search_workspace, workspace_search_arguments
from app.clients.base import MediaItem
from tests.support import IsolatedDatabaseTestCase


def _identity(arguments):
    return dict(arguments)


def _empty_records(*_args, **_kwargs):
    return {"items": [], "truncated": False}


class WorkspaceSearchArgumentTests(unittest.TestCase):
    def test_arguments_normalize_sections_and_reject_unsafe_shapes(self):
        self.assertEqual(
            workspace_search_arguments(
                {"query": "  沙丘２  ", "sections": ["downloads", "library"]}
            ),
            {"query": "沙丘2", "sections": ["library", "downloads"]},
        )
        self.assertEqual(
            workspace_search_arguments({"query": "沙丘2"})["sections"],
            ["library", "rss", "downloads", "organize", "local_media"],
        )
        invalid = (
            {},
            {"query": ""},
            {"query": "x\ny"},
            {"query": "x" * 121},
            {"query": "x", "sections": []},
            {"query": "x", "sections": "rss"},
            {"query": "x", "sections": ["rss", "rss"]},
            {"query": "x", "sections": ["unknown"]},
            {"query": "x", "token": "secret"},
            {"query": "Bearer PRIVATE-CREDENTIAL /private/data"},
            {"query": "www.private.example/library"},
            {"query": "ghp_abcdefghijklmnopqrstuvwxyz1234567890"},
            {"query": "tmdb_id=12345678"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                workspace_search_arguments(arguments)


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

    def test_real_database_projection_and_media_server_projection_do_not_leak(self):
        self._seed_sensitive_records()
        media_item = MediaItem(
            id="PRIVATE-MEDIA-ID",
            name="沙丘2",
            type="Movie",
            year="2024",
            overview="token=PRIVATE-OVERVIEW",
            web_url="https://private.example/media/PRIVATE-MEDIA-ID",
            primary_image="PRIVATE-IMAGE",
        )
        with patch(
            "app.agent.workspace_actions.search_media_servers",
            return_value=[
                {
                    "server_type": "jellyfin",
                    "server_name": "PRIVATE-SERVER-NAME",
                    "items": [media_item],
                }
            ],
        ):
            result = search_workspace(workspace_search_arguments({"query": "沙丘2"}))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.data["returned"], 5)
        self.assertTrue(result.data["network_accessed"])
        self.assertTrue(result.data["database_accessed"])
        self.assertFalse(result.data["filesystem_scanned"])
        self.assertEqual(
            [section["source"] for section in result.data["sections"]],
            ["library", "rss", "downloads", "organize", "local_media"],
        )
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "PRIVATE-RSS",
            "PRIVATE-RULE",
            "PRIVATE-GUID",
            "PRIVATE-PAYLOAD",
            "/private",
            "PRIVATE-TASK-ID",
            "PRIVATE-DOWNLOAD",
            "PRIVATE-FILE-ID",
            "PRIVATE-ORGANIZE",
            "PRIVATE-QB-HASH",
            "PRIVATE-OP-TOKEN",
            "PRIVATE-TMDB-ID",
            "PRIVATE-LOCAL",
            "PRIVATE-MEDIA-ID",
            "PRIVATE-OVERVIEW",
            "PRIVATE-IMAGE",
            "PRIVATE-SERVER-NAME",
            "private.example",
        ):
            self.assertNotIn(secret, serialized)

    def test_like_wildcards_are_treated_as_literal_title_characters(self):
        stamp = db.now()
        with db.get_conn() as conn:
            rss_id = conn.execute(
                "INSERT INTO rss_items(name,urls,created_at,updated_at) VALUES(?,?,?,?)",
                ("demo", "https://example.invalid/rss", stamp, stamp),
            ).lastrowid
            conn.execute(
                "INSERT INTO rss_entries(rss_item_id,title,status,created_at) VALUES(?,?,?,?)",
                (rss_id, "100% 完成", "pending", stamp),
            )
            conn.execute(
                "INSERT INTO rss_entries(rss_item_id,title,status,created_at) VALUES(?,?,?,?)",
                (rss_id, "普通条目", "pending", stamp),
            )
        result = db.search_agent_workspace_rss("%", limit=8)
        self.assertEqual([item["title"] for item in result["items"]], ["100% 完成"])

    def test_source_failure_is_isolated_and_error_detail_is_hidden(self):
        with (
            patch("app.agent.workspace_actions.search_media_servers", return_value=[]),
            patch(
                "app.agent.workspace_actions.db.search_agent_workspace_rss",
                side_effect=RuntimeError("token=PRIVATE-SOURCE-ERROR"),
            ),
            patch(
                "app.agent.workspace_actions.db.search_agent_workspace_downloads",
                side_effect=_empty_records,
            ),
            patch(
                "app.agent.workspace_actions.db.search_agent_workspace_organize",
                side_effect=_empty_records,
            ),
            patch(
                "app.agent.workspace_actions.db.search_agent_workspace_local_media",
                side_effect=_empty_records,
            ),
        ):
            result = search_workspace(workspace_search_arguments({"query": "沙丘2"}))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        rss = next(

                section
                for section in result.data["sections"]
                if section["source"] == "rss"

        )
        self.assertEqual(rss["status"], "unavailable")
        self.assertNotIn(
            "PRIVATE-SOURCE-ERROR", json.dumps(result.to_dict(), ensure_ascii=False)
        )

    def test_untrusted_titles_dates_years_and_hashes_are_redacted(self):
        malicious = (
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
        )
        for title in malicious:
            with (
                self.subTest(title=title),
                patch(
                    "app.agent.workspace_actions.db.search_agent_workspace_rss",
                    return_value={
                        "items": [
                            {
                                "title": title,
                                "status": "pending",
                                "processed": 0,
                                "pub_date": "https://private.example/token=PRIVATE-DATE",
                                "created_at": "2026-08-01T12:00:00+08:00",
                            }
                        ],
                        "truncated": False,
                    },
                ),
            ):
                result = search_workspace(
                    workspace_search_arguments({"query": "沙丘", "sections": ["rss"]})
                )
            item = result.data["sections"][0]["items"][0]
            self.assertEqual(item["title"], "RSS 条目")
            self.assertEqual(item["published_at"], "")
            serialized = json.dumps(result.to_dict(), ensure_ascii=False)
            self.assertNotIn("PRIVATE", serialized)
        media_item = MediaItem(
            id="safe-id", name="沙丘", type="Movie", year="2026token"
        )
        with patch(
            "app.agent.workspace_actions.search_media_servers",
            return_value=[{"server_type": "jellyfin", "items": [media_item]}],
        ):
            result = search_workspace(
                workspace_search_arguments({"query": "沙丘", "sections": ["library"]})
            )
        self.assertEqual(result.data["sections"][0]["items"][0]["year"], "")

    def test_organize_real_statuses_and_legacy_names_remain_searchable(self):
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
        for status in statuses:
            with (
                self.subTest(status=status),
                patch(
                    "app.agent.workspace_actions.db.search_agent_workspace_organize",
                    return_value={
                        "items": [
                            {
                                "title": "沙丘2",
                                "media_type": "movie",
                                "year": "2024",
                                "season": None,
                                "episode": None,
                                "status": status,
                                "source": "organize",
                                "created_at": "2026-08-01T12:00:00+08:00",
                            }
                        ],
                        "truncated": False,
                    },
                ),
            ):
                result = search_workspace(
                    workspace_search_arguments(
                        {"query": "沙丘2", "sections": ["organize"]}
                    )
                )
            self.assertEqual(result.data["sections"][0]["items"][0]["status"], status)
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO organize_log(source,original_path,new_path,file_id,status,title,original_name,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "guangya",
                    "/private/legacy",
                    "/private/new",
                    "PRIVATE-ID",
                    "success",
                    "",
                    "Dune.2021.1080p.mkv",
                    db.now(),
                ),
            )
        result = db.search_agent_workspace_organize("Dune", limit=8)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["title"], "")
