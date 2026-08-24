"""自动整理、下载恢复和 STRM URL 的增量韧性回归。"""
from __future__ import annotations

import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import database as db
from app.clients.qbittorrent import TorrentAddResult
from app.modules.download_dispatcher import _submit_qb, dispatch_request
from app.modules.download_tracker import DownloadTracker
from app.modules.local_media_service import LocalMediaService
from app.modules.media_proxy import _extract_guangya_file_id
from app.modules.playgy_signing import sign_playgy
from app.modules.scraper import MatchResult
from app.modules.strm import build_play_url
from tests.support import IsolatedDatabaseTestCase, release_parse_result


class _LocalScraper:
    supports_parent_path = True

    def __init__(self, confidence: float = 0.75, status: str = "matched"):
        self.confidence = confidence
        self.status = status

    def match(self, filename: str, parent_path: str = "", *, media_type_hint: str = ""):
        return MatchResult(
            tmdb_id="1", title="Movie", year="2026", media_type="movie",
            confidence=self.confidence, status=self.status,
        )

    def match_from_tmdb(self, tmdb_id: str, media_type: str):
        return MatchResult(
            tmdb_id=str(tmdb_id), title="Movie", year="2026", media_type=media_type,
            confidence=1.0, status="matched",
        )

    def parse_media(self, filename: str, parent_path: str = "", match=None):
        return release_parse_result(
            {"season": None, "episode": None, "title": "Movie", "year": "2026", "type": "movie"},
            filename=filename, parent_path=parent_path,
        )

    def get_detail(self, tmdb_id: str, media_type: str):
        return {"genres": [{"id": 28}], "origin_country": ["US"], "release_date": "2026-01-01"}


