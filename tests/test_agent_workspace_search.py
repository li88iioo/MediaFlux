"""Media Agent 工作区标题搜索的安全、路由与 API 回归测试。"""
from __future__ import annotations

import json
import re
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator, is_workspace_search_message
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.agent.workspace_actions import search_workspace, workspace_search_arguments
from app.clients.base import MediaItem
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _identity(arguments):
    return dict(arguments)


def _empty_records(*_args, **_kwargs):
    return {"items": [], "truncated": False}


class WorkspaceSearchArgumentTests(unittest.TestCase):
    def test_arguments_normalize_sections_and_reject_unsafe_shapes(self):
        self.assertEqual(
            workspace_search_arguments({
                "query": "  沙丘２  ",
                "sections": ["downloads", "library"],
            }),
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

    def test_registry_exposes_read_only_strict_tool(self):
        capabilities = {item["name"]: item for item in build_tool_registry().capabilities()}
        spec = capabilities["workspace.search"]
        self.assertEqual(spec["risk"], "read")
        self.assertFalse(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])
        self.assertTrue(spec["parameters"]["properties"]["sections"]["uniqueItems"])


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
                ("PRIVATE-RSS-NAME", "https://private.example/rss?token=PRIVATE-RSS", "PRIVATE-RULE", stamp, stamp),
            ).lastrowid
            conn.execute(
                "INSERT INTO rss_entries(rss_item_id,title,status,processed,pub_date,guid,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (rss_id, "沙丘2 RSS 更新", "pending", 0, stamp, "PRIVATE-GUID", '{"token":"PRIVATE-PAYLOAD"}', stamp),
            )
            conn.execute(
                "INSERT INTO download_log(source,title,path,status,backend_task_id,progress,error,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("qb", "沙丘2 下载", "/private/download/path", "submitted", "PRIVATE-TASK-ID", 42, "token=PRIVATE-DOWNLOAD", stamp),
            )
            conn.execute(
                "INSERT INTO organize_log(source,original_path,new_path,file_id,status,title,media_type,year,error,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("guangya", "/private/original", "/private/new", "PRIVATE-FILE-ID", "success", "沙丘2", "movie", "2024", "PRIVATE-ORGANIZE-ERROR", stamp),
            )
            source_id = conn.execute(
                "INSERT INTO local_media_sources(owner,name,local_root,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("admin", "PRIVATE-SOURCE", "/private/local/root", stamp, stamp),
            ).lastrowid
            conn.execute(
                "INSERT INTO local_media_tasks(owner,source_id,qb_hash,content_path,operation_token,status,tmdb_id,media_type,title,year,error,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("admin", source_id, "PRIVATE-QB-HASH", "/private/content", "PRIVATE-OP-TOKEN", "completed", "PRIVATE-TMDB-ID", "movie", "沙丘2", "2024", "PRIVATE-LOCAL-ERROR", stamp, stamp),
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
            return_value=[{
                "server_type": "jellyfin",
                "server_name": "PRIVATE-SERVER-NAME",
                "items": [media_item],
            }],
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
            "PRIVATE-RSS", "PRIVATE-RULE", "PRIVATE-GUID", "PRIVATE-PAYLOAD",
            "/private", "PRIVATE-TASK-ID", "PRIVATE-DOWNLOAD", "PRIVATE-FILE-ID",
            "PRIVATE-ORGANIZE", "PRIVATE-QB-HASH", "PRIVATE-OP-TOKEN", "PRIVATE-TMDB-ID",
            "PRIVATE-LOCAL", "PRIVATE-MEDIA-ID", "PRIVATE-OVERVIEW", "PRIVATE-IMAGE",
            "PRIVATE-SERVER-NAME", "private.example",
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
        with patch("app.agent.workspace_actions.search_media_servers", return_value=[]), patch(
            "app.agent.workspace_actions.db.search_agent_workspace_rss",
            side_effect=RuntimeError("token=PRIVATE-SOURCE-ERROR"),
        ), patch(
            "app.agent.workspace_actions.db.search_agent_workspace_downloads", side_effect=_empty_records
        ), patch(
            "app.agent.workspace_actions.db.search_agent_workspace_organize", side_effect=_empty_records
        ), patch(
            "app.agent.workspace_actions.db.search_agent_workspace_local_media", side_effect=_empty_records
        ):
            result = search_workspace(workspace_search_arguments({"query": "沙丘2"}))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        rss = next(section for section in result.data["sections"] if section["source"] == "rss")
        self.assertEqual(rss["status"], "unavailable")
        self.assertNotIn("PRIVATE-SOURCE-ERROR", json.dumps(result.to_dict(), ensure_ascii=False))

    def test_untrusted_titles_dates_years_and_hashes_are_redacted(self):
        malicious = (
            "沙丘 file://private/share",
            "沙丘 smb://private/share",
            "沙丘 ../private/file.mkv",
            "沙丘 Bearer PRIVATE-CREDENTIAL",
            "沙丘 token PRIVATE-CREDENTIAL",
            "沙丘 0123456789abcdef0123456789abcdef01234567",
            "沙丘 Downloads/Dune.mkv",
            r"沙丘 Downloads\Dune.mkv",
            "沙丘 www.private.example/library",
            "沙丘 alice:PRIVATE-PASSWORD@private.example",
            "沙丘 ghp_abcdefghijklmnopqrstuvwxyz1234567890",
            "沙丘 tmdb_id=12345678",
        )
        for title in malicious:
            with self.subTest(title=title), patch(
                "app.agent.workspace_actions.db.search_agent_workspace_rss",
                return_value={
                    "items": [{
                        "title": title,
                        "status": "pending",
                        "processed": 0,
                        "pub_date": "https://private.example/token=PRIVATE-DATE",
                        "created_at": "2026-08-01T12:00:00+08:00",
                    }],
                    "truncated": False,
                },
            ):
                result = search_workspace(workspace_search_arguments({"query": "沙丘", "sections": ["rss"]}))
            item = result.data["sections"][0]["items"][0]
            self.assertEqual(item["title"], "RSS 条目")
            self.assertEqual(item["published_at"], "")
            serialized = json.dumps(result.to_dict(), ensure_ascii=False)
            self.assertNotIn("PRIVATE", serialized)

        media_item = MediaItem(id="safe-id", name="沙丘", type="Movie", year="2026token")
        with patch(
            "app.agent.workspace_actions.search_media_servers",
            return_value=[{"server_type": "jellyfin", "items": [media_item]}],
        ):
            result = search_workspace(workspace_search_arguments({"query": "沙丘", "sections": ["library"]}))
        self.assertEqual(result.data["sections"][0]["items"][0]["year"], "")

    def test_organize_real_statuses_and_legacy_names_remain_searchable(self):
        statuses = (
            "interrupted", "partial_failed", "deleted", "reorganizing", "returning", "deleting",
            "reverted", "revert_failed",
        )
        for status in statuses:
            with self.subTest(status=status), patch(
                "app.agent.workspace_actions.db.search_agent_workspace_organize",
                return_value={
                    "items": [{
                        "title": "沙丘2",
                        "media_type": "movie",
                        "year": "2024",
                        "season": None,
                        "episode": None,
                        "status": status,
                        "source": "organize",
                        "created_at": "2026-08-01T12:00:00+08:00",
                    }],
                    "truncated": False,
                },
            ):
                result = search_workspace(
                    workspace_search_arguments({"query": "沙丘2", "sections": ["organize"]})
                )
            self.assertEqual(result.data["sections"][0]["items"][0]["status"], status)

        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO organize_log(source,original_path,new_path,file_id,status,title,original_name,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    "guangya", "/private/legacy", "/private/new", "PRIVATE-ID", "success", "",
                    "Dune.2021.1080p.mkv", db.now(),
                ),
            )
        result = db.search_agent_workspace_organize("Dune", limit=8)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["title"], "")


