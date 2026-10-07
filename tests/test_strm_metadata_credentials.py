"""真实凭据快照/SQLite + 假SDK：元数据线程必须跟随凭据轮换，无真实网络。"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaClient
from app.modules import strm_metadata_worker as metadata
from app.modules.strm import STRM_SUBDIR
from tests.support import isolated_test_database
from tests.test_strm_metadata_queue import _Response


class TokenRawClient:
    def __init__(self, access_token=None, refresh_token=None, device_id=None):
        self.token = access_token or ""
        self.refresh_token_value = refresh_token or ""
        self.device_id = device_id or "fixture"
        self.token_expires_at = None
        self.closed = 0
        self.info_calls = 0
        self.download_calls = 0
        self.before_detail = None

    def refresh_token(self, refresh_token=None):
        self.token += "-rotated"
        self.refresh_token_value += "-rotated"
        self.token_expires_at = time.time() + 7200
        return {"access_token": self.token, "refresh_token": self.refresh_token_value,
                "expires_in": 7200}

    def fs_detail(self, file_id):
        self.info_calls += 1
        if self.before_detail:
            self.before_detail()
        return {"code": 0, "data": {"fileId": file_id, "fileName": "Movie.nfo",
                                   "resType": 1, "size": 8, "etag": "v1", "parentId": "source"}}

    def download_url(self, file_id):
        self.download_calls += 1
        return {"code": 0, "data": {"signedURL": "https://metadata.invalid/file"}}

    def close(self):
        self.closed += 1
        return True


class MetadataCredentialTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.token = self.root / "token.json"
        self.login()
        self.enterContext(patch("app.clients.guangya._load_raw", return_value=TokenRawClient))
        self.worker = metadata.STRMMetadataWorker()
        self.addCleanup(self.worker.stop)
        self.factory = self.enterContext(patch.object(
            metadata, "GuangYaClient", side_effect=lambda: GuangYaClient(token_file=self.token),
        ))
        self.enterContext(patch.object(metadata, "get_bool", return_value=True))
        self.enterContext(patch.object(metadata, "get", side_effect=lambda key, default="": {
            "STRM_ROOT": str(self.root / "strm"), "STRM_METADATA_EXTS": "nfo",
        }.get(key, default)))
        self.enterContext(patch.object(self.worker, "_flush_media_refresh"))
        self.download = self.enterContext(patch("app.modules.strm.requests.get", return_value=_Response()))
        self.enterContext(patch("httpx.Client.request", side_effect=AssertionError("real network forbidden")))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("real network forbidden")))
        self.job = {"source_id": "source", "source_name": "整理", "file_id": "meta",
                    "parent_id": "source", "filename": "Movie.nfo", "etag": "v1", "size": 8,
                    "rel_dir": "Movie", "target_rel_path": f"{STRM_SUBDIR}/Movie/Movie.nfo"}
        db.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
        self.target = self.root / "strm" / STRM_SUBDIR / "Movie" / "Movie.nfo"

    def login(self):
        self.token.write_text(json.dumps({"access_token": "fake-access", "refresh_token": "fake-refresh",
                                          "device_id": "fixture", "expires_at": time.time() + 7200}))

    def external_client(self):
        client = GuangYaClient(token_file=self.token)
        self.addCleanup(client.close)
        return client

    def row(self):
        return dict(db.list_strm_metadata_queue(status="all")[0])

    def make_due(self):
        with db.get_conn() as conn:
            conn.execute("UPDATE strm_metadata_queue SET next_attempt_at='2000-01-01 00:00:00'")

    def assert_no_file_failure(self):
        self.assertEqual(self.row()["attempts"], 0)
        self.assertEqual(db.list_strm_failures(status="all"), [])
        self.assertEqual(self.worker._failed_session, 0)
        self.assertEqual(self.worker._consecutive_failures, 0)
        self.assertEqual(self.worker._breaker_until, 0)

    def test_rotation_replaces_stale_client_even_during_old_breaker(self):
        old = self.worker._runtime_client()
        raw = old._raw
        self.external_client().refresh_now()
        self.assertFalse(old.credentials_current)
        self.worker._breaker_until = time.monotonic() + 120
        self.worker._consecutive_failures = 4
        self.assertTrue(self.worker._process_one())
        self.assertIsNot(self.worker._client, old)
        self.assertEqual(raw.closed, 1)
        self.assertEqual(self.factory.call_count, 2)
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assert_no_file_failure()
        current = self.worker._client
        self.assertIs(self.worker._runtime_client(), current)
        self.assertEqual(self.factory.call_count, 2)
        current_raw = current._raw
        self.assertTrue(self.worker.stop())
        self.assertEqual(current_raw.closed, 1)

    def test_logout_pauses_without_claiming_and_login_resumes(self):
        old = self.worker._runtime_client()
        raw = old._raw
        self.external_client().clear_tokens()
        for _ in range(3):
            self.assertFalse(self.worker._process_one())
        self.assertEqual(self.row()["status"], "queued")
        self.assertEqual(self.row()["lease_generation"], 0)
        self.assertEqual(raw.closed, 1)
        self.download.assert_not_called()
        self.assert_no_file_failure()
        self.login()
        self.assertTrue(self.worker._process_one())
        self.assertEqual(self.row()["status"], "completed")
        self.assert_no_file_failure()

    def test_rotation_during_remote_prepare_defers_without_consuming_attempt(self):
        old = self.worker._runtime_client()
        old._raw.before_detail = self.external_client().refresh_now
        self.assertFalse(self.worker._process_one())
        self.assertEqual(self.row()["status"], "retry_wait")
        self.assertEqual(self.row()["last_error_type"], "GuangYaCredentialsUnavailable")
        self.assertEqual(self.row()["lease_owner"], "")
        self.assert_no_file_failure()
        self.make_due()
        self.assertTrue(self.worker._process_one())
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assertEqual(self.download.call_count, 1)

    def test_logout_during_prepare_defers_then_waits_for_login(self):
        old = self.worker._runtime_client()
        old._raw.before_detail = self.external_client().clear_tokens
        self.assertFalse(self.worker._process_one())
        lease = self.row()["lease_generation"]
        self.make_due()
        self.assertFalse(self.worker._process_one())
        self.assertEqual(self.row()["lease_generation"], lease)
        self.assert_no_file_failure()
        self.login()
        self.assertTrue(self.worker._process_one())
        self.assertEqual(self.row()["status"], "completed")

    def test_rotation_after_successful_download_does_not_discard_valid_data(self):
        original = metadata.prepare_strm_metadata_job
        refresher = self.external_client()
        def prepare(*args, **kwargs):
            result = original(*args, **kwargs)
            refresher.refresh_now()
            return result
        with patch.object(metadata, "prepare_strm_metadata_job", side_effect=prepare):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assertEqual(self.download.call_count, 1)
        self.assert_no_file_failure()

    def test_ack_failure_after_commit_and_rotation_still_uses_handoff_only(self):
        original = metadata.commit_strm_metadata_job
        refresher = self.external_client()
        def commit(*args, **kwargs):
            result = original(*args, **kwargs)
            refresher.refresh_now()
            return result
        with (
            patch.object(metadata, "commit_strm_metadata_job", side_effect=commit),
            patch.object(db, "settle_strm_metadata_job", side_effect=OSError("fixture ack failure")),
        ):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.row()["status"], "retry_wait")
        self.assertEqual(self.row()["last_error_type"], "OSError")
        self.assert_no_file_failure()
        self.make_due()
        self.assertTrue(self.worker._process_one())
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.download.call_count, 1)

    def test_true_file_failure_still_consumes_retry_budget(self):
        with patch.object(metadata, "prepare_strm_metadata_job", side_effect=ValueError("invalid body")):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.row()["status"], "failed")
        self.assertEqual(self.row()["attempts"], 1)
        self.assertEqual(self.worker._failed_session, 1)
        self.assertEqual(len(db.list_strm_failures(status="all")), 1)

    def test_failed_old_connection_close_retains_client_and_leaves_job_unclaimed(self):
        old = self.worker._runtime_client()
        self.external_client().refresh_now()
        with (
            patch.object(old, "close", return_value=False),
            self.assertRaisesRegex(RuntimeError, "旧连接尚未释放"),
        ):
            self.worker._process_one()
        self.assertIs(self.worker._client, old)
        self.assertEqual(self.row()["status"], "queued")
        self.assertEqual(self.row()["lease_generation"], 0)
        self.assertTrue(self.worker._process_one())
        self.assertEqual(self.row()["status"], "completed")


    def test_credential_file_read_error_during_prepare_releases_lease(self):
        client = self.worker._runtime_client()
        checks = self.enterContext(patch.object(client, "_credentials_current", return_value=True))
        def fail_prepare(*args, **kwargs):
            checks.side_effect = RuntimeError("无法读取光鸭凭据文件")
            raise OSError("read interrupted")
        with patch.object(metadata, "prepare_strm_metadata_job", side_effect=fail_prepare):
            self.assertFalse(self.worker._process_one())
        self.assertEqual(self.row()["status"], "retry_wait")
        self.assertEqual(self.row()["lease_owner"], "")
        self.assert_no_file_failure()
        checks.side_effect = None
        self.make_due()
        self.assertTrue(self.worker._process_one())
        self.assertEqual(self.row()["status"], "completed")
