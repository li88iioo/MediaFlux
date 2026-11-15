"""受理与用户移除共用真实 qB writer；回执只收尾自己的目标。"""
from __future__ import annotations

import tests  # noqa: F401 - 应用导入前隔离运行路径与数据库

from concurrent.futures import Future
import asyncio
import copy
import json
import socket
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import rss as rss_repo
from app.clients.qbittorrent import QBittorrentClient, TorrentAddResult, TorrentTask
from app.indexers.downloads import submit_download_input
from app.modules import download_dispatcher as dispatcher
from app.modules import offline
from app.modules.download_tracker import DownloadTracker
from app.modules.process_lock import CrossProcessLock
from app.modules.qb_control import qb_control_write_lease, remove_qb_tasks
from app.modules.rss import RSSEngine
from app.repositories.rss import bind_rss_entry_download
from tests.support import isolated_test_database
from tests import test_guangya_offline_selection as offline_test_fixtures
from tests.test_guangya_offline_selection import (
    FakeResponse, FakeSelectionClient, RESOLVE_SUBFILES_FIXTURE,
)
from tests.test_guangya_single_file_manifest import SINGLE
from tests.test_qbittorrent_523_compat import _Response


class DownloadSubmissionCancellationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        for owner, name in (
            (socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "getaddrinfo"),
        ):
            self.enterContext(patch.object(owner, name, side_effect=AssertionError("禁止外联")))
        self.enterContext(patch.object(
            dispatcher, "get", side_effect=lambda key, default="": "http://qb.invalid" if key == "QB_URL" else default,
        ))
        self.enterContext(patch.object(
            dispatcher, "analyze_offline_url", return_value=SimpleNamespace(allowed=True, reason=""),
        ))
        self.client = Mock()
        self.enterContext(patch.object(dispatcher, "QBittorrentClient", return_value=self.client))
        self.serial = 0

    def prepare(self, path, *, target="qb", kind="magnet"):
        self.serial += 1
        torrent_hash = f"{self.serial:040x}"
        item = dispatcher.DownloadInput(
            kind, "受理取消竞争",
            f"https://download.invalid/{self.serial}.torrent" if kind == "http"
            else f"magnet:?xt=urn:btih:{torrent_hash}",
        )
        request_id = dispatcher.create_request(item, "", "cancel-race")["id"]
        if path in {"supplement", "retry"}:
            download_requests_repo.update_download_request(
                request_id, status="downloading", targets="both",
                qb_status=("submitted" if target == "both" else "failed" if path == "retry" else ""),
                qb_task_id=torrent_hash if target == "both" else "",
                gy_status=("failed" if path == "retry" else "") if target == "both" else "downloading",
                gy_task_id="kept-cloud" if target == "qb" else "",
                gy_task_ids='["kept-cloud"]' if target == "qb" else "[]",
            )
        elif path == "successor":
            download_requests_repo.update_download_request(
                request_id, status="failed", targets=target, qb_status="failed",
                gy_status="failed" if target == "both" else "",
            )
        operation = {
            "first": dispatcher.dispatch_request,
            "supplement": dispatcher.dispatch_missing_targets,
            "retry": dispatcher.resubmit_download_request,
            "successor": dispatcher.resubmit_download_request,
        }[path]
        # 活动请求的原位重试只选择失败的目标。
        selected = "guangya" if path == "retry" and target == "both" else target
        self.client.reset_mock()
        self.client.add_torrent_detailed.side_effect = None
        self.client.add_torrent_detailed.return_value = TorrentAddResult(True, task_ids=(torrent_hash,))
        self.client.delete_torrents.side_effect = None
        self.client.delete_torrents.return_value = True
        return request_id, item, torrent_hash, lambda **options: operation(request_id, selected, **options)

    def offline_case(self, stop_at, *, cleanup="completed"):
        stopped = threading.Event()
        cancellation = asyncio.CancelledError("用户停止了离线准备")
        cloud = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        cloud.close = Mock(return_value=True)
        cloud.file_info = Mock()
        cloud.list_dir = Mock(return_value=[object()] if cleanup == "retained" else [])
        cloud.supports_guarded_empty_directory_delete = True
        cloud.delete_empty_directory = Mock(return_value=True)
        if cleanup == "failed":
            cloud.delete_empty_directory.side_effect = TimeoutError("回收结果未知")
        resolve = cloud.resolve_url

        def resolving(url):
            result = resolve(url)
            if stop_at == "resolve":
                stopped.set()
            return result

        def creating(name, parent):
            cloud.file_info.return_value = SimpleNamespace(
                file_id="cancel-stage", parent_id=parent, name=name, is_dir=True,
                size=0, etag="stage-v1", updated_at=100,
            )
            if stop_at == "create":
                stopped.set()
            return "cancel-stage"

        cloud.resolve_url = Mock(side_effect=resolving)
        cloud.create_dir = Mock(side_effect=creating)

        def check_cancelled():
            if stopped.is_set():
                raise cancellation

        return cloud, stopped, cancellation, check_cancelled

    @staticmethod
    def use_offline(cloud):
        return patch.object(dispatcher, "submit_offline", side_effect=lambda *args, **kwargs: offline.submit_offline(
            *args, client=cloud, **kwargs,
        ))

    def real_cloud_case(self, *, refresh=False, unknown_at=0):
        raw = Mock()
        client = offline_test_fixtures.client_with_raw(self, raw)
        self.addCleanup(client.close)
        stopped = threading.Event()
        cancellation = asyncio.CancelledError("真实光鸭写前取消")
        tree: dict = copy.deepcopy(SINGLE if refresh else RESOLVE_SUBFILES_FIXTURE)
        if not refresh:
            tree["data"].update(excludeIndices=[], subfiles=[
                {"fileIndex": i, "name": f"Episode.{i:03d}.mkv", "size": 1073741824}
                for i in range(501)
            ])
        raw.cloud_resolve_url.return_value = tree
        raw.fs_files.return_value = {"code": 0, "data": {"files": [], "total": 0}}
        raw.fs_delete_empty.return_value = {"code": 0, "msg": "success"}

        def create(**kwargs):
            raw.fs_detail.return_value = {"code": 0, "data": {
                "fileId": "real-stage", "fileName": kwargs["dir_name"],
                "parentId": kwargs["parent_id"] or "0", "isDir": True, "etag": "stage-v1",
            }}
            if refresh:
                raw.token_expires_at = 1
            return {"code": 0, "data": {"fileId": "real-stage"}}

        def refreshing():
            raw.token_expires_at = None
            stopped.set()

        def submit(*args, **kwargs):
            batch = raw.request.call_count
            if batch == (unknown_at or 1):
                stopped.set()
            if batch == unknown_at:
                raise TimeoutError("已发出但响应未知")
            return FakeResponse({"code": 0, "data": {"taskId": f"task-{batch}"}})

        def check():
            if stopped.is_set():
                raise cancellation

        raw.fs_create_dir.side_effect = create
        raw.refresh_token.side_effect = refreshing
        raw.request.side_effect = submit
        raw.cloud_create_task.return_value = {"code": 0, "data": {"taskId": "single-task"}}
        return client, raw, cancellation, check

    def test_real_qb_login_cancellation_prevents_add_and_keeps_original_exception(self):
        for path in ("first", "supplement", "retry", "successor"):
            with self.subTest(path=path):
                _request_id, _item, torrent_hash, operation = self.prepare(path)
                client = QBittorrentClient("http://qb.invalid", username="probe", password="fake")
                self.addCleanup(client.close)
                cancellation = asyncio.CancelledError("登录期间取消")
                stopped = False

                def post(url, **kwargs):
                    nonlocal stopped
                    if url.endswith("/auth/login"):
                        stopped = True
                        return _Response(200, "Ok.")
                    return _Response(200, {"success_count": 1, "added_torrent_ids": [torrent_hash]})

                def check():
                    if stopped:
                        raise cancellation

                client._session.post = Mock(side_effect=post)
                with patch.object(dispatcher, "QBittorrentClient", return_value=client):
                    with self.assertRaises(asyncio.CancelledError) as raised:
                        operation(check_cancelled=check)
                self.assertIs(raised.exception, cancellation)
                self.assertEqual([call.args[0] for call in client._session.post.call_args_list],
                                 ["http://qb.invalid/api/v2/auth/login"])
                with db.get_conn() as conn:
                    row = conn.execute("SELECT * FROM download_requests ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(row["qb_status"], "failed")
                self.assertIn("操作已取消，未提交下载后端", row["error"])
                self.assertFalse(dispatcher.dispatch_request(row["id"], "qb")["ok"])
                self.assertEqual(client._session.post.call_count, 1)

    def test_real_cloud_partial_is_persisted_before_original_cancellation(self):
        for path in ("first", "supplement", "retry", "successor"):
            with self.subTest(path=path):
                _request_id, _item, torrent_hash, operation = self.prepare(path, target="both")
                cloud, raw, cancellation, check = self.real_cloud_case()
                with self.use_offline(cloud), self.assertRaises(asyncio.CancelledError) as raised:
                    operation(check_cancelled=check)
                self.assertIs(raised.exception, cancellation)
                with db.get_conn() as conn:
                    row = conn.execute("SELECT * FROM download_requests ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual((row["qb_status"], row["qb_task_id"]), ("submitted", torrent_hash))
                self.assertEqual((row["gy_status"], row["gy_task_id"]), ("outcome_unknown", "task-1"))
                self.assertEqual((json.loads(row["gy_task_ids"]), row["gy_batch_count"]), (["task-1"], 2))
                self.assertEqual((row["gy_target_dir"], row["gy_staging_cleanup_status"]),
                                 ("real-stage", "retained"))
                self.assertEqual(raw.request.call_count, 1)
                self.assertEqual(raw.request.call_args.kwargs["json"]["fileIndexes"], list(range(500)))
                raw.fs_detail.assert_not_called()
                raw.fs_delete_empty.assert_not_called()
                with self.use_offline(cloud):
                    self.assertFalse(dispatcher.dispatch_request(row["id"], "both")["ok"])
                self.assertEqual(raw.request.call_count, 1)

    def test_real_cloud_partial_receipt_database_failure_precedes_cancellation(self):
        request_id, _item, _hash, operation = self.prepare("first", target="guangya")
        cloud, raw, _cancellation, check = self.real_cloud_case()
        with db.get_conn() as conn:
            conn.execute("CREATE TRIGGER reject_partial_receipt BEFORE UPDATE ON download_requests "
                         "WHEN NEW.gy_status='outcome_unknown' "
                         "BEGIN SELECT RAISE(ABORT,'partial receipt fault'); END")
        with self.use_offline(cloud), self.assertRaisesRegex(sqlite3.IntegrityError, "partial receipt fault"):
            operation(check_cancelled=check)
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual((row["gy_status"], row["gy_target_dir"]), ("submitting", "real-stage"))
        self.assertEqual(raw.request.call_count, 1)
        raw.fs_delete_empty.assert_not_called()

    def test_real_single_file_refresh_cancellation_does_not_create_task(self):
        request_id, _item, _hash, operation = self.prepare("first", target="guangya")
        cloud, raw, cancellation, check = self.real_cloud_case(refresh=True)
        with self.use_offline(cloud), self.assertRaises(asyncio.CancelledError) as raised:
            operation(check_cancelled=check)
        self.assertIs(raised.exception, cancellation)
        raw.refresh_token.assert_called_once_with()
        raw.cloud_create_task.assert_not_called()
        raw.request.assert_not_called()
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual((row["gy_status"], row["gy_staging_cleanup_status"]), ("failed", "completed"),
                         row["gy_staging_cleanup_error"])
        self.assertIn("操作已取消，未提交下载后端", row["error"])
        raw.fs_delete_empty.assert_called_once()

    def test_real_cloud_unknown_receipt_survives_late_cancel_without_cleanup(self):
        for batch in (1, 2):
            with self.subTest(batch=batch):
                request_id, _item, _hash, operation = self.prepare("first", target="guangya")
                cloud, raw, _cancellation, check = self.real_cloud_case(unknown_at=batch)
                with self.use_offline(cloud):
                    result = operation(check_cancelled=check)
                self.assertFalse(result["ok"])
                row = download_requests_repo.get_download_request(request_id)
                assert row is not None
                self.assertEqual((row["gy_status"], row["gy_staging_cleanup_status"]),
                                 ("outcome_unknown", "retained"))
                self.assertEqual(json.loads(row["gy_task_ids"]), ["task-1"] if batch == 2 else [])
                self.assertEqual(raw.request.call_count, batch)
                raw.fs_delete_empty.assert_not_called()

    def test_offline_resolve_cancellation_closes_each_admission_without_overwriting_qb(self):
        for path in ("first", "supplement", "retry", "successor"):
            with self.subTest(path=path):
                _request_id, _item, torrent_hash, operation = self.prepare(path, target="both")
                cloud, _stopped, cancellation, check = self.offline_case("resolve")
                with self.use_offline(cloud), self.assertRaises(asyncio.CancelledError) as raised:
                    operation(check_cancelled=check)
                self.assertIs(raised.exception, cancellation)
                cloud.create_dir.assert_not_called()
                self.assertEqual(cloud.selection_calls, [])
                self.assertEqual(cloud.legacy_calls, [])
                with db.get_conn() as conn:
                    row = conn.execute("SELECT * FROM download_requests ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual((row["qb_status"], row["qb_task_id"], row["gy_status"]),
                                 ("submitted", torrent_hash, "failed"))
                self.assertEqual(row["status"], "submitted")

    def test_admission_guard_error_propagates_without_backend_or_unknown_receipt(self):
        request_id, _item, _hash, operation = self.prepare("first")
        failure = sqlite3.OperationalError("admission guard read failed")
        checks = 0

        def check():
            nonlocal checks
            checks += 1
            if checks == 2:
                raise failure

        with self.assertRaises(sqlite3.OperationalError) as raised:
            operation(check_cancelled=check)
        self.assertIs(raised.exception, failure)
        self.client.add_torrent_detailed.assert_not_called()
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual(row["qb_status"], "submitting")
        self.assertEqual(download_requests_repo.list_download_logs(), [])

    def test_offline_create_cancellation_persists_the_existing_rollback_receipt(self):
        for cleanup in ("completed", "retained", "failed"):
            with self.subTest(cleanup=cleanup):
                request_id, _item, _hash, operation = self.prepare("first", target="guangya")
                cloud, _stopped, cancellation, check = self.offline_case("create", cleanup=cleanup)
                with self.use_offline(cloud), self.assertRaises(asyncio.CancelledError) as raised:
                    operation(check_cancelled=check)
                self.assertIs(raised.exception, cancellation)
                self.assertEqual(cloud.selection_calls, [])
                row = download_requests_repo.get_download_request(request_id)
                assert row is not None
                self.assertEqual((row["status"], row["gy_status"]), ("failed", "failed"))
                self.assertEqual((row["gy_target_dir"], row["gy_staging_cleanup_status"]), ("cancel-stage", cleanup))
                self.assertEqual(row["gy_task_ids"], "[]")
                self.assertEqual(cloud.delete_empty_directory.call_count, int(cleanup != "retained"))
                with self.use_offline(cloud):
                    self.assertFalse(dispatcher.dispatch_request(request_id, "guangya")["ok"])
                self.assertEqual(cloud.create_dir.call_count, 1)
                self.assertEqual(cloud.delete_empty_directory.call_count, int(cleanup != "retained"))

    def test_cancel_before_first_backend_settles_remaining_targets_without_submitting(self):
        request_id, _item, _hash, operation = self.prepare("first", target="both")
        cancellation = asyncio.CancelledError("停止全部未发出目标")
        checks = 0

        def check():
            nonlocal checks
            checks += 1
            if checks == 2:
                raise cancellation

        with patch.object(dispatcher, "_submit_guangya") as cloud, self.assertRaises(asyncio.CancelledError) as raised:
            operation(check_cancelled=check)
        self.assertIs(raised.exception, cancellation)
        self.client.add_torrent_detailed.assert_not_called()
        cloud.assert_not_called()
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual((row["status"], row["qb_status"], row["gy_status"]), ("failed", "failed", "failed"))
        self.assertEqual((row["qb_task_id"], row["gy_task_id"]), ("", ""))

    def test_offline_cancel_receipt_database_error_is_not_masked_by_cancellation(self):
        request_id, _item, _hash, operation = self.prepare("first", target="guangya")
        cloud, _stopped, _cancellation, check = self.offline_case("create")
        with db.get_conn() as conn:
            conn.execute("CREATE TRIGGER reject_cancel_receipt BEFORE UPDATE ON download_requests "
                         "WHEN NEW.gy_status='failed' BEGIN SELECT RAISE(ABORT,'cancel receipt fault'); END")
        with self.use_offline(cloud), self.assertRaisesRegex(sqlite3.IntegrityError, "cancel receipt fault"):
            operation(check_cancelled=check)
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual(row["gy_status"], "submitting")
        self.assertEqual(cloud.selection_calls, [])

    def test_offline_accepted_or_unknown_receipt_survives_late_stop_without_cleanup(self):
        receipts = (
            {"ok": True, "task_ids": ["accepted"]},
            {"ok": False, "outcome_unknown": True, "error": "响应未知"},
            {"ok": False, "partial_success": True, "task_ids": ["partial"], "error": "部分受理"},
        )
        for receipt in receipts:
            with self.subTest(receipt=receipt):
                request_id, _item, _hash, operation = self.prepare("first", target="guangya")
                cloud, stopped, _cancellation, check = self.offline_case("")
                submit = cloud.add_offline_selection

                def accepted(*args, **kwargs):
                    submit(*args, **kwargs)
                    stopped.set()
                    return receipt

                cloud.add_offline_selection = Mock(side_effect=accepted)
                with self.use_offline(cloud):
                    result = operation(check_cancelled=check)
                row = download_requests_repo.get_download_request(request_id)
                assert row is not None
                self.assertEqual(row["gy_status"], "submitted" if receipt["ok"] else "outcome_unknown")
                self.assertEqual(row["gy_staging_cleanup_status"], "pending" if receipt["ok"] else "retained")
                self.assertEqual(result["results"]["guangya"]["task_ids"], receipt.get("task_ids", []))
                cloud.delete_empty_directory.assert_not_called()

    @staticmethod
    def start(operation):
        future: Future[dict] = Future()

        def run():
            try:
                future.set_result(operation())
            except BaseException as exc:
                future.set_exception(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker, future

    def finish(self, task):
        worker, future = task
        worker.join(5)
        self.assertFalse(worker.is_alive(), "提交/取消不应死锁")
        return future.result(timeout=0)

    def assert_qb_not_tracked(self, request_id):
        tracker = DownloadTracker()
        with (
            patch.object(tracker, "_start_local_import") as local_import,
            patch.object(tracker, "_start_organize"),
            patch.object(tracker, "_notify_completion"),
        ):
            row = download_requests_repo.get_download_request(request_id)
            assert row is not None
            tracker._update_request(row, [], [], qb_available=True, gy_available=False)
        local_import.assert_not_called()
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual(row["qb_status"], "cancelled")
        self.assertIsNone(row["qb_task_missing_since"])
        self.assertNotIn("qB", row["error"])

    def test_writer_spans_remote_visibility_through_identity_commit_in_all_paths(self):
        for path in ("first", "supplement", "retry", "successor"):
            for kind in ("magnet", "http"):
                for boundary in ("remote_visible", "before_commit"):
                    with self.subTest(path=path, kind=kind, boundary=boundary):
                        _source_id, item, torrent_hash, operation = self.prepare(path, kind=kind)
                        entered, release, cancelling = threading.Event(), threading.Event(), threading.Event()
                        real_finalize = dispatcher._finalize_submission_state
                        remote_tasks = set()

                        def pause():
                            entered.set()
                            self.assertTrue(release.wait(5), "测试未释放提交边界")

                        def add(**_kwargs):
                            remote_tasks.add(torrent_hash)
                            if boundary == "remote_visible":
                                pause()
                            return TorrentAddResult(True, task_ids=(torrent_hash,))

                        def finalize(request_id, targets, updates):
                            if boundary == "before_commit":
                                pause()
                            return real_finalize(request_id, targets, updates)

                        def remove():
                            cancelling.set()
                            with qb_control_write_lease():
                                remove_qb_tasks(self.client, [torrent_hash])
                            return {}

                        def delete(hashes, *, delete_files):
                            current = download_requests_repo.get_download_request_by_request_key(dispatcher.request_key(item))
                            assert current is not None
                            self.assertEqual(current["qb_status"], "cancelled")
                            self.assertFalse(delete_files)
                            remote_tasks.remove(hashes)
                            return True

                        self.client.add_torrent_detailed.side_effect = add
                        self.client.delete_torrents.side_effect = delete
                        with patch.object(dispatcher, "_finalize_submission_state", side_effect=finalize):
                            submitting = self.start(operation)
                            removing = None
                            try:
                                self.assertTrue(entered.wait(5))
                                current = download_requests_repo.get_download_request_by_request_key(dispatcher.request_key(item))
                                assert current is not None
                                self.assertEqual((current["qb_status"], current["qb_task_id"]), ("submitting", ""))
                                self.assertEqual(remote_tasks, {torrent_hash})
                                contender = CrossProcessLock(
                                    "qb-control-write" if kind == "http" else f"qb-submit-{torrent_hash}"
                                )
                                acquired = contender.acquire(blocking=False)
                                if acquired:
                                    contender.release()
                                self.assertFalse(acquired, "远端已出现而身份未落盘时，移除 writer 不得进入")
                                removing = self.start(remove)
                                self.assertTrue(cancelling.wait(5))
                            finally:
                                release.set()
                                result = self.finish(submitting)
                                if removing is not None:
                                    self.finish(removing)
                        request_id = result["request_id"]
                        self.assertTrue(result["ok"], result)
                        row = download_requests_repo.get_download_request(request_id)
                        assert row is not None
                        self.assertEqual((row["qb_status"], row["qb_task_id"]), ("cancelled", torrent_hash))
                        logs = [row for row in download_requests_repo.list_download_logs() if row["request_id"] == request_id]
                        self.assertEqual([(row["source"], row["status"]) for row in logs], [("qb", "cancelled")])
                        self.assert_qb_not_tracked(request_id)
                        self.assertEqual(remote_tasks, set())
                        self.client.add_torrent_detailed.assert_called_once()
                        self.client.delete_torrents.assert_called_once_with(torrent_hash, delete_files=False)

    def test_cloud_receipt_after_qb_removal_keeps_its_own_result_and_rss_receipt(self):
        receipts = (
            {"ok": True, "task_id": "cloud-late", "task_ids": ["cloud-late"]},
            {"ok": False, "error": "云端明确拒绝"},
            {"ok": False, "outcome_unknown": True, "task_ids": ["cloud-unknown"], "error": "云端回执未知"},
        )
        for path in ("first", "supplement", "retry", "successor"):
            for receipt in receipts:
                with self.subTest(path=path, receipt=receipt):
                    _source_id, item, torrent_hash, operation = self.prepare(path, target="both")
                    sid = rss_repo.add_rss_subscription(f"取消 {self.serial}", f"https://rss.invalid/{self.serial}", download_method="guangya")
                    eid = rss_repo.add_rss_entry_with_media(sid, "Cloud", f"cancel-{self.serial}")["id"]
                    self.assertTrue(rss_repo.claim_rss_entry(eid))
                    bind_rss_entry_download(eid, dispatcher.request_key(item), "guangya")
                    entered, release = threading.Event(), threading.Event()
                    seen = {}

                    def cloud(row, **_kwargs):
                        seen["request_id"] = row["id"]
                        current = download_requests_repo.get_download_request(int(row["id"]))
                        assert current is not None
                        self.assertEqual((current["status"], current["qb_status"], current["gy_status"]),
                                         ("submitting", "submitted", "submitting"))
                        entered.set()
                        self.assertTrue(release.wait(5))
                        return receipt

                    with patch.object(dispatcher, "_submit_guangya", side_effect=cloud) as submit_cloud:
                        submitting = self.start(operation)
                        try:
                            self.assertTrue(entered.wait(5))
                            row = download_requests_repo.get_download_request(seen["request_id"])
                            assert row is not None
                            self.assertEqual((row["qb_status"], row["qb_task_id"]), ("submitted", torrent_hash))
                            contender = CrossProcessLock("qb-control-write")
                            acquired = contender.acquire(blocking=False)
                            if acquired:
                                contender.release()
                            self.assertTrue(acquired, "等待 GY 回执时不能占用 qB writer")
                            with qb_control_write_lease():
                                remove_qb_tasks(self.client, [torrent_hash])
                        finally:
                            release.set()
                            result = self.finish(submitting)
                    request_id = result["request_id"]
                    row = download_requests_repo.get_download_request(request_id)
                    assert row is not None
                    self.assertEqual(row["qb_status"], "cancelled")
                    self.assertEqual(row["gy_status"], "submitted" if receipt["ok"] else "outcome_unknown" if receipt.get("outcome_unknown") else "failed")
                    self.assertFalse(result.get("stale_result"), result)
                    self.assertEqual(result["results"]["guangya"], receipt)
                    entry = rss_repo.get_rss_entry(eid)
                    assert entry is not None
                    self.assertEqual((entry["status"], entry["processed"]), ("downloaded", 1) if receipt["ok"] else ("submitting", 0))
                    self.assertEqual(entry["download_request_id"], request_id)
                    before = dict(row)
                    db.init_db()
                    recovered = download_requests_repo.get_download_request(request_id)
                    assert recovered is not None
                    self.assertEqual(dict(recovered), before)
                    self.assert_qb_not_tracked(request_id)
                    submit_cloud.assert_called_once()

    def test_recovered_backend_does_not_discard_normal_peer_receipt(self):
        for stale in ("qb", "guangya"):
            with self.subTest(stale=stale):
                source_id, _item, _hash, operation = self.prepare("first", target="both")

                def lose_claim(row, **_kwargs):
                    download_requests_repo.update_download_request(row["id"], **{("qb" if stale == "qb" else "gy") + "_status": "manual_review"})
                    return {"ok": True, "task_id": "late"}

                with (
                    patch.object(dispatcher, "_submit_qb", side_effect=lose_claim if stale == "qb" else None,
                                 return_value={"ok": True, "task_id": "normal-qb"}),
                    patch.object(dispatcher, "_submit_guangya", side_effect=lose_claim if stale == "guangya" else None,
                                 return_value={"ok": True, "task_id": "normal-cloud"}),
                ):
                    result = operation()
                row = download_requests_repo.get_download_request(source_id)
                assert row is not None
                self.assertEqual(row["qb_status"], "manual_review" if stale == "qb" else "submitted")
                self.assertEqual(row["gy_status"], "manual_review" if stale == "guangya" else "submitted")
                self.assertTrue(result["stale_result"], result)

    def test_target_receipt_cannot_write_cancelled_peer_fields(self):
        request_id, _item, torrent_hash, _operation = self.prepare("first", target="both")
        self.assertTrue(download_requests_repo.claim_download_request(request_id, "both"))
        download_requests_repo.update_download_request(request_id, qb_task_id=torrent_hash)
        with qb_control_write_lease():
            remove_qb_tasks(self.client, [torrent_hash])
        self.assertEqual(download_requests_repo.finalize_download_request_submission(
            request_id, ("guangya",), gy_status="submitted", gy_task_id="normal-cloud",
            qb_status="submitted", qb_task_id="b" * 40,
        ), "submitted")
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual((row["qb_status"], row["qb_task_id"]), ("cancelled", torrent_hash))

    def test_successful_receipt_preserves_peer_failure_details(self):
        for failed in ("qb", "guangya"):
            with self.subTest(failed=failed):
                request_id, _item, _hash, operation = self.prepare("first", target="both")
                error = "明确失败; 保留完整原因"
                failure = {"ok": False, "error": error}
                with (
                    patch.object(dispatcher, "_submit_qb", return_value=failure if failed == "qb" else {"ok": True, "task_id": "qb-kept"}),
                    patch.object(dispatcher, "_submit_guangya", return_value=failure if failed == "guangya" else {"ok": True, "task_id": "cloud-kept"}),
                ):
                    result = operation()
                self.assertTrue(result["ok"], result)
                row = download_requests_repo.get_download_request(request_id)
                assert row is not None
                self.assertEqual(row["error"], f"{failed}: {error}")

    def test_tracker_preserves_inflight_submission_while_peer_receipt_is_pending(self):
        request_id, _item, torrent_hash, operation = self.prepare("first", target="both")
        tracker = DownloadTracker()
        observed = []

        def cloud(row, **_kwargs):
            for progress in (0.5, 1.0):
                task = TorrentTask(
                    hash=torrent_hash, name="Tracked", progress=progress,
                    state="uploading" if progress == 1.0 else "downloading",
                    save_path="/downloads", content_path="/downloads/Tracked.mkv",
                    size=100, downloaded=int(100 * progress), dlspeed=0, upspeed=0,
                    eta=0, ratio=0.0, category="", added_on=0,
                )
                current = download_requests_repo.get_download_request(request_id)
                assert current is not None
                tracker._update_request(current, [task], [], gy_available=False)
                current = download_requests_repo.get_download_request(request_id)
                assert current is not None
                observed.append((current["status"], current["qb_status"], current["gy_status"]))
            return {"ok": True, "task_id": "cloud-after-tracking"}

        with patch.object(dispatcher, "_submit_guangya", side_effect=cloud), patch.object(
            tracker, "_start_local_import",
        ), patch.object(tracker, "_start_organize"), patch.object(tracker, "_notify_completion"):
            self.assertTrue(operation()["ok"])
        self.assertEqual(observed, [("submitting", "downloading", "submitting"),
                                    ("submitting", "completed", "submitting")])
        current = download_requests_repo.get_download_request(request_id)
        assert current is not None
        self.assertEqual((current["status"], current["qb_status"], current["gy_status"]),
                         ("submitted", "completed", "submitted"))

    def test_cloud_persistence_failure_cannot_revive_cancelled_qb(self):
        _request_id, item, torrent_hash, _operation = self.prepare("first", target="both")
        entered, release = threading.Event(), threading.Event()

        def cloud(row, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"ok": True, "task_id": "cloud-accepted"}

        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER reject_cloud_receipt BEFORE UPDATE ON download_requests "
                "WHEN NEW.gy_status='submitted' BEGIN SELECT RAISE(ABORT,'cloud receipt fault'); END"
            )
        with patch.object(dispatcher, "_submit_guangya", side_effect=cloud):
            submitting = self.start(lambda: submit_download_input(item, "both", origin="cancel-race"))
            try:
                self.assertTrue(entered.wait(5))
                with qb_control_write_lease():
                    remove_qb_tasks(self.client, [torrent_hash])
            finally:
                release.set()
                result = self.finish(submitting)
        row = download_requests_repo.get_download_request(result["request_id"])
        assert row is not None
        self.assertEqual((row["qb_status"], row["gy_status"]), ("cancelled", "manual_review"))
        self.assertTrue(result["dispatch"].get("review_required"))
        self.assert_qb_not_tracked(result["request_id"])

    def test_recovery_read_failure_keeps_unknown_receipt_instead_of_guessing_acceptance(self):
        for read_fails in (False, True):
            with self.subTest(read_fails=read_fails), isolated_test_database():
                _request_id, item, torrent_hash, _operation = self.prepare("first")
                backend_returned = False
                real_read = download_requests_repo.get_download_request

                def add(**_kwargs):
                    nonlocal backend_returned
                    backend_returned = True
                    return TorrentAddResult(True, task_ids=(torrent_hash,))

                def read(request_id):
                    if read_fails and backend_returned:
                        raise sqlite3.OperationalError("injected recovery read failure")
                    return real_read(request_id)

                with db.get_conn() as conn:
                    conn.execute(
                        "CREATE TRIGGER reject_qb_receipt BEFORE UPDATE ON download_requests "
                        "WHEN NEW.qb_status='submitted' BEGIN SELECT RAISE(ABORT,'qb receipt fault'); END"
                    )
                self.client.add_torrent_detailed.side_effect = add
                with patch.object(download_requests_repo, "get_download_request", side_effect=read), patch("app.indexers.downloads.logger") as logger:
                    result = submit_download_input(item, "qb", origin="read-fault")
                self.assertFalse(result["summary"]["ok"])
                self.assertEqual(result["summary"]["status"], "manual_review")
                self.assertTrue(result["dispatch"]["review_required"])
                self.assertEqual(result["dispatch"]["succeeded"], [])
                self.assertEqual(logger.error.call_count, int(read_fails))
                if read_fails:
                    self.assertEqual(logger.error.call_args.args[-1], "OperationalError")
                    self.assertIn("读取失败", logger.error.call_args.args[0])
                row = download_requests_repo.get_download_request(result["request_id"])
                assert row is not None
                self.assertEqual((row["qb_status"], row["qb_task_id"]), ("manual_review", torrent_hash))
                self.assertTrue(submit_download_input(item, "qb", origin="read-fault")["summary"].get("duplicate"))
                self.client.add_torrent_detailed.assert_called_once()

    def test_qb_receipt_persistence_failure_keeps_identity_inside_original_lease(self):
        for path, kind in (
            (path, kind) for path in ("first", "supplement", "retry", "successor")
            for kind in ("magnet", "http")
        ):
            with self.subTest(path=path, kind=kind):
                _request_id, item, torrent_hash, operation = self.prepare(path, kind=kind)
                lock_name = "qb-control-write" if kind == "http" else f"qb-submit-{torrent_hash}"
                observed = []
                finalize = download_requests_repo.finalize_download_request_submission

                def observe_finalize(request_id, targets, **fields):
                    status = finalize(request_id, targets, **fields)
                    if status is not None and fields.get("qb_status") == "manual_review":
                        contender = CrossProcessLock(lock_name)
                        acquired = contender.acquire(blocking=False)
                        if acquired:
                            contender.release()
                        row = download_requests_repo.get_download_request(request_id)
                        assert row is not None
                        observed.append((acquired, row["qb_task_id"]))
                    return status

                with db.get_conn() as conn:
                    conn.execute(
                        "CREATE TRIGGER reject_qb_receipt BEFORE UPDATE ON download_requests "
                        "WHEN NEW.qb_status='submitted' BEGIN SELECT RAISE(ABORT,'receipt write fault'); END"
                    )
                try:
                    with patch.object(download_requests_repo, "finalize_download_request_submission", side_effect=observe_finalize):
                        if path == "first":
                            result = submit_download_input(item, "qb", origin="receipt-fault")
                            self.assertFalse(result["summary"]["ok"])
                            self.assertEqual(result["summary"]["status"], "manual_review")
                        else:
                            with self.assertRaisesRegex(sqlite3.IntegrityError, "receipt write fault"):
                                operation()
                finally:
                    with db.get_conn() as conn:
                        conn.execute("DROP TRIGGER reject_qb_receipt")
                self.client.add_torrent_detailed.assert_called_once()
                self.assertEqual(observed, [(False, torrent_hash)],
                                 "已验证身份必须在原 lease 释放前随核验状态提交")
                row = download_requests_repo.get_download_request_by_request_key(dispatcher.request_key(item))
                assert row is not None
                self.assertEqual((row["qb_status"], row["qb_task_id"]), ("manual_review", torrent_hash))
                if path in {"supplement", "retry"}:
                    self.assertEqual((row["gy_status"], row["gy_task_id"], row["gy_task_ids"]),
                                     ("downloading", "kept-cloud", '["kept-cloud"]'))

    def test_qb_receipt_fault_recovery_preserves_cancelled_and_settled_target(self):
        for status in ("cancelled", "submitted", "completed"):
            with self.subTest(status=status):
                request_id, _item, torrent_hash, operation = self.prepare("first")
                original_error = RuntimeError("original receipt acknowledgement failure")
                committed = {}

                def commit_then_fault(request_id, targets, updates):
                    # 模拟事务已经结算/被接管，随后调用方才收到异常。
                    self.assertIsNotNone(download_requests_repo.finalize_download_request_submission(request_id, targets, **updates))
                    if status == "cancelled":
                        download_requests_repo.cancel_qb_download_tracking([torrent_hash])
                    elif status == "completed":
                        download_requests_repo.update_download_request(request_id, status="completed", qb_status="completed")
                    row = download_requests_repo.get_download_request(request_id)
                    assert row is not None
                    committed.update(dict(row))
                    raise original_error

                with patch.object(dispatcher, "_finalize_submission_state", side_effect=commit_then_fault):
                    with self.assertRaises(RuntimeError) as raised:
                        operation()
                self.assertIs(raised.exception, original_error)
                row = download_requests_repo.get_download_request(request_id)
                assert row is not None
                self.assertEqual(dict(row), committed)
                self.client.add_torrent_detailed.assert_called_once()

    def test_qb_identity_recovery_failure_preserves_original_exception(self):
        request_id, _item, _torrent_hash, operation = self.prepare("first")
        original_error = sqlite3.OperationalError("original receipt commit failure")
        recovery_error = sqlite3.OperationalError("identity recovery failure")
        with patch.object(download_requests_repo, "finalize_download_request_submission", side_effect=[
            original_error, recovery_error,
        ]) as finalize:
            with self.assertRaises(sqlite3.OperationalError) as raised:
                operation()
        self.assertIs(raised.exception, original_error)
        self.assertEqual([call.kwargs["qb_status"] for call in finalize.call_args_list],
                         ["submitted", "manual_review"])
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual((row["status"], row["qb_status"], row["qb_task_id"]), ("submitting", "submitting", ""))
        self.client.add_torrent_detailed.assert_called_once()

    def test_unknown_qb_receipt_persistence_failure_cannot_invent_identity_or_success(self):
        request_id, item, _torrent_hash, _operation = self.prepare("first", kind="http")
        self.client.add_torrent_detailed.return_value = TorrentAddResult(False, "qb_outcome_unknown", False)
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER reject_unknown_receipt BEFORE UPDATE ON download_requests "
                "WHEN NEW.qb_status='outcome_unknown' BEGIN SELECT RAISE(ABORT,'unknown receipt fault'); END"
            )
        result = submit_download_input(item, "qb", origin="unknown-receipt-fault")
        self.assertFalse(result["summary"]["ok"])
        self.assertEqual(result["summary"]["status"], "manual_review")
        self.assertEqual(result["dispatch"]["succeeded"], [])
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual((row["qb_status"], row["qb_task_id"]), ("manual_review", ""))
        self.assertTrue(submit_download_input(item, "qb", origin="unknown-receipt-fault")["summary"]["duplicate"])
        self.client.add_torrent_detailed.assert_called_once()

    def test_qb_receipt_persistence_failure_can_be_cancelled_and_release_rss_claim(self):
        request_id, item, torrent_hash, _operation = self.prepare("first")
        media_key = "tmdb:900006:tv:S01E001"
        sid = rss_repo.add_rss_subscription("Receipt fault", "https://rss.invalid/receipt-fault", download_method="qb")
        eid = rss_repo.add_rss_entry_with_media(
            sid, item.title, "receipt-fault", media_key=media_key,
            payload=json.dumps({"torrent_url": item.source_value}),
        )["id"]
        engine = RSSEngine()
        self.addCleanup(engine.close)
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER reject_qb_receipt BEFORE UPDATE ON download_requests "
                "WHEN NEW.qb_status='submitted' BEGIN SELECT RAISE(ABORT,'receipt write fault'); END"
            )
        try:
            result = engine.download(eid)
        finally:
            # 用户移除时 DB 已恢复正常，不能把残留归因于持续的数据库故障。
            with db.get_conn() as conn:
                conn.execute("DROP TRIGGER reject_qb_receipt")
        self.assertFalse(result["ok"])
        self.assertEqual(result["request_id"], request_id)
        self.client.add_torrent_detailed.assert_called_once()
        with qb_control_write_lease():
            self.assertTrue(remove_qb_tasks(self.client, [torrent_hash]))
        self.client.delete_torrents.assert_called_once_with(torrent_hash, delete_files=False)
        db.init_db()
        row = download_requests_repo.get_download_request(request_id)
        assert row is not None
        self.assertEqual((row["status"], row["qb_status"], row["qb_task_id"]),
                         ("cancelled", "cancelled", torrent_hash))
        entry = rss_repo.get_rss_entry(eid)
        assert entry is not None
        self.assertEqual(entry["download_request_id"], request_id)
        alternative = rss_repo.add_rss_entry_with_media(
            sid, "Alternative", "receipt-alternative", media_key=media_key,
            payload=json.dumps({"torrent_url": f"magnet:?xt=urn:btih:{'b' * 40}"}),
        )
        self.assertEqual(alternative["status"], "pending")
