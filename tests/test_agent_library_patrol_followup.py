"""全库巡检结果安全接力测试。"""

from __future__ import annotations

import unittest

from app.agent.models import ToolResult
from app.agent.recent_patrol import RecentPatrolStore


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
        result = _patrol_result(
            findings=[
                _finding(
                    missing=[
                        {"season": 2, "episode": 4},
                        {"season": 2, "episode": 3},
                        {"season": 3, "episode": 1},
                    ]
                ),
                _finding(title="不可靠", tmdb_id="999", status="inconclusive"),
                _finding(title="已截断", tmdb_id="998", truncated=True),
            ],
            status="inconclusive",
            ok=False,
        )
        store.capture(owner="session-a", result=result)
        snapshot = store.get(owner="session-a")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["patrol_status"], "inconclusive")
        self.assertEqual(
            snapshot["options"],
            [
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
            ],
        )
        serialized = repr(snapshot)
        self.assertNotIn("private", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("token", serialized)
        self.assertIsNone(store.get(owner="session-b"))
        now[0] = 110.0
        self.assertIsNone(store.get(owner="session-a"))

    def test_latest_patrol_replaces_previous_snapshot(self):
        store = RecentPatrolStore()
        store.capture(
            owner="owner", result=_patrol_result(findings=[_finding(title="旧剧")])
        )
        store.capture(
            owner="owner", result=_patrol_result(findings=[_finding(title="新剧")])
        )
        self.assertEqual(store.get(owner="owner")["options"][0]["title"], "新剧")
        store.capture(
            owner="owner", result=_patrol_result(findings=[], status="up_to_date")
        )
        self.assertEqual(store.get(owner="owner")["options"], [])