class WorkspaceSearchRoutingTests(unittest.TestCase):
    def test_natural_language_routing_preserves_existing_search_boundaries(self):
        calls: list[tuple[str, dict]] = []
        registry = ToolRegistry()
        for name in (
            "workspace.search",
            "indexer.search_resources",
            "discovery.search",
            "library.search",
            "downloads.diagnose_queue",
        ):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: (
                    calls.append((tool, dict(arguments))) or ToolResult(True, "success", tool)
                ),
                validator=_identity,
            ))
        agent = AgentOrchestrator(registry)

        self.assertTrue(is_workspace_search_message("全局搜索《沙丘2》"))
        direct = agent.query("全局搜索《沙丘2》")
        self.assertEqual(direct["tool_call"]["name"], "workspace.search")
        self.assertEqual(direct["tool_call"]["arguments"], {"query": "沙丘2"})
        scoped = agent.query("在下载记录和整理日志里找《沙丘2》")
        self.assertEqual(scoped["tool_call"]["name"], "workspace.search")
        self.assertEqual(scoped["tool_call"]["arguments"]["sections"], ["downloads", "organize"])
        self.assertEqual(agent.query("《沙丘2》现在走到哪一步")["tool_call"]["name"], "workspace.search")
        self.assertEqual(agent.query("搜索《沙丘2》的资源")["tool_call"]["name"], "indexer.search_resources")
        self.assertEqual(agent.query("在网上找《沙丘2》")["tool_call"]["name"], "discovery.search")
        self.assertEqual(agent.query("媒体库里有没有《沙丘2》")["tool_call"]["name"], "library.search")
        self.assertEqual(agent.query("诊断下载任务")["tool_call"]["name"], "downloads.diagnose_queue")

        for title in ("资源", "豆瓣", "RSS", "下载任务", "本地媒体"):
            with self.subTest(title=title):
                response = agent.query(f"全局搜索《{title}》")
                self.assertEqual(response["tool_call"]["name"], "workspace.search")
                self.assertEqual(response["tool_call"]["arguments"], {"query": title})

        scoped_title = agent.query("在下载记录里找《豆瓣》")
        self.assertEqual(scoped_title["tool_call"]["name"], "workspace.search")
        self.assertEqual(scoped_title["tool_call"]["arguments"], {
            "query": "豆瓣",
            "sections": ["downloads"],
        })
        self.assertEqual(
            agent.query("在下载任务里找《沙丘2》")["tool_call"]["arguments"]["sections"],
            ["downloads"],
        )
        self.assertEqual(
            agent.query("在本地媒体里找《沙丘2》")["tool_call"]["arguments"]["sections"],
            ["local_media"],
        )
        unquoted = agent.query("整理日志里找本地媒体")
        self.assertEqual(unquoted["tool_call"]["name"], "workspace.search")
        self.assertEqual(unquoted["tool_call"]["arguments"], {
            "query": "本地媒体",
            "sections": ["organize"],
        })
        for message in (
            "全局搜索《下载任务》的状态",
            "工作区搜索《RSS》的状态",
            "全局搜索《本地媒体》的异常",
        ):
            with self.subTest(message=message):
                self.assertEqual(agent.query(message)["tool_call"]["name"], "workspace.search")


class WorkspaceSearchAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        matched = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not matched:
            matched = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not matched:
            raise AssertionError("CSRF token missing")
        return matched.group(1)

    def _login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def test_auth_csrf_validation_and_shared_direct_query_rate_limit(self):
        path = "/api/agent/tools/workspace.search"
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {"query": "沙丘2"}}).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {"query": "沙丘2"}}).status_code, 403)
        headers = {"X-CSRF-Token": csrf}
        with patch("app.agent.workspace_actions.search_media_servers", return_value=[]):
            for _ in range(4):
                response = self.client.post(
                    path,
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "arguments": {"query": "沙丘2", "sections": ["library"]}},
                )
                self.assertEqual(response.status_code, 200, response.text)
            limited = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "全局搜索《沙丘2》"},
            )
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_query_then_direct_share_limit_and_invalid_arguments_do_not_read_sources(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        with patch("app.agent.workspace_actions.search_media_servers", return_value=[]):
            messages = ["全局搜索《下载任务》的状态"] + ["全局搜索《沙丘2》"] * 3
            for message in messages:
                response = self.client.post(
                    "/api/agent/query",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "message": message},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('"name":"workspace.search"', response.text)
            limited = self.client.post(
                "/api/agent/tools/workspace.search",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"query": "沙丘2"}},
            )
        self.assertEqual(limited.status_code, 429, limited.text)

        agent_rate_limiter.reset()
        with patch("app.agent.workspace_actions.search_media_servers") as media_search:
            invalid = self.client.post(
                "/api/agent/tools/workspace.search",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"query": "沙丘2", "token": "PRIVATE"}},
            )
        self.assertEqual(invalid.status_code, 400, invalid.text)
        media_search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