class PipelineResilienceIncrementalTests(IsolatedDatabaseTestCase):
    def test_download_notification_is_claimed_by_exactly_one_tracker(self):
        request_id, _ = db.create_download_request(
            "notification-claim", "magnet", title="并发通知"
        )
        db.update_download_request(
            request_id,
            status="completed",
            notification_event_status="completed",
            notification_delivery_status="pending",
        )
        barrier = threading.Barrier(3)

        def claim():
            barrier.wait()
            return db.claim_download_request_notification(request_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            barrier.wait()
            claims = [future.result(timeout=5) for future in futures]

        winners = [item for item in claims if item is not None]
        self.assertEqual(len(winners), 1)
        token = str(winners[0]["token"])
        self.assertFalse(db.finalize_download_request_notification(
            request_id, "stale-token", delivered=True,
        ))
        self.assertTrue(db.finalize_download_request_notification(
            request_id, token, delivered=True,
        ))
        row = db.get_download_request(request_id)
        self.assertEqual(row["notification_delivery_status"], "sent")
        self.assertEqual(row["notification_lease_token"], "")

    def test_guangya_organize_claim_survives_qb_manual_review(self):
        request_id, _ = db.create_download_request(
            "mixed-terminal-organize", "magnet", title="混合终态资源"
        )
        db.update_download_request(
            request_id,
            targets="both",
            status="manual_review",
            qb_status="manual_review",
            gy_status="completed",
            organize_started=0,
        )

        self.assertTrue(db.claim_download_request_organize(request_id))
        row = db.get_download_request(request_id)
        self.assertEqual(int(row["organize_started"]), 1)
        self.assertEqual(str(row["status"]), "manual_review")

    def _local_inspection(self, confidence: float = 0.75):
        root = tempfile.TemporaryDirectory()
        source_root = Path(root.name) / "downloads"
        target_root = Path(root.name) / "movies"
        source_root.mkdir()
        target_root.mkdir()
        (source_root / "Movie.2026.mkv").write_bytes(b"movie")
        source_id = db.create_local_media_source(
            name=f"source-{uuid.uuid4().hex}", qb_profile="", qb_path_prefix="",
            local_root=str(source_root), stable_seconds=0, owner="admin",
        )
        db.upsert_local_library_target(source_id, "movie", str(target_root), owner="admin")
        service = LocalMediaService(scraper=_LocalScraper(confidence))
        inspection = service.inspect_source("admin", source_id, source_root)
        return root, service, inspection

    def test_low_confidence_only_blocks_automatic_local_preview(self):
        root, service, inspection = self._local_inspection()
        try:
            manual = service.preview("admin", inspection["inspection_id"], automatic=False)
            automatic = service.preview("admin", inspection["inspection_id"], automatic=True)
        finally:
            root.cleanup()

        self.assertEqual(manual["status"], "planned")
        self.assertEqual(automatic["status"], "requires_manual")
        self.assertIn("90%", automatic["reason"])
        self.assertEqual(automatic["candidates"][0]["tmdb_id"], "1")
        self.assertEqual(automatic["files"], [{"name": "Movie.2026.mkv"}])
        self.assertTrue(automatic["snapshot_digest"])
        self.assertTrue(automatic["rules_snapshot"])

    def test_high_confidence_legacy_match_without_status_remains_automatic(self):
        root, service, inspection = self._local_inspection(confidence=1.0)
        service.scraper.status = ""
        try:
            preview = service.preview("admin", inspection["inspection_id"], automatic=True)
        finally:
            root.cleanup()

        self.assertEqual(preview["status"], "planned")

    def test_explicit_tmdb_selection_bypasses_automatic_confidence_gate(self):
        root, service, inspection = self._local_inspection()
        try:
            preview = service.preview(
                "admin", inspection["inspection_id"], tmdb_id="1",
                media_type="movie", automatic=True,
            )
        finally:
            root.cleanup()

        self.assertEqual(preview["status"], "planned")

    def test_qb_unknown_outcome_is_persisted_without_becoming_retryable_failure(self):
        row = {
            "id": 91, "title": "Unknown", "source_value": "magnet:?xt=urn:btih:abcdef",
            "kind": "magnet", "torrent_data": None,
        }
        with patch("app.modules.download_dispatcher.get", side_effect=lambda key, default="": {
            "QB_URL": "http://qb", "QB_USERNAME": "", "QB_PASSWORD": "",
            "QB_API_KEY": "", "TG_QB_CATEGORY": "", "TG_QB_SAVE_PATH": "",
        }.get(key, default)), patch(
            "app.modules.download_dispatcher.QBittorrentClient.add_torrent_detailed",
            return_value=TorrentAddResult(False, "qb_outcome_unknown", False),
        ):
            backend = _submit_qb(row)
        self.assertFalse(backend["ok"])
        self.assertFalse(backend["retryable"])
        self.assertEqual(backend["failure_code"], "qb_outcome_unknown")
        self.assertIn("勿直接重复提交", backend["error"])

        with patch("app.database.get_download_request", return_value=row), patch(
            "app.database.claim_download_request", return_value=True,
        ), patch("app.modules.download_dispatcher._submit_qb", return_value=backend), patch(
            "app.database.update_download_request_and_sync_media_admission",
        ) as update, patch("app.database.add_download_log") as add_log:
            result = dispatch_request(91, "qb")

        self.assertEqual(result["status"], "submitted")
        self.assertFalse(result["ok"])
        self.assertEqual(update.call_args.kwargs["qb_status"], "outcome_unknown")
        self.assertEqual(add_log.call_args.kwargs["status"], "outcome_unknown")

    def test_tracker_does_not_age_missing_task_while_backend_is_unavailable(self):
        tracker = DownloadTracker()
        row = {
            "id": 92, "status": "submitted", "title": "Task", "chat_id": "",
            "qb_status": "submitted", "gy_status": "", "qb_task_id": "hash",
            "gy_task_id": "", "source_value": "magnet:?xt=urn:btih:hash",
            "organize_started": 0,
        }
        with patch("app.database.update_download_request_and_sync_media_admission") as update, patch(
            "app.modules.download_tracker.send",
        ):
            tracker._update_request(row, [], [], qb_available=False)
        update.assert_not_called()

    def test_gy_submitted_without_task_id_ages_into_manual_review(self):
        """提交时未拿到任务 ID 且云端匹配不到时，不得永远停在 submitted。"""
        tracker = DownloadTracker()
        base_row = {
            "id": 94, "status": "submitted", "title": "Task", "chat_id": "",
            "qb_status": "", "gy_status": "submitted", "qb_task_id": "",
            "gy_task_id": "", "gy_task_ids": "[]", "gy_batch_count": 0,
            "source_value": "magnet:?xt=urn:btih:gyhash", "organize_started": 0,
        }
        with patch("app.database.update_download_request_and_sync_media_admission") as update, patch(
            "app.modules.download_tracker.send",
        ):
            tracker._update_request(dict(base_row), [], [], gy_available=True)
        self.assertIn("gy_task_missing_since", update.call_args.kwargs)
        self.assertNotIn("gy_status", update.call_args.kwargs)

        stale = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        aged_row = {**base_row, "gy_task_missing_since": stale}
        with patch("app.database.update_download_request_and_sync_media_admission") as update, patch(
            "app.modules.download_tracker.send",
        ):
            tracker._update_request(aged_row, [], [], gy_available=True)
        self.assertEqual(update.call_args.kwargs["gy_status"], "manual_review")

    def test_tracker_moves_expired_missing_task_to_manual_review(self):
        tracker = DownloadTracker()
        row = {
            "id": 93, "status": "downloading", "title": "Task", "chat_id": "",
            "qb_status": "downloading", "gy_status": "", "qb_task_id": "hash",
            "qb_task_missing_since": (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "gy_task_id": "", "source_value": "magnet:?xt=urn:btih:hash",
            "organize_started": 0,
        }
        with patch("app.database.update_download_request_and_sync_media_admission") as update, patch(
            "app.modules.download_tracker.send",
        ):
            tracker._update_request(row, [], [], qb_available=True)
        self.assertEqual(update.call_args.kwargs["qb_status"], "manual_review")
        self.assertEqual(update.call_args.kwargs["status"], "manual_review")

    def test_manual_review_notification_retries_until_delivery_succeeds(self):
        request_id, _ = db.create_download_request(
            f"notify-{uuid.uuid4().hex}", "magnet", title="Notify",
            source_value="magnet:?xt=urn:btih:notify", chat_id="100",
        )
        db.update_download_request(
            request_id,
            status="submitted",
            qb_status="outcome_unknown",
            qb_task_id="",
            gy_status="",
        )
        tracker = DownloadTracker()
        row = db.get_download_request(request_id)
        with patch("app.modules.download_tracker.send", return_value=False):
            tracker._update_request(row, [], [], qb_available=True)

        pending = db.get_download_request(request_id)
        self.assertEqual(pending["status"], "manual_review")
        self.assertEqual(pending["notification_delivery_status"], "retry_wait")
        self.assertEqual(int(pending["notification_attempts"]), 1)

        db.update_download_request(
            request_id,
            title="Changed after terminal state",
            notification_next_retry_at="2000-01-01 00:00:00",
        )
        retry_row = db.get_download_request(request_id)
        with patch("app.modules.download_tracker.send", return_value=True) as send:
            tracker._update_request(retry_row, [], [], qb_available=True)

        delivered = db.get_download_request(request_id)
        self.assertEqual(delivered["notification_delivery_status"], "sent")
        self.assertTrue(delivered["notification_sent_at"])
        send.assert_called_once()
        event = send.call_args.args[0]
        self.assertEqual(dict(event.fields)["任务"], "Notify")

    def test_manual_review_targets_require_explicit_successor_resubmit(self):
        request_id, _ = db.create_download_request(
            f"retry-{uuid.uuid4().hex}", "magnet", title="Retry",
            source_value="magnet:?xt=urn:btih:retry",
        )
        stale = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.update_download_request(
            request_id, targets="both", status="manual_review",
            qb_status="manual_review", gy_status="manual_review",
            qb_task_missing_since=stale, gy_task_missing_since=stale,
        )

        claimed = db.claim_download_request_targets(request_id, "both")
        row = db.get_download_request(request_id)

        self.assertEqual(claimed, ())
        self.assertEqual(row["status"], "manual_review")
        self.assertEqual(row["qb_status"], "manual_review")
        self.assertEqual(row["gy_status"], "manual_review")
        self.assertEqual(row["qb_task_missing_since"], stale)
        self.assertEqual(row["gy_task_missing_since"], stale)

    def test_tracker_cursor_reaches_active_rows_beyond_first_hundred(self):
        ids = []
        for index in range(150):
            request_id, _ = db.create_download_request(
                f"cursor-{uuid.uuid4().hex}", "magnet", title=f"Task {index}",
                source_value=f"magnet:?xt=urn:btih:{index}",
            )
            db.update_download_request(request_id, status="submitted")
            ids.append(request_id)
        tracker = DownloadTracker()
        visited: list[int] = []
        with patch.object(tracker, "_update_request", side_effect=lambda row, *_args, **_kwargs: visited.append(int(row["id"]))):
            self.assertEqual(tracker.run_once(), 100)
            self.assertEqual(tracker.run_once(), 100)
        self.assertTrue(set(ids).issubset(set(visited)))

    def test_init_db_recovers_interrupted_share_without_replaying_cloud_write(self):
        request_id, _ = db.create_share_transfer_request(
            f"share-{uuid.uuid4().hex}", title="Share", origin="web",
        )
        db.update_download_request(request_id, status="submitting", gy_status="submitting")
        db.init_db()
        row = db.get_download_request(request_id)
        self.assertEqual(row["status"], "manual_review")
        self.assertEqual(row["gy_status"], "manual_review")
        self.assertIn("勿直接重试", row["error"])

    def test_init_db_recovers_interrupted_regular_submit_without_backend_replay(self):
        request_id, _ = db.create_download_request(
            f"regular-{uuid.uuid4().hex}", "magnet", title="Regular",
            source_value="magnet:?xt=urn:btih:regular",
        )
        self.assertTrue(db.claim_download_request(request_id, "both"))
        db.update_download_request(request_id, qb_status="submitted")

        db.init_db()
        row = db.get_download_request(request_id)

        self.assertEqual(row["status"], "manual_review")
        self.assertEqual(row["qb_status"], "submitted")
        self.assertEqual(row["gy_status"], "manual_review")
        self.assertIn("远端接收结果未知", row["error"])
        self.assertIn("勿直接重复提交", row["error"])
        attention_ids = {
            int(item["id"])
            for item in db.list_download_requests_requiring_attention(limit=20, offset=0)
        }
        self.assertIn(request_id, attention_ids)
        active_ids = {int(item["id"]) for item in db.list_active_download_requests()}
        self.assertIn(request_id, active_ids)

        first_error = row["error"]
        db.init_db()
        self.assertEqual(db.get_download_request(request_id)["error"], first_error)

    def test_strm_url_encodes_reserved_filename_without_changing_signature_payload(self):
        url = build_play_url(
            "http://media.example/base", "文件-id", "etag%?#", 123,
            "片 名?#%.mkv",
        )
        parsed = urlsplit(url)
        self.assertNotIn(" ", parsed.path)
        self.assertNotIn("?", parsed.path)
        self.assertNotIn("#", parsed.path)
        decoded_parts = [unquote(part) for part in parsed.path.split("/")[-4:]]
        self.assertEqual(decoded_parts, ["文件-id", "etag%?#", "123", "片 名?#%.mkv"])
        query = parse_qs(parsed.query)
        self.assertEqual(query["v"], ["1"])
        self.assertEqual(query["sig"], [sign_playgy("文件-id", "etag%?#", 123)])

    def test_strm_url_with_slash_identifier_reaches_proxy_with_original_identity(self):
        from app.routes.proxy import router as playgy_router

        raw_file_id = "folder/file-id"
        raw_etag = "etag/part"
        url = build_play_url(
            "http://testserver", raw_file_id, raw_etag, 123, "Movie.mkv",
        )
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual(query.get("enc"), ["b64"])
        self.assertEqual(_extract_guangya_file_id(url), raw_file_id)

        app = FastAPI()
        app.include_router(playgy_router)
        client_stub = Mock(logged_in=True)
        client_stub.get_download_url.return_value = "https://storage.invalid/movie"
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        with patch("app.routes.proxy.GuangYaClient", return_value=client_stub):
            with TestClient(app) as client:
                response = client.get(target, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        client_stub.get_download_url.assert_called_once_with(
            raw_file_id, timeout=8.0, raise_timeout=True
        )

    def test_playgy_head_returns_local_probe_metadata_after_provider_validation(self):
        from app.routes.proxy import router as playgy_router

        url = build_play_url(
            "http://testserver", "file-id", "etag", 123, "Movie.mkv",
        )
        parsed = urlsplit(url)
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        app = FastAPI()
        app.include_router(playgy_router)
        client_stub = Mock(logged_in=True)
        client_stub.get_download_url.return_value = "https://storage.invalid/movie"

        with patch("app.routes.proxy.GuangYaClient", return_value=client_stub):
            with TestClient(app) as client:
                response = client.head(
                    target,
                    headers={
                        "If-Range": '"probe-etag"',
                        "Range": "bytes=0-0",
                        "User-Agent": "Jellyfin-Android/2.6.3",
                    },
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["content-length"], "123")
        self.assertNotIn("content-range", response.headers)
        self.assertTrue(response.headers["etag"].startswith('"mf-'))
        self.assertEqual(response.headers["content-type"], "video/x-matroska")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("location", response.headers)
        self.assertEqual(response.content, b"")
        client_stub.get_download_url.assert_called_once_with(
            "file-id", timeout=8.0, raise_timeout=True
        )


    def test_playgy_head_honors_matching_if_range_validator(self):
        from app.routes.proxy import router as playgy_router

        url = build_play_url(
            "http://testserver", "matching-range-file", "etag", 123,
            "Movie.mkv",
        )
        parsed = urlsplit(url)
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        app = FastAPI()
        app.include_router(playgy_router)
        client_stub = Mock(logged_in=True)
        client_stub.get_download_url.return_value = (
            "https://storage.invalid/matching-range-file"
        )

        with patch("app.routes.proxy.GuangYaClient", return_value=client_stub):
            with TestClient(app) as client:
                metadata = client.head(target, follow_redirects=False)
                response = client.head(
                    target,
                    headers={
                        "If-Range": metadata.headers["etag"],
                        "Range": "bytes=0-0",
                    },
                    follow_redirects=False,
                )

        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["etag"], metadata.headers["etag"])
        self.assertEqual(response.headers["content-length"], "1")
        self.assertEqual(response.headers["content-range"], "bytes 0-0/123")
        self.assertNotIn("location", response.headers)
        self.assertEqual(response.content, b"")


    def test_playgy_head_rejects_invalid_range_without_redirecting(self):
        from app.routes.proxy import router as playgy_router

        url = build_play_url(
            "http://testserver", "range-file", "etag", 123, "Movie.mkv",
        )
        parsed = urlsplit(url)
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        app = FastAPI()
        app.include_router(playgy_router)
        client_stub = Mock(logged_in=True)
        client_stub.get_download_url.return_value = (
            "https://storage.invalid/range-file"
        )

        with patch("app.routes.proxy.GuangYaClient", return_value=client_stub):
            with TestClient(app) as client:
                response = client.head(
                    target,
                    headers={"Range": "bytes=999-1000"},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], "bytes */123")
        self.assertNotIn("location", response.headers)
        self.assertEqual(response.content, b"")


    def test_playgy_head_reports_provider_logout_instead_of_stale_success(self):
        from app.routes.proxy import router as playgy_router

        url = build_play_url(
            "http://testserver", "logged-out-file", "etag", 123, "Movie.mkv",
        )
        parsed = urlsplit(url)
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        app = FastAPI()
        app.include_router(playgy_router)
        client_stub = Mock(logged_in=False)

        with patch("app.routes.proxy.GuangYaClient", return_value=client_stub):
            with TestClient(app) as client:
                response = client.head(target, follow_redirects=False)

        self.assertEqual(response.status_code, 503)
        client_stub.get_download_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
