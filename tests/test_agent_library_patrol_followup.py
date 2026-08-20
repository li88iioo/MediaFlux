"""全库巡检结果安全接力测试。"""
from __future__ import annotations

from datetime import date
import re
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_recent_library_patrol_resource_message,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.recent_patrol import RecentPatrolStore
from app.agent.registry import ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _identity(arguments):
    return dict(arguments)


def _patrol_result(*, findings=None, status="updates_available", ok=True):
    return ToolResult(
        ok,
        status,
        "patrol",
        data={
            "as_of": "2026-08-01",
            "findings_truncated": False,
            "findings": list(findings or []),
            "sources": [{"server_name": "private", "path": "/secret/media"}],
        },
        error="private upstream details",
    )


def _finding(
    title="示例剧",
    tmdb_id="12345",
    missing=None,
    *,
    status="updates_available",
    truncated=False,
):
    return {
        "title": title,
        "tmdb_id": tmdb_id,
        "status": status,
        "missing_count": len(missing or []),
        "missing_sample": list(missing or [{"season": 2, "episode": 3}]),
        "missing_sample_truncated": truncated,
        "sources": [{"path": "/private"}],
        "token": "must-not-leak",
    }


class RecentPatrolStoreTests(unittest.TestCase):
    def test_snapshot_is_session_bound_short_lived_and_safely_projected(self):
        now = [100.0]
        store = RecentPatrolStore(ttl_seconds=10, clock=lambda: now[0])
        result = _patrol_result(findings=[
            _finding(missing=[
                {"season": 2, "episode": 4},
                {"season": 2, "episode": 3},
                {"season": 3, "episode": 1},
            ]),
            _finding(title="不可靠", tmdb_id="999", status="inconclusive"),
            _finding(title="已截断", tmdb_id="998", truncated=True),
        ], status="inconclusive", ok=False)

        store.capture(owner="session-a", result=result)
        snapshot = store.get(owner="session-a")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["patrol_status"], "inconclusive")
        self.assertEqual(snapshot["options"], [
            {
                "position": 1,
                "title": "示例剧",
                "tmdb_id": "12345",
                "season": 2,
                "missing_count": 2,
                "episode_sample": [3, 4],
            },
            {
                "position": 2,
                "title": "示例剧",
                "tmdb_id": "12345",
                "season": 3,
                "missing_count": 1,
                "episode_sample": [1],
            },
        ])
        serialized = repr(snapshot)
        self.assertNotIn("private", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("token", serialized)
        self.assertIsNone(store.get(owner="session-b"))

        now[0] = 110.0
        self.assertIsNone(store.get(owner="session-a"))

    def test_latest_patrol_replaces_previous_snapshot(self):
        store = RecentPatrolStore()
        store.capture(owner="owner", result=_patrol_result(findings=[_finding(title="旧剧")]))
        store.capture(owner="owner", result=_patrol_result(findings=[_finding(title="新剧")]))
        self.assertEqual(store.get(owner="owner")["options"][0]["title"], "新剧")

        store.capture(owner="owner", result=_patrol_result(findings=[], status="up_to_date"))
        self.assertEqual(store.get(owner="owner")["options"], [])


class PatrolFollowupOrchestratorTests(unittest.TestCase):
    @staticmethod
    def _agent(*, findings):
        calls = []
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="library.audit_library_episodes",
            description="patrol",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda arguments: _patrol_result(findings=findings),
            validator=_identity,
        ))
        registry.register(ToolSpec(
            name="library.search_missing_season_resources",
            description="search",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda arguments: (
                calls.append(dict(arguments))
                or ToolResult(True, "success", "searched", data=arguments)
            ),
            validator=_identity,
        ))
        return AgentOrchestrator(registry), calls

    def test_persisted_status_can_seed_the_existing_resource_followup(self):
        calls = []
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="library.patrol_status",
            description="status",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda arguments: _patrol_result(findings=[_finding()]),
            validator=_identity,
        ))
        registry.register(ToolSpec(
            name="library.search_missing_season_resources",
            description="search",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda arguments: (
                calls.append(dict(arguments))
                or ToolResult(True, "success", "searched", data=arguments)
            ),
            validator=_identity,
        ))
        agent = AgentOrchestrator(registry)

        status = agent.query("上次自动缺集巡检结果", owner="session-a")
        followup = agent.query(
            "把刚才巡检发现的缺集找资源",
            owner="session-a",
        )

        self.assertEqual(status["tool_call"]["name"], "library.patrol_status")
        self.assertEqual(
            followup["tool_call"]["name"],
            "library.search_missing_season_resources",
        )
        self.assertEqual(calls, [{
            "query": "示例剧",
            "tmdb_id": "12345",
            "season": 2,
            "max_episodes": 3,
            "limit_per_episode": 8,
            "as_of": "2026-08-01",
        }])

    def test_intent_is_explicit_and_rejects_configuration_questions(self):
        for message in (
            "把刚才巡检发现的缺集找资源",
            "搜索上次巡检第 2 个缺集的资源",
            "给巡检结果第1项找种子",
        ):
            self.assertTrue(is_recent_library_patrol_resource_message(message), message)
        for message in (
            "搜索示例剧资源",
            "怎么配置巡检结果找资源",
            "不要给刚才巡检结果找资源",
            "巡检整个媒体库有没有缺集",
        ):
            self.assertFalse(is_recent_library_patrol_resource_message(message), message)

        agent, calls = self._agent(findings=[_finding()])
        agent.query("巡检整个媒体库有没有缺集", owner="session-a")
        cancelled = agent.query("不要给刚才巡检结果找资源", owner="session-a")
        self.assertEqual(cancelled["result"]["status"], "unsupported")
        self.assertEqual(calls, [])

    def test_single_candidate_continues_with_existing_season_tool(self):
        agent, calls = self._agent(findings=[_finding()])
        agent.query("巡检整个媒体库有没有缺集", owner="session-a")
        response = agent.query("把刚才巡检发现的缺集找资源", owner="session-a")
        self.assertEqual(response["tool_call"]["name"], "library.search_missing_season_resources")
        self.assertEqual(calls, [{
            "query": "示例剧",
            "tmdb_id": "12345",
            "season": 2,
            "max_episodes": 3,
            "limit_per_episode": 8,
            "as_of": "2026-08-01",
        }])

    def test_multiple_candidates_require_selection_and_honor_explicit_index(self):
        agent, calls = self._agent(findings=[
            _finding(title="甲剧", tmdb_id="101", missing=[{"season": 1, "episode": 2}]),
            _finding(title="乙剧", tmdb_id="202", missing=[{"season": 3, "episode": 4}]),
        ])
        agent.query("巡检整个媒体库有没有缺集", owner="session-a")

        selection = agent.query("把刚才巡检发现的缺集找资源", owner="session-a")
        self.assertEqual(selection["result"]["status"], "selection_required")
        self.assertEqual([item["title"] for item in selection["result"]["data"]["options"]], ["甲剧", "乙剧"])
        self.assertEqual(calls, [])

        selected = agent.query("给巡检结果第2项找种子", owner="session-a")
        self.assertEqual(selected["tool_call"]["name"], "library.search_missing_season_resources")
        self.assertEqual(calls[0]["query"], "乙剧")
        self.assertEqual(calls[0]["season"], 3)

        invalid = agent.query("给巡检结果第9项找种子", owner="session-a")
        self.assertEqual(invalid["result"]["status"], "selection_required")
        self.assertEqual(len(calls), 1)

    def test_missing_or_cross_session_snapshot_fails_closed(self):
        agent, calls = self._agent(findings=[_finding()])
        agent.query("巡检整个媒体库有没有缺集", owner="session-a")
        response = agent.query("把刚才巡检发现的缺集找资源", owner="session-b")
        self.assertEqual(response["result"]["status"], "precondition_failed")
        self.assertEqual(calls, [])


