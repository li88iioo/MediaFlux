"""光鸭磁力重试复用已绑定 qB 元数据，不重投成功目标或猜测文件索引。"""
from __future__ import annotations

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.clients.qbittorrent import QBTorrentExportError
from app.modules import download_dispatcher as dispatcher
from app.modules import offline
from tests.support import IsolatedDatabaseTestCase
from tests.test_guangya_offline_selection import (
    RESOLVE_SUBFILES_FIXTURE,
    FakeSelectionClient,
)
from tests.test_torrent_manifest import _torrent

TORRENT = _torrent({
    b"length": 4096, b"name": b"Fixture.mkv", b"piece length": 16384,
    b"pieces": b"x" * 20,
})
V2_TORRENT = _torrent({
    b"file tree": {b"Fixture.mkv": {b"": {b"length": 4096}}},
    b"meta version": 2, b"name": b"Fixture", b"piece length": 16384,
})


class GuangYaMagnetRecoveryTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM download_log")
            conn.execute("DELETE FROM download_requests")
        self.rules = offline.OfflineRules(
            magnet_enabled=True, ed2k_enabled=True, http_enabled=False,
            target_dir_id="fixture-parent", target_dir_name="测试目录",
            secondary_enabled=False, secondary_dir_id="0", secondary_dir_name="",
            secondary_keywords=(), exclude_keywords=("sample",), min_file_mb=0,
            allowed_exts=("mkv", "mp4"),
        )
        self.gy = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        self.gy.create_dir = Mock(return_value="fixture-staging")
        setattr(self.gy, "close", Mock())
        self.qb = Mock()
        self.qb.export_torrent.return_value = TORRENT
        self.qb_factory = Mock(return_value=self.qb)
        self.qb_submit = Mock(side_effect=AssertionError("must not resubmit qB"))
        self.network = Mock(side_effect=AssertionError("external network forbidden"))
        values = {"QB_URL": "http://qb.invalid", "QB_API_KEY": "fixture-api-key"}
        for target in (
            patch.object(dispatcher, "get", side_effect=lambda key, default="": values.get(key, default)),
            patch.object(offline.OfflineRules, "from_config", return_value=self.rules),
            patch.object(offline, "GuangYaClient", wraps=offline.GuangYaClient),
            patch.object(dispatcher, "QBittorrentClient", self.qb_factory),
            patch.object(dispatcher, "_submit_qb", self.qb_submit),
            patch.object(offline.time, "sleep"),
            patch("socket.socket.connect", self.network),
            patch("socket.getaddrinfo", self.network),
        ):
            active = target.start()
            self.addCleanup(target.stop)
            if getattr(target, "attribute", "") == "GuangYaClient":
                active.return_value = self.gy

    def tearDown(self):
        self.qb_submit.assert_not_called()
        self.network.assert_not_called()

    def request(self, *, payload=TORRENT, source_value="", qb_status="downloading",
                gy_status="failed", status="submitted", bound_hash=None):
        item = dispatcher.torrent_download_input("fixture.torrent", payload)
        source = source_value or item.source_value
        request = dispatcher.DownloadInput(kind="magnet", title="测试资源", source_value=source)
        created = dispatcher.create_request(request, "fixture-chat", "fixture-message")
        self.assertTrue(created["created"])
        request_id = created["id"]
        task_id = dispatcher.parse_torrent_metadata(payload)[1] if bound_hash is None else bound_hash
        download_requests_repo.update_download_request(
            request_id, targets="both", status=status, qb_status=qb_status,
            gy_status=gy_status, qb_task_id=task_id,
            qb_content_path="/fixture/content", local_import_status="pending",
        )
        return request_id

    def assert_recovered(self, request_id, payload=TORRENT):
        self.qb.export_torrent.assert_called_once_with(dispatcher.parse_torrent_metadata(payload)[1])
        self.qb.close.assert_called_once()
        self.assertEqual(self.gy.torrent_resolve_calls, [payload])
        self.assertEqual(self.gy.resolve_calls, [])
        self.assertEqual(len(self.gy.selection_calls), 1)
        self.assertEqual(self.gy.selection_calls[0]["file_indexes"], [0])
        self.assertEqual(self.gy.selection_calls[0]["target_dir_id"], "fixture-staging")
        self.assertEqual(self.gy.legacy_calls, [])
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual(row["gy_status"], "submitted")
        self.assertEqual(row["gy_expected_file_count"], 1)

    def test_partial_failure_retry_uses_same_request_and_never_resubmits_qb(self):
        request_id = self.request()
        get_download_request_result_6 = download_requests_repo.get_download_request(request_id)
        assert get_download_request_result_6 is not None
        before = dict(get_download_request_result_6)
        result = dispatcher.resubmit_download_request(request_id, "guangya")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["request_id"], request_id)
        self.assertFalse(result["created"])
        self.assertFalse(result["source_attention_preserved"])
        self.assert_recovered(request_id)
        after = download_requests_repo.get_download_request(request_id)
        for key in ("qb_status", "qb_task_id", "qb_content_path", "local_import_status", "request_key"):
            assert after is not None
            self.assertEqual(after[key], before[key], key)
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM download_requests").fetchone()[0], 1)
        repeated = dispatcher.resubmit_download_request(request_id, "guangya")
        self.assertFalse(repeated["ok"])
        self.assertEqual(len(self.gy.selection_calls), 1)
        self.qb.export_torrent.assert_called_once()

    def test_missing_target_dispatch_uses_the_same_verified_recovery(self):
        request_id = self.request()
        result = dispatcher.dispatch_missing_targets(request_id, "guangya")
        self.assertTrue(result["ok"], result)
        self.assert_recovered(request_id)

    def test_settled_partial_failure_creates_torrent_successor_not_a_second_qb_task(self):
        request_id = self.request(qb_status="completed", status="completed")
        result = dispatcher.resubmit_download_request(request_id, "guangya")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["created"])
        successor = download_requests_repo.get_download_request(result["request_id"])
        assert successor is not None
        self.assertEqual(successor["kind"], "torrent")
        self.assertEqual(successor["torrent_data"], TORRENT)
        self.assertEqual(successor["qb_status"], "")
        get_download_request_result = download_requests_repo.get_download_request(request_id)
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result["qb_status"], "completed")
        self.assert_recovered(result["request_id"])

    def test_base32_magnet_uses_canonical_btih_without_tracker_dependency(self):
        task_id = dispatcher.parse_torrent_metadata(TORRENT)[1]
        base32 = base64.b32encode(bytes.fromhex(task_id)).decode("ascii")
        request_id = self.request(source_value=f"magnet:?xt=urn:btih:{base32}&tr=udp%3A%2F%2Ftracker.invalid%2Fannoun")
        result = dispatcher.dispatch_missing_targets(request_id, "guangya")
        self.assertTrue(result["ok"], result)
        self.assert_recovered(request_id)

    def test_v2_requires_full_btmh_identity(self):
        self.qb.export_torrent.return_value = V2_TORRENT
        request_id = self.request(payload=V2_TORRENT)
        result = dispatcher.dispatch_missing_targets(request_id, "guangya")
        self.assertTrue(result["ok"], result)
        self.assert_recovered(request_id, V2_TORRENT)

    def test_same_qb_prefix_with_different_full_v2_identity_is_not_uploaded(self):
        source = dispatcher.torrent_download_input("v2.torrent", V2_TORRENT).source_value
        # qB 的截短 40 位 TorrentID 仍相同；BTMH 的完整 64 位不能被截断验证。
        before, suffix = source.split("&", 1)
        forged = before[:-1] + ("0" if before[-1] != "0" else "1") + "&" + suffix
        self.qb.export_torrent.return_value = V2_TORRENT
        request_id = self.request(payload=V2_TORRENT, source_value=forged)
        result = dispatcher.dispatch_missing_targets(request_id, "guangya")
        self.assertTrue(result["ok"], result)  # 安全退回由光鸭独立解析原磁力。
        self.qb.export_torrent.assert_called_once()
        self.assertEqual(self.gy.torrent_resolve_calls, [])
        self.assertEqual(len(self.gy.resolve_calls), 1)

    def test_different_or_malformed_export_falls_back_without_upload(self):
        other = _torrent({b"length": 1, b"name": b"Other.mkv", b"pieces": b"y" * 20})
        for payload in (other, b"not-a-torrent"):
            with self.subTest(payload=payload[:12]):
                self.qb.export_torrent.return_value = payload
                request_id = self.request(status="completed", qb_status="completed")
                result = dispatcher.resubmit_download_request(request_id, "guangya")
                self.assertTrue(result["ok"], result)
                self.assertEqual(self.gy.torrent_resolve_calls, [])
                get_download_request_result_2 = download_requests_repo.get_download_request(result["request_id"])
                assert get_download_request_result_2 is not None
                self.assertEqual(get_download_request_result_2["kind"], "magnet")
                # 清理的是当前隔离测试类的临时数据库，不碰开发/生产记录。
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM download_requests")

    def test_export_unavailable_keeps_bounded_magnet_resolution_and_no_writes_on_empty(self):
        for code in ("unavailable", "not_found", "authentication", "timeout", "invalid_response"):
            with self.subTest(code=code):
                self.qb.export_torrent.side_effect = QBTorrentExportError(code)
                self.gy.resolve_payload = {}
                self.gy.resolve_calls.clear()
                request_id = self.request()
                result = dispatcher.resubmit_download_request(request_id, "guangya")
                self.assertFalse(result["ok"])
                self.assertTrue(result["source_attention_preserved"])
                self.assertEqual(len(self.gy.resolve_calls), 4)
                self.assertEqual(self.gy.torrent_resolve_calls, [])
                self.assertEqual(self.gy.selection_calls, [])
                self.gy.create_dir.assert_not_called()
                get_download_request_result_3 = download_requests_repo.get_download_request(request_id)
                assert get_download_request_result_3 is not None
                self.assertEqual(get_download_request_result_3["qb_status"], "downloading")
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM download_requests")

    def test_unbound_or_mismatched_qb_identity_never_triggers_export(self):
        for task_id in ("", "not-a-hash", "f" * 40):
            with self.subTest(task_id=task_id):
                request_id = self.request(bound_hash=task_id)
                result = dispatcher.dispatch_missing_targets(request_id, "guangya")
                self.assertTrue(result["ok"])
                self.qb_factory.assert_not_called()
                self.assertEqual(self.gy.torrent_resolve_calls, [])
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM download_requests")

    def test_both_or_unknown_cloud_target_cannot_replay_accepted_work(self):
        for target, gy_status in (("both", "failed"), ("guangya", "outcome_unknown")):
            with self.subTest(target=target, gy_status=gy_status):
                request_id = self.request(gy_status=gy_status)
                result = dispatcher.resubmit_download_request(request_id, target)
                self.assertFalse(result["ok"])
                self.qb_factory.assert_not_called()
                self.assertEqual(self.gy.selection_calls, [])
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM download_requests")

    def test_concurrent_retry_is_claimed_once_and_preserves_qb_completion(self):
        request_id = self.request()
        started, release = threading.Event(), threading.Event()

        def export(_torrent_id):
            started.set()
            if not release.wait(5):
                raise AssertionError("test export was not released")
            return TORRENT

        self.qb.export_torrent.side_effect = export
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(dispatcher.resubmit_download_request, request_id, "guangya")
            try:
                self.assertTrue(started.wait(5))
                second = dispatcher.resubmit_download_request(request_id, "guangya")
                self.assertFalse(second["ok"])
                download_requests_repo.update_download_request(request_id, qb_status="completed")
            finally:
                release.set()
            first = future.result(timeout=5)
        self.assertTrue(first["ok"], first)
        self.assert_recovered(request_id)
        get_download_request_result_4 = download_requests_repo.get_download_request(request_id)
        assert get_download_request_result_4 is not None
        self.assertEqual(get_download_request_result_4["qb_status"], "completed")

    def test_cancelled_or_superseded_request_is_not_revived_between_capability_and_claim(self):
        for status in ("cancelled", "resubmitted"):
            with self.subTest(status=status):
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM download_requests")
                request_id = self.request()
                original = dispatcher.dispatch_missing_targets

                def stop_then_dispatch(
                    *args, request_id=request_id, status=status, original=original, **kwargs,
                ):
                    download_requests_repo.update_download_request(request_id, status=status)
                    return original(*args, **kwargs)

                with patch.object(dispatcher, "dispatch_missing_targets", side_effect=stop_then_dispatch):
                    result = dispatcher.resubmit_download_request(request_id, "guangya")
                self.assertFalse(result["ok"])
                get_download_request_result_5 = download_requests_repo.get_download_request(request_id)
                assert get_download_request_result_5 is not None
                self.assertEqual(get_download_request_result_5["status"], status)
                self.qb_factory.assert_not_called()
                self.gy.create_dir.assert_not_called()
                self.assertEqual(self.gy.selection_calls, [])
                self.assertEqual(self.gy.legacy_calls, [])
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM download_requests")

    def test_qb_client_construction_failure_does_not_block_independent_cloud_resolution(self):
        self.qb_factory.side_effect = RuntimeError("fixture-secret-constructor")
        request_id = self.request()
        result = dispatcher.dispatch_missing_targets(request_id, "guangya")
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.gy.resolve_calls), 1)
        self.assertEqual(self.gy.torrent_resolve_calls, [])
        self.assertNotIn("fixture-secret", json.dumps(result))

    def test_initial_both_submission_does_not_assume_qb_metadata_is_ready(self):
        source = dispatcher.torrent_download_input("fixture.torrent", TORRENT).source_value
        item = dispatcher.DownloadInput(kind="magnet", title="测试资源", source_value=source)
        request_id = dispatcher.create_request(item, "fixture-chat", "initial")["id"]
        self.gy.resolve_payload = {}
        with patch.object(dispatcher, "_submit_qb", return_value={
            "ok": True, "task_id": dispatcher.parse_torrent_metadata(TORRENT)[1],
        }) as accepted_qb:
            result = dispatcher.dispatch_request(request_id, "both")
        self.assertTrue(result["ok"])
        self.assertEqual(result["succeeded"], ["qb"])
        self.assertEqual(result["failed"], ["guangya"])
        accepted_qb.assert_called_once()
        self.qb_factory.assert_not_called()
        self.assertEqual(self.gy.torrent_resolve_calls, [])
        self.assertEqual(len(self.gy.resolve_calls), 4)
        self.gy.create_dir.assert_not_called()
        self.assertEqual(self.gy.selection_calls, [])
        altered = dispatcher.DownloadInput(kind="magnet", title="改名", source_value=source + "&tr=udp%3A%2F%2Ffixture.invalid%2Fannoun")
        duplicate = dispatcher.create_request(altered, "fixture-chat", "again")
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["id"], request_id)

    def test_original_torrent_input_does_not_depend_on_qb_export(self):
        item = dispatcher.torrent_download_input("fixture.torrent", TORRENT)
        request_id = dispatcher.create_request(item, "fixture-chat", "original-torrent")["id"]
        result = dispatcher.dispatch_request(request_id, "guangya")
        self.assertTrue(result["ok"], result)
        self.qb_factory.assert_not_called()
        self.assertEqual(self.gy.torrent_resolve_calls, [TORRENT])
        self.assertEqual(self.gy.resolve_calls, [])
        self.assertEqual(self.gy.selection_calls[0]["file_indexes"], [0])

    def test_recovered_torrent_still_requires_valid_cloud_file_indexes(self):
        for invalid in (False, -1, 0.25):
            with self.subTest(invalid=invalid):
                request_id = self.request()
                self.gy.resolve_payload = {"data": {"btResInfo": {
                    "infoHash": "synthetic-only", "subfilesNum": 2,
                    "subfiles": [
                        {"fileIndex": invalid, "name": "First.mkv", "size": 4096},
                        {"fileIndex": 1, "name": "Second.mkv", "size": 4096},
                    ],
                }}}
                result = dispatcher.resubmit_download_request(request_id, "guangya")
                self.assertFalse(result["ok"])
                self.assertIn("光鸭资源解析响应无效", result["error"])
                self.assertEqual(self.gy.resolve_calls, [])
                self.assertEqual(self.gy.selection_calls, [])
                self.assertEqual(self.gy.legacy_calls, [])
                self.gy.create_dir.assert_not_called()
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM download_requests")

    def test_agent_confirmation_does_not_promise_a_successor_for_partial_retry(self):
        request_id = self.request()
        with patch.object(dispatcher, "guangya_download_credential_generation", return_value=1):
            result = dispatcher.prepare_download_retry(request_id, "guangya")
        effects = "".join(result["effects"])
        self.assertIn("重试所选目标", effects)
        self.assertIn("保留未重试目标的任务", effects)
        self.assertIn("补投原请求或创建新的提交记录", effects)

    def test_magnet_public_failure_explains_no_task_and_failed_target_retry_safely(self):
        summary = dispatcher.public_dispatch_summary({
            "succeeded": ["qb"], "failed": ["guangya"],
            "error": "磁力资源连续 4 次未解析到可验证文件列表 token=fixture-secret",
        })
        self.assertEqual(summary["status"], "partial")
        self.assertIn("未创建下载任务", summary["error"])
        self.assertIn("仅重试光鸭", summary["error"])
        self.assertIn("原始种子", summary["error"])
        self.assertNotIn("fixture-secret", json.dumps(summary))
