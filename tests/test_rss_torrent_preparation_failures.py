"""RSS 种子准备失败专项回归：真实 prepare/repo/RSS，仅替换网络与下载提交边界。"""
from __future__ import annotations

from tests.support import assert_present

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import database as db
from app.indexers import downloads
from app.modules import download_dispatcher as dispatcher
from app.modules import offline, rss
from app.repositories import rss as rss_repo
from app.routes import rss_api
from tests.support import IsolatedDatabaseTestCase, isolated_test_database

_INFO = b"d6:lengthi10485760e4:name9:Movie.mkve"
_TORRENT = b"d4:info" + _INFO + b"e"
_INFOHASH = hashlib.sha1(_INFO).hexdigest()
_TOKEN = "rss-preparation-test-token"
_URL = f"https://feed.invalid/download/{_INFOHASH}.torrent?passkey={_TOKEN}"
_HTTP_CASES = (
    (408, True), (425, True), (429, True),
    (500, True), (502, True), (503, True), (504, True), (599, True),
    (400, False), (401, False), (403, False), (404, False), (410, False),
)


def _http_error(status):
    request = httpx.Request("GET", _URL)
    return httpx.HTTPStatusError(
        f"upstream token={_TOKEN}", request=request,
        response=httpx.Response(status, request=request),
    )


class RSSTorrentPreparationFailureTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        # 每个用例都使用独立临时库，表驱动子例使用不同订阅/条目。
        self.enterContext(isolated_test_database())
        self.enterContext(patch(
            "socket.socket.connect", side_effect=AssertionError("real network forbidden"),
        ))
        self.enterContext(patch(
            "socket.getaddrinfo", side_effect=AssertionError("real DNS forbidden"),
        ))
        rules = offline.OfflineRules(
            True, True, False, "target", "测试目录", False, "0", "", (), (), 0, ("mkv",),
        )
        self.enterContext(patch.object(offline.OfflineRules, "from_config", return_value=rules))
        self.fetch = self.enterContext(patch.object(
            rss, "_fetch_rss_payload",
            return_value=(_TORRENT, {"content-type": "application/x-bittorrent"}),
        ))
        self.guangya = self.enterContext(patch.object(
            dispatcher, "_submit_guangya",
            return_value={"ok": True, "task_id": "gy-task", "task_ids": ["gy-task"]},
        ))
        self.qb = self.enterContext(patch.object(
            dispatcher, "_submit_qb", return_value={"ok": True, "task_id": _INFOHASH},
        ))
        self.engine = rss.RSSEngine()
        self.addCleanup(self.engine.close)
        self.sequence = 0

    def entry(self, url=_URL, method="guangya"):
        self.sequence += 1
        sid = db.add_rss_subscription(
            f"准备失败 {self.sequence}", f"https://feed.invalid/{self.sequence}.xml",
            download_method=method, gy_target_dir="target", gy_target_dir_name="测试目录",
        )
        eid = db.add_rss_entry_with_media(
            sid, "Movie", f"preparation-{self.sequence}",
            payload=json.dumps({"torrent_url": url, "content_type": "application/x-bittorrent"}),
        )["id"]
        assert (eid) is not None
        return int(eid)

    def logs(self, eid):
        with db.get_conn() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM download_log WHERE rss_item_id=? ORDER BY id",
                (assert_present(db.get_rss_entry(eid))["rss_item_id"],),
            )]

    def assert_no_requests(self):
        self.guangya.assert_not_called()
        self.qb.assert_not_called()
        with db.get_conn() as conn:
            for table in ("download_requests", "download_request_keys"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def failure(self, eid, code, retryable=False, *, review=False, public_fields=True):
        with self.assertLogs("app", level="WARNING") as captured:
            result = self.engine.download(eid)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed")
        if public_fields:
            self.assertEqual(result["failure_code"], code)
            self.assertEqual(result["retryable"], retryable)
        self.assertEqual(bool(result.get("review_required")), review)
        row = db.get_rss_entry(eid)
        assert row is not None
        self.assertEqual((row["status"], row["processed"]), ("failed", 0))
        self.assertEqual((row["failure_code"], row["failure_retryable"]), (code, int(retryable)))
        self.assertTrue(row["failed_at"])
        logs = self.logs(eid)
        self.assertTrue(logs)
        self.assertTrue(all(not log["backend_task_id"] for log in logs))
        self.assertNotIn(_TOKEN, json.dumps([result, logs, captured.output], default=str))
        return result, captured.output

    def age_failure(self, eid, seconds=61):
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET failed_at=datetime('now','localtime',?) WHERE id=?",
                (f"-{seconds} seconds", eid),
            )

    def retryable_ids(self):
        return {int(row["id"]) for row in db.get_retryable_failed_rss_snapshot("guangya")}

    def test_preparation_error_public_constructor_and_defaults(self):
        error = offline.TorrentPreparationError(
            "安全错误", failure_code="torrent_invalid", stage="validate",
        )
        assert isinstance(error, ValueError)
        self.assertEqual(str(error), "安全错误")
        self.assertEqual((error.failure_code, error.stage, error.retryable, error.http_status),
                         ("torrent_invalid", "validate", False, None))
        error = offline.TorrentPreparationError(
            "HTTP 503", failure_code="torrent_http_error", stage="fetch",
            retryable=True, http_status=503,
        )
        self.assertEqual((error.failure_code, error.stage, error.retryable, error.http_status),
                         ("torrent_http_error", "fetch", True, 503))

    def test_real_fetch_classification_survives_prepare_download_input(self):
        cases = [
            (httpx.ReadTimeout(_URL), "torrent_fetch_timeout", True, None),
            (httpx.ConnectTimeout(_URL), "torrent_fetch_timeout", True, None),
            (TimeoutError(_URL), "torrent_fetch_timeout", True, None),
            (httpx.ConnectError(_URL), "torrent_fetch_failed", True, None),
            (httpx.ReadError(_URL), "torrent_fetch_failed", True, None),
            (httpx.RemoteProtocolError(_URL), "torrent_fetch_failed", True, None),
            (OSError(_URL), "torrent_fetch_failed", True, None),
            *[(_http_error(status), "torrent_http_error", retryable, status)
              for status, retryable in _HTTP_CASES],
        ]
        for source_error, code, retryable, status in cases:
            for target in ("offline", "guangya", "both"):
                with self.subTest(error=type(source_error).__name__, status=status, target=target):
                    self.fetch.side_effect = source_error
                    with self.assertRaises(offline.TorrentPreparationError) as caught:
                        if target == "offline":
                            offline._prepare_offline_resource(_URL)
                        else:
                            dispatcher.prepare_download_input(
                                dispatcher.normalize_download_url(_URL), target,
                            )
                    error = caught.exception
                    self.assertEqual((error.failure_code, error.stage, error.retryable, error.http_status),
                                     (code, "fetch", retryable, status))
                    self.assertIs(error.__cause__, source_error)
                    self.assertNotIn(_TOKEN, str(error))
        self.assert_no_requests()

    def test_validation_classifies_bad_bencode_and_mismatched_hash(self):
        cases = (
            (b"", _URL, "torrent_invalid"),
            (b"de", _URL, "torrent_invalid"),
            (f"<html>token={_TOKEN}</html>".encode(), _URL, "torrent_invalid"),
            (_TORRENT, _URL.replace(_INFOHASH, "f" * 40), "torrent_identity_mismatch"),
        )
        for payload, url, code in cases:
            for supplied in (False, True):
                with self.subTest(code=code, payload=payload[:12], supplied=supplied):
                    self.fetch.return_value = (payload, {})
                    self.fetch.reset_mock()
                    with self.assertRaises(offline.TorrentPreparationError) as caught:
                        offline._prepare_offline_resource(url, payload if supplied else None)
                    error = caught.exception
                    self.assertEqual((error.failure_code, error.stage, error.retryable, error.http_status),
                                     (code, "validate", False, None))
                    self.assertNotIn(_TOKEN, str(error))
                    self.assertEqual(self.fetch.call_count, int(not supplied))
        self.assert_no_requests()

    def test_rss_initial_timeout_is_unbound_without_backend_or_request(self):
        eid = self.entry()
        self.fetch.side_effect = httpx.ReadTimeout(_URL)
        result, output = self.failure(eid, "torrent_fetch_timeout", True)
        self.assertEqual(result["infohash"], _INFOHASH)
        self.assertEqual(result["request_id"], 0)
        row = db.get_rss_entry(eid)
        assert row is not None
        self.assertEqual((row["download_request_key"], row["download_backend"], row["retry_count"]),
                         ("", "", 0))
        self.assertIn("stage=fetch", "\n".join(output))
        self.fetch.assert_called_once_with(
            _URL, user_agent="MediaFlux/1.0", timeout_seconds=20, allow_http=True,
        )
        self.assert_no_requests()

    def test_cooldown_rejects_retry_then_expired_timeout_submits_once(self):
        eid = self.entry()
        self.fetch.side_effect = httpx.ReadTimeout(_URL)
        self.failure(eid, "torrent_fetch_timeout", True)
        get_rss_entry_result_8 = db.get_rss_entry(eid)
        assert get_rss_entry_result_8 is not None
        before = dict(get_rss_entry_result_8)
        self.assertNotIn(eid, self.retryable_ids())
        self.assertFalse(self.engine.download(eid)["ok"])
        get_rss_entry_result_9 = db.get_rss_entry(eid)
        assert get_rss_entry_result_9 is not None
        self.assertEqual(dict(get_rss_entry_result_9), before)
        self.assertEqual(self.fetch.call_count, 1)
        self.assert_no_requests()
        self.age_failure(eid)
        self.assertIn(eid, self.retryable_ids())
        self.fetch.side_effect = None
        result = self.engine.download(eid)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["existing"])
        self.assertEqual(self.fetch.call_count, 2)
        self.guangya.assert_called_once()
        row = db.get_rss_entry(eid)
        assert row is not None
        self.assertEqual((row["status"], row["processed"], row["retry_count"]), ("downloaded", 1, 1))
        self.assertEqual((row["failure_code"], row["failure_retryable"]), ("", 0))
        request = db.get_download_request(result["request_id"])
        assert request is not None
        self.assertEqual(request["torrent_data"], _TORRENT)
        self.assertEqual(request["gy_task_id"], "gy-task")
        self.assertEqual(row["download_request_key"], request["request_key"])
        repeated = self.engine.download(eid)
        self.assertTrue(repeated["ok"])
        self.assertTrue(repeated["existing"])
        self.assertEqual(self.fetch.call_count, 2)
        self.guangya.assert_called_once()
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0], 1)

    def test_all_read_codes_share_cooldown_and_five_retry_limit(self):
        for error, code in (
            (httpx.ReadTimeout(_URL), "torrent_fetch_timeout"),
            (httpx.ConnectError(_URL), "torrent_fetch_failed"),
            (_http_error(503), "torrent_http_error"),
        ):
            with self.subTest(code=code):
                eid = self.entry()
                self.fetch.reset_mock()
                self.fetch.side_effect = error
                self.failure(eid, code, True)
                self.age_failure(eid, 30)
                get_rss_entry_result_10 = db.get_rss_entry(eid)
                assert get_rss_entry_result_10 is not None
                before = dict(get_rss_entry_result_10)
                self.assertNotIn(eid, self.retryable_ids())
                self.assertFalse(self.engine.download(eid)["ok"])
                get_rss_entry_result_11 = db.get_rss_entry(eid)
                assert get_rss_entry_result_11 is not None
                self.assertEqual(dict(get_rss_entry_result_11), before)
                self.assertEqual(self.fetch.call_count, 1)
                for retry in range(1, 6):
                    self.age_failure(eid)
                    self.assertIn(eid, self.retryable_ids())
                    self.failure(eid, code, True)
                    get_rss_entry_result = db.get_rss_entry(eid)
                    assert get_rss_entry_result is not None
                    self.assertEqual(get_rss_entry_result["retry_count"], retry)
                self.age_failure(eid)
                get_rss_entry_result_12 = db.get_rss_entry(eid)
                assert get_rss_entry_result_12 is not None
                before = dict(get_rss_entry_result_12)
                self.assertNotIn(eid, self.retryable_ids())
                self.assertFalse(self.engine.download(eid)["ok"])
                self.assertFalse(db.claim_rss_entry(eid))
                get_rss_entry_result_13 = db.get_rss_entry(eid)
                assert get_rss_entry_result_13 is not None
                self.assertEqual(dict(get_rss_entry_result_13), before)
                self.assertEqual(self.fetch.call_count, 6)
                self.assert_no_requests()

    def test_connect_error_retry_api_reports_remaining_cooldown_without_new_attempt(self):
        eid = self.entry()
        self.fetch.side_effect = httpx.ConnectError(_URL)
        with patch.object(db, "now", return_value="2026-11-09 00:12:35"):
            self.failure(eid, "torrent_fetch_failed", True)
        before = dict(assert_present(db.get_rss_entry(eid)))
        app = FastAPI()
        app.include_router(rss_api.router)
        with patch.object(rss_api, "require_api_login"), TestClient(app) as client:
            for timestamp, remaining in (("2026-11-09 00:13:27", 8), ("2026-11-09 00:13:34", 1)):
                with self.subTest(remaining=remaining), patch.object(db, "now", return_value=timestamp):
                    self.assertNotIn(eid, self.retryable_ids())
                    response = client.post(f"/api/rss/entries/{eid}/download")
                    self.assertEqual(response.status_code, 400)
                    result = response.json()
                    self.assertEqual(result["retry_reason"], "cooldown")
                    self.assertEqual(result["failure_code"], "torrent_fetch_failed")
                    self.assertEqual(result["retry_after"], remaining)
                    self.assertEqual(result["retry_at"], "2026-11-09 00:13:35")
                    self.assertIn("网络或传输错误", result["error"])
                    self.assertIn(f"{remaining} 秒", result["error"])
                    self.assertIn(result["retry_at"], result["error"])
                    self.assertNotIn(_TOKEN, response.text)
                    self.assertEqual(dict(assert_present(db.get_rss_entry(eid))), before)
            self.assertEqual(self.fetch.call_count, 1)
            self.assert_no_requests()
            self.fetch.side_effect = None
            with patch.object(db, "now", return_value="2026-11-09 00:13:35"):
                self.assertIn(eid, self.retryable_ids())
                result = client.post(f"/api/rss/entries/{eid}/download").json()["result"]
                self.assertTrue(result["ok"], result)
                self.assertTrue(client.post(f"/api/rss/entries/{eid}/download").json()["result"]["existing"])
        self.assertEqual(self.fetch.call_count, 2)
        self.guangya.assert_called_once()
        self.assertEqual(assert_present(db.get_rss_entry(eid))["retry_count"], 1)

    def test_retry_rejections_distinguish_limit_backend_unknown_and_invalid_failure_time(self):
        cases = (
            ("torrent_fetch_failed", True, 5, "2026-11-09 00:12:35", "retry_limit", "5 次"),
            ("qb_unavailable", True, 0, "2026-11-09 00:12:35", "backend_mismatch", "下载后端不匹配"),
            ("torrent_invalid", False, 0, "2026-11-09 00:12:35", "not_retryable", "损坏"),
            ("torrent_identity_mismatch", False, 0, "2026-11-09 00:12:35", "not_retryable", "身份不一致"),
            ("qb_auth_failed", False, 0, "2026-11-09 00:12:35", "not_retryable", "qBittorrent 鉴权失败"),
            ("guangya_auth_failed", False, 0, "2026-11-09 00:12:35", "not_retryable", "光鸭鉴权失败"),
            ("missing_torrent_url", False, 0, "2026-11-09 00:12:35", "not_retryable", "缺少种子下载链接"),
            ("invalid_payload", False, 0, "2026-11-09 00:12:35", "not_retryable", "条目数据"),
            ("unknown_failure", False, 0, "2026-11-09 00:12:35", "not_retryable", "失败原因未记录"),
            ("guangya_outcome_unknown", False, 0, "2026-11-09 00:12:35", "review_required", "核验"),
            *[("torrent_fetch_failed", True, 0, timestamp, "invalid_failed_at", "失败时间")
              for timestamp in (None, "", "invalid")],
        )
        for code, retryable, count, failed_at, reason, message in cases:
            with self.subTest(code=code, reason=reason, failed_at=failed_at):
                eid = self.entry()
                db.record_rss_entry_failure(eid, code, retryable)
                with db.get_conn() as conn:
                    conn.execute("UPDATE rss_entries SET retry_count=?,failed_at=? WHERE id=?", (count, failed_at, eid))
                before = dict(assert_present(db.get_rss_entry(eid)))
                with patch.object(db, "now", return_value="2026-11-09 00:13:35"):
                    self.assertNotIn(eid, self.retryable_ids())
                    result = self.engine.download(eid)
                self.assertFalse(result["ok"])
                self.assertEqual(result["retry_reason"], reason)
                self.assertIn(message, result["error"])
                self.assertIsNone(result["retry_after"])
                self.assertIsNone(result["retry_at"])
                self.assertEqual(dict(assert_present(db.get_rss_entry(eid))), before)
        self.fetch.assert_not_called()
        self.assert_no_requests()

    def test_concurrent_expired_retry_claims_once_and_reports_pending(self):
        eid = self.entry()
        self.fetch.side_effect = httpx.ConnectError(_URL)
        with patch.object(db, "now", return_value="2026-11-09 00:12:35"):
            self.failure(eid, "torrent_fetch_failed", True)
        self.fetch.side_effect = None
        entered, release = threading.Event(), threading.Event()

        def submit(*_args, **_kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("retry fixture timeout")
            return {"ok": True, "task_id": "gy-task", "task_ids": ["gy-task"]}

        self.guangya.side_effect = submit
        with patch.object(db, "now", return_value="2026-11-09 00:13:35"), ThreadPoolExecutor(max_workers=1) as pool:
            winner = pool.submit(self.engine.download, eid)
            try:
                self.assertTrue(entered.wait(5))
                loser = self.engine.download(eid)
                self.assertTrue(loser["pending"], loser)
                self.assertEqual(loser["status"], "submitting")
                self.assertEqual(assert_present(db.get_rss_entry(eid))["retry_count"], 1)
            finally:
                release.set()
            self.assertTrue(winner.result()["ok"])
        self.guangya.assert_called_once()
        self.assertEqual(self.fetch.call_count, 2)

    def test_orphan_failed_entry_cannot_retry_but_orphan_pending_can_claim(self):
        failed, pending = self.entry(method="qb"), self.entry(method="qb")
        db.record_rss_entry_failure(failed, "qb_unavailable", True)
        with db.get_conn() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DELETE FROM rss_items WHERE id IN (SELECT rss_item_id FROM rss_entries WHERE id IN (?,?))", (failed, pending))
        before = dict(assert_present(db.get_rss_entry(failed)))
        admission = {}
        self.assertFalse(db.claim_rss_entry(failed, admission=admission))
        self.assertEqual(admission["retry_reason"], "subscription_missing")
        self.assertEqual(dict(assert_present(db.get_rss_entry(failed))), before)
        self.assertTrue(db.claim_rss_entry(pending))
        self.assertEqual(assert_present(db.get_rss_entry(pending))["status"], "submitting")
        self.assert_no_requests()

    def test_rss_http_statuses_preserve_source_classification_and_safe_logs(self):
        for status, retryable in _HTTP_CASES:
            with self.subTest(status=status):
                eid = self.entry()
                self.fetch.side_effect = _http_error(status)
                result, output = self.failure(eid, "torrent_http_error", retryable)
                self.assertIn(str(status), result["error"])
                self.assertIn(f"http_status={status}", "\n".join(output))
                get_rss_entry_result_2 = db.get_rss_entry(eid)
                assert get_rss_entry_result_2 is not None
                self.assertEqual(get_rss_entry_result_2["download_request_key"], "")
                self.age_failure(eid)
                self.assertEqual(eid in self.retryable_ids(), retryable)
                self.assert_no_requests()

    def test_rss_network_errors_are_fetch_failures_not_invalid_urls(self):
        for error in (httpx.ConnectError(_URL), httpx.ReadError(_URL), httpx.RemoteProtocolError(_URL)):
            with self.subTest(error=type(error).__name__):
                eid = self.entry()
                self.fetch.side_effect = error
                self.failure(eid, "torrent_fetch_failed", True)
                get_rss_entry_result_3 = db.get_rss_entry(eid)
                assert get_rss_entry_result_3 is not None
                self.assertEqual(get_rss_entry_result_3["download_request_key"], "")
                self.assert_no_requests()

    def test_rss_bad_content_and_hash_mismatch_never_become_retryable(self):
        for payload, url, code in (
            (f"token={_TOKEN}".encode(), _URL, "torrent_invalid"),
            (b"de", _URL, "torrent_invalid"),
            (_TORRENT, _URL.replace(_INFOHASH, "f" * 40), "torrent_identity_mismatch"),
        ):
            with self.subTest(code=code, payload=payload[:12]):
                eid = self.entry(url)
                self.fetch.return_value = (payload, {})
                _, output = self.failure(eid, code)
                self.assertIn("stage=validate", "\n".join(output))
                self.age_failure(eid)
                get_rss_entry_result_14 = db.get_rss_entry(eid)
                assert get_rss_entry_result_14 is not None
                before = dict(get_rss_entry_result_14)
                calls = self.fetch.call_count
                self.assertNotIn(eid, self.retryable_ids())
                self.assertFalse(self.engine.download(eid)["ok"])
                get_rss_entry_result_15 = db.get_rss_entry(eid)
                assert get_rss_entry_result_15 is not None
                self.assertEqual(dict(get_rss_entry_result_15), before)
                self.assertEqual(self.fetch.call_count, calls)
                self.assertEqual(before["download_request_key"], "")
                self.assert_no_requests()

    def test_truly_invalid_url_is_normalize_failure_without_fetch(self):
        for url in (
            f"https:///movie.torrent?passkey={_TOKEN}",
            f"magnet:?dn=Movie&token={_TOKEN}",
            f"ftp://feed.invalid/movie.torrent?token={_TOKEN}",
        ):
            with self.subTest(url=url.split("?")[0]):
                _, output = self.failure(self.entry(url), "invalid_payload")
                self.assertIn("stage=normalize", "\n".join(output))
                self.fetch.assert_not_called()
                self.assert_no_requests()

    def test_real_binding_conflict_does_not_overwrite_winner_or_record_bad_link(self):
        self.assertTrue(issubclass(rss_repo.RSSEntryStateChangedError, ValueError))
        for status, processed in (("skipped", 1), ("failed", 0), ("downloaded", 1)):
            with self.subTest(status=status):
                eid = self.entry()
                winner = {}

                def fetch_and_change_state(
                    *_args, status=status, processed=processed, eid=eid, winner=winner, **_kwargs,
                ):
                    with db.get_conn() as conn:
                        conn.execute(
                            "UPDATE rss_entries SET status=?,processed=?,failure_code='guangya_auth_failed',"
                            "failure_retryable=0,retry_count=3,download_request_key='winner-key',"
                            "download_backend='guangya',failed_at='2026-10-01 00:00:00' WHERE id=?",
                            (status, processed, eid),
                        )
                    get_rss_entry_result_16 = db.get_rss_entry(eid)
                    assert get_rss_entry_result_16 is not None
                    winner.update(dict(get_rss_entry_result_16))
                    return _TORRENT, {}

                self.fetch.side_effect = fetch_and_change_state
                with patch.object(db, "record_rss_entry_failure", wraps=db.record_rss_entry_failure) as record, \
                        patch.object(rss_repo, "bind_rss_entry_download", wraps=rss_repo.bind_rss_entry_download) as bind, \
                        self.assertLogs("app", level="WARNING") as captured:
                    result = self.engine.download(eid)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "conflict")
                self.assertTrue(result["error"])
                self.assertNotEqual(result.get("failure_code"), "invalid_payload")
                bind.assert_called_once()
                record.assert_not_called()
                get_rss_entry_result_17 = db.get_rss_entry(eid)
                assert get_rss_entry_result_17 is not None
                self.assertEqual(dict(get_rss_entry_result_17), winner)
                self.assertEqual(self.logs(eid), [])
                self.assertNotIn(_TOKEN, json.dumps([result, captured.output]))
                self.assert_no_requests()

    def test_backend_valueerror_is_unknown_nonretryable_and_never_a_bad_url(self):
        for method in ("guangya", "qb"):
            # 内容身份相同，必须隔离子例，避免光鸭 unknown 防重挡住 qB 提交。
            with self.subTest(method=method), isolated_test_database():
                eid = self.entry(method=method)
                backend = self.guangya if method == "guangya" else self.qb
                backend.side_effect = ValueError(f"receipt uncertain token={_TOKEN} {_URL}")
                result, _ = self.failure(
                    eid, f"{method}_outcome_unknown", review=True, public_fields=False,
                )
                backend.assert_called_once()
                request = db.get_download_request(result["request_id"])
                backend_status = "gy_status" if method == "guangya" else "qb_status"
                assert request is not None
                self.assertEqual(request[backend_status], "outcome_unknown")
                get_rss_entry_result_4 = db.get_rss_entry(eid)
                assert get_rss_entry_result_4 is not None
                self.assertTrue(get_rss_entry_result_4["download_request_key"])
                before_calls = backend.call_count
                get_rss_entry_result_18 = db.get_rss_entry(eid)
                assert get_rss_entry_result_18 is not None
                before = dict(get_rss_entry_result_18)
                repeated = self.engine.download(eid)
                self.assertFalse(repeated["ok"])
                self.assertTrue(rss.rss_entry_delivery(db.get_rss_entry(eid))["review_required"])
                self.assertEqual(backend.call_count, before_calls)
                get_rss_entry_result_19 = db.get_rss_entry(eid)
                assert get_rss_entry_result_19 is not None
                self.assertEqual(dict(get_rss_entry_result_19), before)
                self.assertEqual(db.update_rss_entries_processed([eid], False), 0)
                self.assertNotIn(_TOKEN, json.dumps(repeated))

    def test_other_submission_valueerror_cannot_masquerade_as_invalid_payload(self):
        for method in ("guangya", "qb"):
            with self.subTest(method=method):
                eid = self.entry(method=method)
                with patch.object(downloads, "create_request", side_effect=ValueError(f"commit token={_TOKEN}")):
                    result, output = self.failure(eid, f"{method}_outcome_unknown", review=True)
                self.assertIn("stage=submit", "\n".join(output))
                self.assertEqual(result["request_id"], 0)
                get_rss_entry_result_5 = db.get_rss_entry(eid)
                assert get_rss_entry_result_5 is not None
                self.assertTrue(get_rss_entry_result_5["download_request_key"])
                self.assert_no_requests()

    def test_qb_http_torrent_is_not_prefetched_and_only_submitted_once(self):
        eid = self.entry(method="qb")
        self.fetch.side_effect = AssertionError("qB HTTP torrent must not be prefetched")
        result = self.engine.download(eid)
        self.assertTrue(result["ok"], result)
        self.qb.assert_called_once()
        self.guangya.assert_not_called()
        self.fetch.assert_not_called()
        request = db.get_download_request(result["request_id"])
        assert request is not None
        self.assertEqual((request["kind"], request["source_value"], request["torrent_data"]),
                         ("http", _URL, None))
        repeated = self.engine.download(eid)
        self.assertTrue(repeated["ok"])
        self.assertTrue(repeated["existing"])
        self.qb.assert_called_once()
        self.fetch.assert_not_called()
        self.assertNotIn(_TOKEN, json.dumps([result, repeated, self.logs(eid)]))

    def test_legacy_invalid_payload_requires_explicit_reset_before_retry(self):
        eid = self.entry()
        self.assertTrue(db.claim_rss_entry(eid))
        db.record_rss_entry_failure(eid, "invalid_payload", False)
        self.age_failure(eid)
        get_rss_entry_result_20 = db.get_rss_entry(eid)
        assert get_rss_entry_result_20 is not None
        before = dict(get_rss_entry_result_20)
        self.assertNotIn(eid, self.retryable_ids())
        self.assertFalse(self.engine.download(eid)["ok"])
        self.assertFalse(db.claim_rss_entry(eid))
        get_rss_entry_result_21 = db.get_rss_entry(eid)
        assert get_rss_entry_result_21 is not None
        self.assertEqual(dict(get_rss_entry_result_21), before)
        self.fetch.assert_not_called()
        self.assert_no_requests()
        self.assertEqual(db.update_rss_entries_processed([eid], False), 1)
        get_rss_entry_result_6 = db.get_rss_entry(eid)
        assert get_rss_entry_result_6 is not None
        self.assertEqual(get_rss_entry_result_6["status"], "pending")
        get_rss_entry_result_7 = db.get_rss_entry(eid)
        assert get_rss_entry_result_7 is not None
        self.assertEqual(get_rss_entry_result_7["failure_code"], "")
        result = self.engine.download(eid)
        self.assertTrue(result["ok"], result)
        self.guangya.assert_called_once()
        self.fetch.assert_called_once()
        self.assertTrue(self.engine.download(eid)["existing"])
        self.guangya.assert_called_once()
