"""Media Agent 媒体更新核对的语义、路由与 API 安全测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agent.models import Evidence, ToolResult
from app.agent.update_actions import check_library_updates


def _identity(arguments):
    return dict(arguments)


def _audit_result(*, status: str = "updates_available") -> ToolResult:
    return ToolResult(
        ok=status in {"updates_available", "up_to_date"},
        status=status,
        summary="发现 1 集已播但本地尚未收录"
        if status == "updates_available"
        else "未找到",
        data={"query": "黑镜", "missing_count": 1},
        evidence=[
            Evidence("media_servers+tmdb", "安全审计", "2026-08-01T10:00:00+08:00")
        ],
    )


class LibraryUpdateActionTests(unittest.TestCase):
    def test_movie_checks_exact_library_presence_and_offers_safe_resource_followup(
        self,
    ):
        arguments = {
            "query": "沙丘2",
            "media_type": "movie",
            "tmdb_id": "693134",
            "season": None,
            "as_of": "2026-08-01",
        }
        sources = [
            {
                "server_type": "jellyfin",
                "server_name": "客厅媒体库",
                "web_url": "http://private.invalid",
                "items": [
                    SimpleNamespace(
                        type="Movie", name="沙丘 2", display_name="沙丘 2", year="2024"
                    ),
                    SimpleNamespace(
                        type="Episode", name="沙丘2", display_name="沙丘2", year="2024"
                    ),
                    SimpleNamespace(
                        type="Movie",
                        name="沙丘：第二部",
                        display_name="沙丘：第二部",
                        year="2024",
                    ),
                ],
                "error": "",
            }
        ]
        with (
            patch("app.agent.update_actions.audit_series_episodes") as audit,
            patch(
                "app.agent.update_actions.search_media_servers", return_value=sources
            ) as search,
        ):
            result = check_library_updates(arguments)
        audit.assert_not_called()
        search.assert_called_once_with("沙丘2", limit=50)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "comparison_unavailable")
        self.assertEqual(result.data["media_type"], "movie")
        self.assertEqual(result.data["local_match_status"], "found")
        self.assertEqual(result.data["exact_match_count"], 1)
        self.assertEqual(result.data["possible_match_count"], 1)
        self.assertNotIn("web_url", result.data["sources"][0])
        self.assertEqual(
            result.data["resource_followups"],
            [
                {
                    "tool": "indexer.search_resources",
                    "label": "搜索《沙丘2》资源候选",
                    "arguments": {
                        "title": "沙丘2",
                        "media_type": "movie",
                        "year": "2024",
                    },
                }
            ],
        )
        self.assertFalse(result.data["comparison"]["available"])
        self.assertNotIn("693134", result.summary)

    def test_movie_presence_is_honest_for_possible_missing_and_unavailable_sources(
        self,
    ):
        possible = [
            {
                "server_type": "emby",
                "server_name": "Emby",
                "items": [
                    SimpleNamespace(
                        type="Movie",
                        name="Dune Part Two",
                        display_name="Dune Part Two",
                        year="2024",
                    )
                ],
                "error": "",
            }
        ]
        with patch(
            "app.agent.update_actions.search_media_servers", return_value=possible
        ):
            result = check_library_updates(
                {
                    "query": "沙丘2",
                    "media_type": "movie",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.data["local_match_status"], "possible")
        self.assertEqual(result.data["exact_match_count"], 0)
        with patch(
            "app.agent.update_actions.search_media_servers",
            return_value=[
                {
                    "server_type": "jellyfin",
                    "server_name": "Jellyfin",
                    "items": [],
                    "error": "",
                }
            ],
        ):
            result = check_library_updates(
                {
                    "query": "沙丘2",
                    "media_type": "movie",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "not_found")
        self.assertEqual(result.data["local_match_status"], "not_found")
        with patch(
            "app.agent.update_actions.search_media_servers",
            side_effect=RuntimeError("secret"),
        ):
            result = check_library_updates(
                {
                    "query": "沙丘2",
                    "media_type": "movie",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        self.assertNotIn("secret", result.summary)

    def test_movie_presence_caps_public_match_rows_across_all_servers(self):
        sources = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "items": [
                    SimpleNamespace(
                        type="Movie",
                        name=f"候选 {index}",
                        display_name=f"候选 {index}",
                        year="2024",
                    )
                    for index in range(8)
                ],
                "error": "",
            },
            {
                "server_type": "emby",
                "server_name": "Emby",
                "items": [
                    SimpleNamespace(
                        type="Movie",
                        name="额外候选",
                        display_name="额外候选",
                        year="2025",
                    )
                ],
                "error": "",
            },
        ]
        with patch(
            "app.agent.update_actions.search_media_servers", return_value=sources
        ):
            result = check_library_updates(
                {
                    "query": "不存在的精确片名",
                    "media_type": "movie",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertEqual(result.data["possible_match_count"], 9)
        self.assertEqual(result.data["matches_truncated"], 1)
        self.assertEqual(
            sum(source["returned"] for source in result.data["sources"]), 8
        )
        self.assertEqual(result.data["sources"][1]["returned"], 0)

    def test_movie_presence_does_not_claim_not_found_when_source_hits_search_cap(self):
        sources = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "items": [
                    SimpleNamespace(
                        type="Episode",
                        name=f"同名剧集 {index}",
                        display_name=f"同名剧集 {index}",
                        year="2024",
                    )
                    for index in range(50)
                ],
                "error": "",
            }
        ]
        with patch(
            "app.agent.update_actions.search_media_servers", return_value=sources
        ):
            result = check_library_updates(
                {
                    "query": "目标电影",
                    "media_type": "movie",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.data["local_match_status"], "indeterminate")
        self.assertEqual(result.data["search_truncated_server_count"], 1)
        self.assertNotIn("未找到同名电影", result.summary)

    def test_movie_presence_scans_later_servers_before_capping_public_rows(self):
        sources = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "items": [
                    SimpleNamespace(
                        type="Movie",
                        name=f"候选 {index}",
                        display_name=f"候选 {index}",
                        year="2024",
                    )
                    for index in range(8)
                ],
                "error": "",
            },
            {
                "server_type": "emby",
                "server_name": "Emby",
                "items": [
                    SimpleNamespace(
                        type="Movie", name="沙丘 2", display_name="沙丘 2", year="2024"
                    )
                ],
                "error": "",
            },
        ]
        with patch(
            "app.agent.update_actions.search_media_servers", return_value=sources
        ):
            result = check_library_updates(
                {
                    "query": "沙丘2",
                    "media_type": "movie",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertEqual(result.status, "comparison_unavailable")
        self.assertEqual(result.data["local_match_status"], "found")
        self.assertEqual(result.data["exact_match_count"], 1)
        self.assertEqual(result.data["possible_match_count"], 8)
        self.assertEqual(
            sum(source["returned"] for source in result.data["sources"]), 8
        )
        self.assertEqual(result.data["sources"][1]["items"][0]["match"], "exact_title")

    def test_tv_delegates_to_episode_audit_and_labels_definition(self):
        with patch(
            "app.agent.update_actions.audit_series_episodes",
            return_value=_audit_result(),
        ) as audit:
            result = check_library_updates(
                {
                    "query": "黑镜",
                    "media_type": "tv",
                    "tmdb_id": "42009",
                    "season": 7,
                    "as_of": "2026-08-01",
                }
            )
        audit.assert_called_once_with(
            {"query": "黑镜", "tmdb_id": "42009", "season": 7, "as_of": "2026-08-01"}
        )
        self.assertEqual(result.status, "updates_available")
        self.assertEqual(result.data["media_type"], "tv")
        self.assertEqual(
            result.data["check_definition"],
            "aired_normal_episodes_missing_from_enabled_media_servers",
        )

    def test_auto_not_found_does_not_claim_movie_or_series_is_current(self):
        with patch(
            "app.agent.update_actions.audit_series_episodes",
            return_value=_audit_result(status="not_found"),
        ):
            result = check_library_updates(
                {
                    "query": "未知标题",
                    "media_type": "auto",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "cannot_determine")
        self.assertIn("无法可靠判断", result.summary)
