"""末轮逆审：边界长度与真实种子在统一重试执行核中仍保留原合同。"""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import database as db
from app.indexers.models import IndexerMediaSearchRequest, IndexerSearchRequest
from app.indexers.query_plan import build_site_queries
from app.modules import download_dispatcher as dispatcher
from tests.support import isolated_test_database


class TenPassReverseContractTests(unittest.TestCase):
    def test_generated_queries_respect_budget_around_every_suffix_boundary(self):
        for length in (1, 12, 109, 110, 112, 113, 114, 119, 120):
            for season, episode in ((0, 1), (100, 1000), (None, 1000)):
                title = "A" * length
                request = IndexerMediaSearchRequest.create(
                    title=title, season=season, episode=episode
                )
                for site in ("mikan", "nyaa", "tpb", "sukebei", "btbtla"):
                    queries = build_site_queries(site, request)
                    self.assertLessEqual(len(queries), 3)
                    for query in queries:
                        self.assertEqual(
                            IndexerSearchRequest.create(query).query,
                            query,
                            (length, site, query),
                        )
                    self.assertIn(f"E{episode:02d}", queries[0])
                self.assertEqual(request.title, title)

    def test_torrent_retry_uses_intact_payload_and_keeps_unknown_peer_identity(self):
        with isolated_test_database():
            payload = (
                b"d4:infod6:lengthi1e4:name5:a.mkv12:piece lengthi16384e6:pieces20:"
                + b"a" * 20
                + b"ee"
            )
            item = dispatcher.torrent_download_input("audit.torrent", payload)
            request_id = dispatcher.create_request(item, "", "audit")["id"]
            db.update_download_request(
                request_id,
                status="downloading",
                targets="both",
                qb_status="failed",
                gy_status="outcome_unknown",
                gy_task_id="unknown-peer",
                gy_staging_name="keep-stage",
            )
            before = dict(db.get_download_request(request_id))
            client = Mock()
            client.add_torrent_detailed.return_value = SimpleNamespace(
                ok=True, failure_code="", task_ids=[], retryable=False
            )
            with (
                patch.object(
                    dispatcher,
                    "get",
                    side_effect=lambda key, default="": (
                        "http://qb.invalid" if key == "QB_URL" else default
                    ),
                ),
                patch.object(
                    dispatcher,
                    "analyze_offline_url",
                    return_value=SimpleNamespace(allowed=True, reason=""),
                ),
                patch.object(dispatcher, "QBittorrentClient", return_value=client),
                patch.object(dispatcher, "_submit_guangya") as peer,
            ):
                result = dispatcher.resubmit_download_request(request_id, "qb")
            self.assertTrue(result["ok"], result)
            self.assertFalse(result["created"])
            self.assertEqual(result["request_id"], request_id)
            peer.assert_not_called()
            client.add_torrent_detailed.assert_called_once()
            self.assertEqual(
                client.add_torrent_detailed.call_args.kwargs["torrents"], payload
            )
            self.assertEqual(client.add_torrent_detailed.call_args.kwargs["urls"], "")
            client.close.assert_called_once()
            after = dict(db.get_download_request(request_id))
            for key in (
                "request_key",
                "gy_status",
                "gy_task_id",
                "gy_staging_name",
                "torrent_data",
            ):
                self.assertEqual(after[key], before[key], key)
            self.assertEqual(
                after["qb_task_id"], dispatcher.parse_torrent_metadata(payload)[1]
            )
