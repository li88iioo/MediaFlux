"""50项跨电影/剧集、跨媒体源的保守聚合与精确刷新绑定。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import services
from app.sdk import library
from app.modules.media_refresh_coordinator import MediaRefreshCoordinator
from app.repositories import media_refresh_queue as queue
from tests.support import isolated_test_database


class InventoryClient:
    def __init__(self, *, mapped="none", truncated=False, fail_tv=False):
        self.mapped = mapped
        self.truncated = truncated
        self.fail_tv = fail_tv
        self.calls = []
        self.closed = 0

    def list_media_identity_inventory(self, media_type, **kwargs):
        self.calls.append((media_type, kwargs))
        if self.fail_tv and media_type == "tv":
            raise OSError("unavailable")
        candidates = [
            SimpleNamespace(tmdb_id=str(i), name=f"Film {i}", year="2026")
            for i in range(1, 51)
            if (media_type == "movie") == bool(i % 2)
            and (self.mapped == "all" or self.mapped == media_type)
        ]
        return SimpleNamespace(
            candidates=candidates,
            truncated=self.truncated,
            total=len(candidates),
            unmapped=0,
        )

    def close(self):
        self.closed += 1


class MediaBatchProjectionAuditTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.identities = [
            {
                "tmdb_id": str(i),
                "media_type": "movie" if i % 2 else "tv",
                "title": f"Film {i}",
                "year": "2026",
            }
            for i in range(1, 51)
        ]

    def inspect(self, first, second):
        with patch.object(
            services,
            "_configured_media_sources",
            return_value=[
                ("emby", "Emby", "https://emby.invalid", first),
                ("jellyfin", "Jellyfin", "https://jf.invalid", second),
            ],
        ):
            result = library.presence(self.identities)
        for client in (first, second):
            self.assertEqual(client.closed, 1)
            self.assertEqual([call[0] for call in client.calls], ["movie", "tv"])
            self.assertTrue(
                all(
                    call[1] == {"max_items": 5000, "page_size": 200}
                    for call in client.calls
                )
            )
        self.assertEqual(result["data"]["total"], 50)
        self.assertEqual(
            [item["tmdb_id"] for item in result["data"]["items"]],
            [str(i) for i in range(1, 51)],
        )
        self.assertNotIn("https://", str(result["data"]))
        return result

    def test_truncated_source_cannot_turn_unseen_tv_into_missing(self):
        result = self.inspect(
            InventoryClient(mapped="movie"), InventoryClient(truncated=True)
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            result["data"]["counts"],
            {"present": 25, "possible": 0, "missing": 0, "indeterminate": 25},
        )

    def test_missing_requires_both_complete_sources_and_keeps_input_order(self):
        result = self.inspect(InventoryClient(), InventoryClient())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["counts"]["missing"], 50)

    def test_failed_source_does_not_hide_other_servers_exact_hits(self):
        result = self.inspect(
            InventoryClient(mapped="movie", fail_tv=True), InventoryClient(mapped="all")
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["counts"]["present"], 50)
        self.assertEqual(
            [source["status"] for source in result["data"]["sources"]],
            ["partial", "ready"],
        )

    def test_ambiguous_binding_keeps_retryable_path_and_never_refreshes_global_library(
        self,
    ):
        queue.enqueue_media_refresh(
            "jellyfin",
            ["/synthetic/Film.mkv"],
            library_binding={"name": "Same"},
            debounce_seconds=0,
        )
        worker = MediaRefreshCoordinator()
        group = queue.claim_due_media_refreshes(owner=worker._owner, force=True)[0]
        client = Mock(display_name="Jellyfin")
        client.list_virtual_folders.return_value = [
            {"id": "one", "name": "Same"},
            {"id": "two", "name": "Same"},
        ]
        with patch.object(worker, "_client_for", return_value=client):
            worker._process_group(group)
        client.refresh_for_paths.assert_not_called()
        client.close.assert_called_once()
        self.assertEqual(queue.media_refresh_queue_status()["retry_wait"], 1)
        retried = queue.claim_due_media_refreshes(owner=worker._owner, force=True)[0]
        self.assertEqual(retried["paths"], ["/synthetic/Film.mkv"])
        self.assertEqual(retried["library_binding"], {"id": "", "name": "Same"})