class PatrolFollowupAPITests(IsolatedDatabaseTestCase):
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

    def test_direct_patrol_can_be_followed_and_shares_weighted_search_limit(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        patrol = _patrol_result(findings=[_finding()])
        audit = ToolResult(
            True,
            "updates_available",
            "audit",
            data={
                "title": "示例剧",
                "tmdb_id": "12345",
                "missing_count": 1,
                "missing_sample": [{"season": 2, "episode": 3}],
                "missing_sample_truncated": False,
            },
        )
        searched = ToolResult(True, "success", "searched", data={
            "query": "示例剧 S02E03",
            "items": [{
                "result_id": "patrol-result-id",
                "site_id": "nyaa",
                "site_name": "Nyaa",
                "title": "示例剧 S02E03 1080p WEB-DL 简中",
                "seeders": 12,
                "download_state": "ready",
                "download_kinds": ["magnet"],
            }],
            "sites_attempted": ["nyaa"],
            "sites_succeeded": ["nyaa"],
            "errors": [],
            "partial": False,
            "cached": False,
            "has_more": False,
        })
        with patch("app.agent.tools.audit_library_episodes", return_value=patrol), patch(
            "app.agent.episode_resource_actions.audit_series_episodes", return_value=audit
        ), patch(
            "app.agent.episode_resource_actions.search_resources", return_value=searched
        ):
            direct = self.client.post(
                "/api/agent/tools/library.audit_library_episodes",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"max_series": 5, "as_of": date(2026, 8, 1).isoformat()}},
            )
            followup = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "把刚才巡检发现的缺集找资源"},
            )
            blocked = self.client.post(
                "/api/agent/tools/library.search_missing_season_resources",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"query": "示例剧", "season": 2}},
            )
            limited = self.client.post(
                "/api/agent/tools/library.search_missing_episode_resources",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {
                    "query": "示例剧", "season": 2, "episode": 3,
                }},
            )
        self.assertEqual(direct.status_code, 200, direct.text)
        self.assertEqual(followup.status_code, 200, followup.text)
        self.assertEqual(
            followup.json()["tool_call"]["name"],
            "library.search_missing_season_resources",
        )
        episode_search = followup.json()["result"]["data"]["episodes"][0]["search"]
        self.assertEqual(episode_search["recommendation"]["status"], "recommended")
        self.assertEqual(episode_search["download_plan"]["result_id"], "patrol-result-id")
        self.assertFalse(episode_search["download_plan"]["auto_submit"])
        self.assertEqual(blocked.status_code, 200, blocked.text)
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_followup_requires_auth_and_csrf(self):
        unauthenticated = self.client.post(
            "/api/agent/query",
            json={"session_id": "test_session_identifier_0001", "message": "把刚才巡检发现的缺集找资源"},
        )
        self.assertIn(unauthenticated.status_code, (401, 403))

        self._login()
        missing_csrf = self.client.post(
            "/api/agent/query",
            json={"session_id": "test_session_identifier_0001", "message": "把刚才巡检发现的缺集找资源"},
        )
        self.assertEqual(missing_csrf.status_code, 403, missing_csrf.text)


if __name__ == "__main__":
    unittest.main()
