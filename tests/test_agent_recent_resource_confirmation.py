"""最近缺集资源推荐的会话化确认接力测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agent.models import ToolResult
from app.agent.recent_resource_candidates import (
    RecentResourceCandidateStore,
    attach_resource_candidate_reference,
    public_candidate_projection,
    restore_resource_candidate_reference,
    validate_safe_resource_snapshot,
)
from app.indexers.models import IndexerItem
from app.indexers.result_store import IndexerResultStore


def _candidate(
    result_id: str,
    *,
    title: str = "Example.S02E03.1080p.WEB-DL",
    rank: int = 1,
    score: int = 300,
) -> dict:
    return {
        "result_id": result_id,
        "title": title,
        "site_id": "mikan",
        "site_name": "Mikan",
        "rank": rank,
        "score": score,
        "confidence": "high",
        "match": "exact_episode",
        "download_state": "ready",
        "reasons": ["精确匹配 S02E03", "可直接提交下载"],
        "warnings": [],
        "tags": {"resolution": "1080p", "media": "WEB-DL"},
        "magnet": "magnet:?xt=must-not-leak",
        "path": "/private/download",
    }


def _single_result(*candidates: dict) -> ToolResult:
    selected = candidates[0] if candidates else None
    return ToolResult(
        True,
        "success",
        "searched",
        data={
            "verification": {
                "title": "The Show",
                "tmdb_id": "12345",
                "season": 2,
                "episode": 3,
                "as_of": "2026-08-03",
                "library_name": "动漫库",
                "verified_missing": True,
                "sources": [{"path": "/secret/library"}],
            },
            "search": {
                "items": [{"private_url": "https://secret.example"}],
                "recommendation": {
                    "selected": selected,
                    "alternatives": list(candidates[1:]),
                },
            },
        },
    )


def _generic_result(*candidates: dict) -> ToolResult:
    items = []
    for candidate in candidates:
        item = dict(candidate)
        item.setdefault("size_text", "1.2 GiB")
        item.setdefault("download_kinds", ["magnet"])
        items.append(item)
    return ToolResult(
        True,
        "success",
        f"找到 {len(items)} 项资源",
        data={"query": "The Show", "items": items},
    )


def _season_result(*candidates: dict) -> ToolResult:
    return ToolResult(
        True,
        "success",
        "searched season",
        data={
            "episodes": [
                {
                    "season": 2,
                    "episode": 3,
                    "episode_label": "S02E03",
                    "search": {
                        "recommendation": {
                            "selected": candidates[0] if candidates else None,
                            "alternatives": list(candidates[1:]),
                        }
                    },
                }
            ],
            "token": "must-not-leak",
        },
    )


class RecentResourceCandidateStoreTests(unittest.TestCase):
    def test_private_reference_restores_verified_candidate_after_process_restart(self):
        original_store = IndexerResultStore()
        result_id = original_store.put(
            IndexerItem(
                site_id="mikan",
                site_name="Mikan",
                title="The.Show.S02E03.1080p.WEB-DL",
                detail_url="https://private.invalid/detail",
                download_state="ready",
                download_kinds=("magnet",),
                magnet="magnet:?xt=urn:btih:" + "b" * 40,
            )
        )
        result = _single_result(_candidate(result_id))

        attach_resource_candidate_reference(result, result_store=original_store)
        private_value = result.references[0].value
        restarted_store = IndexerResultStore()
        with patch(
            "app.indexers.runtime.get_indexer_service",
            return_value=SimpleNamespace(result_store=restarted_store),
        ):
            snapshot = restore_resource_candidate_reference(private_value)

        self.assertIsNotNone(snapshot)
        self.assertEqual(
            snapshot["candidates"][0]["_verification_context"]["episode"], 3
        )
        restored = restarted_store.get(result_id)
        self.assertEqual(restored.title, "The.Show.S02E03.1080p.WEB-DL")
        self.assertTrue(str(restored.magnet).startswith("magnet:?xt=urn:btih:"))

    def test_snapshot_is_owner_bound_short_lived_and_safely_projected(self):
        now = [100.0]
        store = RecentResourceCandidateStore(ttl_seconds=10, clock=lambda: now[0])
        store.capture(
            owner="session-a",
            result=_single_result(
                _candidate("resource-result-0001"),
                _candidate("resource-result-0002", rank=2, score=250),
            ),
        )
        snapshot = store.get(owner="session-a")
        self.assertEqual([item["position"] for item in snapshot["candidates"]], [1, 2])
        self.assertEqual(snapshot["candidates"][0]["episode_label"], "S02E03")
        self.assertEqual(snapshot["candidates"][0]["result_id"], "resource-result-0001")
        self.assertEqual(
            snapshot["candidates"][0]["reasons"], ["精确匹配 S02E03", "可直接提交下载"]
        )
        self.assertEqual(
            snapshot["candidates"][0]["tags"],
            {"media": "WEB-DL", "resolution": "1080p"},
        )
        serialized = repr(snapshot)
        for secret in ("magnet:", "/private", "/secret", "secret.example"):
            self.assertNotIn(secret, serialized)
        self.assertIsNone(store.get(owner="session-b"))
        snapshot["candidates"].clear()
        self.assertEqual(len(store.get(owner="session-a")["candidates"]), 2)
        now[0] = 111.0
        self.assertIsNone(store.get(owner="session-a"))

    def test_verified_missing_context_is_internal_and_invalid_context_is_dropped(self):
        store = RecentResourceCandidateStore()
        store.capture(
            owner="session-a", result=_single_result(_candidate("resource-result-0001"))
        )
        candidate = store.get(owner="session-a")["candidates"][0]
        self.assertEqual(
            candidate["_verification_context"],
            {
                "title": "The Show",
                "tmdb_id": "12345",
                "season": 2,
                "episode": 3,
                "as_of": "2026-08-03",
                "library_name": "动漫库",
            },
        )
        self.assertNotIn(
            "_verification_context", public_candidate_projection(candidate)
        )
        self.assertNotIn("library_name", public_candidate_projection(candidate))
        invalid = _single_result(_candidate("resource-result-0002"))
        invalid.data["verification"]["verified_missing"] = False
        store.capture(owner="session-a", result=invalid)
        self.assertIsNone(
            store.get(owner="session-a")["candidates"][0]["_verification_context"]
        )

    def test_exact_search_id_keeps_older_snapshot_addressable(self):
        store = RecentResourceCandidateStore()
        first_search_id = store.capture(
            owner="session-a",
            result=_generic_result(_candidate("resource-first-0001", title="First")),
        )
        second_search_id = store.capture(
            owner="session-a",
            result=_generic_result(_candidate("resource-second-001", title="Second")),
        )
        self.assertEqual(store.get(owner="session-a")["search_id"], second_search_id)
        self.assertEqual(
            store.get(owner="session-a", search_id=first_search_id)["candidates"][0][
                "title"
            ],
            "First",
        )
        self.assertIsNone(
            store.get(owner="session-a", search_id="rs_missing_snapshot_0001")
        )

    def test_new_empty_search_replaces_old_candidates(self):
        store = RecentResourceCandidateStore()
        store.capture(
            owner="session-a", result=_single_result(_candidate("resource-result-0001"))
        )
        store.capture(owner="session-a", result=_single_result())
        self.assertEqual(store.get(owner="session-a")["candidates"], [])

    def test_season_projection_deduplicates_result_handles(self):
        store = RecentResourceCandidateStore()
        duplicate = _candidate("resource-result-0001")
        store.capture(owner="session-a", result=_season_result(duplicate, duplicate))
        self.assertEqual(len(store.get(owner="session-a")["candidates"]), 1)

    def test_generic_indexer_results_are_projected_for_natural_followup(self):
        store = RecentResourceCandidateStore()
        candidate = _candidate("generic-resource-0001", title="The.Show.1080p")
        candidate.update(
            {
                "size_text": "1.2 GiB",
                "media_title": "The Show",
                "episode_label": "S02E03",
                "subscription_number": 7,
                "magnet": "magnet:?xt=must-not-leak",
                "private_url": "https://secret.example/item",
            }
        )
        store.capture(owner="session-a", result=_generic_result(candidate))
        snapshot = store.get(owner="session-a")
        self.assertEqual(snapshot["candidates"][0]["position"], 1)
        self.assertEqual(snapshot["candidates"][0]["title"], "The.Show.1080p")
        self.assertEqual(snapshot["candidates"][0]["size_text"], "1.2 GiB")
        self.assertEqual(snapshot["candidates"][0]["media_title"], "The Show")
        self.assertEqual(snapshot["candidates"][0]["episode_label"], "S02E03")
        self.assertEqual(snapshot["candidates"][0]["subscription_number"], 7)
        self.assertNotIn("magnet:", repr(snapshot))
        self.assertNotIn("secret.example", repr(snapshot))

    def test_generic_projection_collects_first_twelve_actionable_items(self):
        store = RecentResourceCandidateStore()
        unavailable = []
        for index in range(12):
            candidate = _candidate(
                f"unavailable-resource-{index:04d}", title=f"Unavailable {index}"
            )
            candidate["download_state"] = "unavailable"
            unavailable.append(candidate)
        actionable = [
            _candidate(f"actionable-resource-{index:04d}", title=f"Actionable {index}")
            for index in range(13)
        ]
        store.capture(
            owner="session-a", result=_generic_result(*unavailable, *actionable)
        )
        candidates = store.get(owner="session-a")["candidates"]
        self.assertEqual(len(candidates), 12)
        self.assertEqual(candidates[0]["title"], "Actionable 0")
        self.assertEqual(candidates[-1]["title"], "Actionable 11")
        self.assertEqual(
            [candidate["position"] for candidate in candidates], list(range(1, 13))
        )

    def test_generic_candidate_snapshot_requires_current_single_format(self):
        store = RecentResourceCandidateStore()
        store.capture(
            owner="session-a",
            result=_generic_result(
                _candidate("generic-resource-0001", title="The.Show.1080p")
            ),
        )
        snapshot = store.get(owner="session-a")
        self.assertEqual(snapshot["candidates"][0]["download_kinds"], ["magnet"])
        self.assertEqual(validate_safe_resource_snapshot(snapshot), snapshot)
        missing_search_id = dict(snapshot)
        missing_search_id.pop("search_id")
        self.assertIsNone(validate_safe_resource_snapshot(missing_search_id))
        missing_capability = {
            **snapshot,
            "candidates": [
                {
                    key: value
                    for key, value in snapshot["candidates"][0].items()
                    if key != "download_kinds"
                }
            ],
        }
        self.assertIsNone(validate_safe_resource_snapshot(missing_capability))
