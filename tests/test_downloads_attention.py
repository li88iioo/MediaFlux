"""下载及后处理异常必须与看板计数共享口径并可被用户找到。"""
from __future__ import annotations

import re
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app import sdk
from app.clients.qbittorrent import TorrentAddResult
from app.config import web_credentials
from app.main import create_app
from app.modules import download_dispatcher
from app.modules.download_tracker import DownloadTracker
from tests.support import IsolatedDatabaseTestCase


ROOT = Path(__file__).resolve().parents[1]
TORRENT_DATA = (
    b"d4:infod6:lengthi1e4:name4:test12:piece lengthi16384e"
    b"6:pieces20:xxxxxxxxxxxxxxxxxxxxee"
)


def _create_interrupted_request() -> int:
    request_id, created = download_requests_repo.create_download_request(
        "attention:test-request",
        "torrent",
        title="E.T.外星人.1982.2160p",
        source_value="magnet:?xt=urn:btih:secret-value",
        torrent_data=TORRENT_DATA,
        chat_id="-100123",
        user_id="9988",
        message_id="456",
        origin="telegram",
    )
    if not created:
        raise AssertionError("测试下载请求未创建")
    download_requests_repo.update_download_request(
        request_id,
        status="completed",
        gy_status="completed",
        organize_started=-1,
        organize_status="failed",
        organize_error="上次进程在整理任务运行期间中断，需人工核验",
        strm_status="failed",
        strm_error="上次进程在 STRM 同步或排队期间中断",
    )
    return request_id


class DownloadAttentionDatabaseTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM download_log")
            conn.execute("DELETE FROM download_requests")

    def test_attention_list_and_dashboard_count_share_the_same_predicate(self):
        from app.repositories.download_requests import list_download_request_summaries

        request_id = _create_interrupted_request()

        self.assertEqual(download_requests_repo.count_download_requests_requiring_attention(), 1)
        rows = download_requests_repo.list_download_requests_requiring_attention(limit=20, offset=0)
        self.assertEqual([int(row["id"]) for row in rows], [request_id])
        self.assertEqual(db.get_dashboard_automation_summary()["downloads_review"], 1)
        public = list_download_request_summaries(scope="attention")
        self.assertEqual([item["request_number"] for item in public["items"]], [request_id])
        self.assertEqual({issue["key"] for issue in public["items"][0]["postprocess_issues"]},
                         {"organize", "strm"})

    def test_retained_staging_directory_is_visible_as_attention(self):
        from app.repositories.download_requests import list_download_request_summaries

        request_id, created = download_requests_repo.create_download_request(
            "attention:staging-retained",
            "magnet",
            title="Residual staging",
            source_value="magnet:?xt=urn:btih:staging-retained",
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id,
            status="completed",
            gy_status="completed",
            organize_status="completed",
            strm_status="completed",
            gy_staging_cleanup_status="retained",
            gy_staging_cleanup_error="隔离目录仍有 2 项未整理或未识别：sample.txt、poster.jpg",
        )

        self.assertEqual(download_requests_repo.count_download_requests_requiring_attention(), 1)
        rows = download_requests_repo.list_download_requests_requiring_attention(limit=20, offset=0)
        self.assertEqual([int(row["id"]) for row in rows], [request_id])
        item = list_download_request_summaries(scope="attention")["items"][0]
        self.assertEqual(item["request_number"], request_id)
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["postprocess_issues"], [{
            "key": "staging_cleanup", "label": "暂存清理", "status": "retained",
            "error": "隔离目录仍有 2 项未整理或未识别：sample.txt、poster.jpg",
        }])

    def test_resubmit_creates_successor_and_resolves_previous_attention(self):
        request_id = _create_interrupted_request()
        capabilities = {
            "qb": {"enabled": True, "reason": ""},
            "guangya": {"enabled": True, "reason": ""},
            "both": {"enabled": True, "reason": ""},
        }
        dispatch_result = {
            "ok": True,
            "request_id": 0,
            "status": "submitted",
            "succeeded": ["guangya"],
            "failed": [],
            "results": {},
            "error": "",
        }
        with (
            patch.object(download_dispatcher, "download_resubmit_capabilities", return_value=capabilities),
            patch.object(download_dispatcher, "_dispatch_claimed_targets", return_value=dispatch_result),
        ):
            result = download_dispatcher.resubmit_download_request(request_id, "guangya")

        self.assertTrue(result["ok"])
        successor_id = int(result["request_id"])
        self.assertNotEqual(successor_id, request_id)
        successor = download_requests_repo.get_download_request(successor_id)
        assert successor is not None
        self.assertEqual(successor["message_id"], f"resubmit:{request_id}")
        self.assertEqual(successor["origin"], "web")
        self.assertEqual(successor["chat_id"], "-100123")
        self.assertEqual(successor["user_id"], "9988")
        self.assertEqual(successor["kind"], "torrent")
        self.assertEqual(successor["torrent_data"], TORRENT_DATA)
        attention_ids = {
            int(row["id"])
            for row in download_requests_repo.list_download_requests_requiring_attention(limit=100, offset=0)
        }
        self.assertNotIn(request_id, attention_ids)
        self.assertNotIn(successor_id, attention_ids)
        original = download_requests_repo.get_download_request(request_id)
        assert original is not None
        self.assertEqual(original["organize_started"], 1)
        self.assertEqual(original["organize_status"], "resubmitted")
        self.assertEqual(original["strm_status"], "resubmitted")
        self.assertIn(f"请求 #{successor_id}", original["error"])
        active_ids = {int(row["id"]) for row in download_requests_repo.list_active_download_requests()}
        self.assertNotIn(request_id, active_ids)

    def test_manual_review_resubmit_creates_explicit_successor_and_tracks_other_backend(self):
        item = download_dispatcher.DownloadInput(
            kind="magnet",
            title="Recovered request",
            source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
        )
        request_id, created = download_requests_repo.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
            chat_id="-100123",
            user_id="9988",
            message_id="456",
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id,
            targets="both",
            status="manual_review",
            qb_status="submitted",
            gy_status="manual_review",
        )
        duplicate_id, duplicate_created = download_requests_repo.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
        )
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate_id, request_id)
        capabilities = {
            "qb": {"enabled": True, "reason": ""},
            "guangya": {"enabled": True, "reason": ""},
            "both": {"enabled": True, "reason": ""},
        }
        dispatch_result = {
            "ok": True,
            "request_id": 0,
            "status": "submitted",
            "succeeded": ["guangya"],
            "failed": [],
            "results": {},
            "error": "",
        }
        with (
            patch.object(download_dispatcher, "download_resubmit_capabilities", return_value=capabilities),
            patch.object(download_dispatcher, "_dispatch_claimed_targets", return_value=dispatch_result) as dispatch,
        ):
            result = download_dispatcher.resubmit_download_request(request_id, "guangya")

        self.assertTrue(result["ok"])
        successor_id = int(result["request_id"])
        self.assertNotEqual(successor_id, request_id)
        dispatch.assert_called_once()
        self.assertEqual(int(dispatch.call_args.args[0]["id"]), successor_id)
        self.assertEqual(dispatch.call_args.args[1], ("guangya",))
        original = download_requests_repo.get_download_request(request_id)
        successor = download_requests_repo.get_download_request(successor_id)
        assert original is not None
        self.assertEqual(original["status"], "resubmitted")
        self.assertIn(":history:", original["request_key"])
        assert successor is not None
        self.assertEqual(successor["status"], "submitting")
        self.assertEqual(successor["chat_id"], "-100123")
        self.assertEqual(successor["user_id"], "9988")
        active_ids = {int(row["id"]) for row in download_requests_repo.list_active_download_requests()}
        self.assertIn(request_id, active_ids)

        tracker = DownloadTracker()
        completed_task = SimpleNamespace(hash="qb-active", progress=1.0, state="uploading")
        download_requests_repo.update_download_request(request_id, qb_task_id="qb-active")
        with (
            patch.object(tracker, "_match_qb", return_value=completed_task),
            patch.object(tracker, "_start_local_import"),
            patch.object(tracker, "_notify_completion"),
        ):
            tracker._update_request(
                download_requests_repo.get_download_request(request_id),
                [completed_task],
                [],
                qb_available=True,
                gy_available=False,
            )
        settled_original = download_requests_repo.get_download_request(request_id)
        assert settled_original is not None
        self.assertEqual(settled_original["status"], "resubmitted")
        self.assertEqual(settled_original["qb_status"], "completed")
        settled_active_ids = {int(row["id"]) for row in download_requests_repo.list_active_download_requests()}
        self.assertNotIn(request_id, settled_active_ids)

    def test_qb_manual_review_successor_clears_old_attention(self):
        item = download_dispatcher.DownloadInput(
            kind="magnet",
            title="Recovered qB request",
            source_value="magnet:?xt=urn:btih:1123456789abcdef0123456789abcdef01234567",
        )
        request_id, created = download_requests_repo.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id,
            targets="both",
            status="manual_review",
            qb_status="manual_review",
            gy_status="submitted",
        )
        capabilities = {
            "qb": {"enabled": True, "reason": ""},
            "guangya": {"enabled": False, "reason": "active"},
            "both": {"enabled": False, "reason": "active"},
        }
        dispatch_result = {
            "ok": True,
            "request_id": 0,
            "status": "submitted",
            "succeeded": ["qb"],
            "failed": [],
            "results": {},
            "error": "",
        }
        with (
            patch.object(download_dispatcher, "download_resubmit_capabilities", return_value=capabilities),
            patch.object(download_dispatcher, "_dispatch_claimed_targets", return_value=dispatch_result),
        ):
            result = download_dispatcher.resubmit_download_request(request_id, "qb")

        self.assertTrue(result["ok"])
        original = download_requests_repo.get_download_request(request_id)
        assert original is not None
        self.assertEqual(original["status"], "resubmitted")
        self.assertEqual(original["qb_status"], "resubmitted")
        attention_ids = {
            int(row["id"])
            for row in download_requests_repo.list_download_requests_requiring_attention(limit=100, offset=0)
        }
        self.assertNotIn(request_id, attention_ids)
        self.assertIn(
            request_id,
            {int(row["id"]) for row in download_requests_repo.list_active_download_requests()},
        )

    def test_resubmitted_requests_keep_real_status_across_sdk_pages(self):
        item = download_dispatcher.DownloadInput(
            "magnet", "Transferred query fixture", "magnet:?xt=urn:btih:" + "a" * 40
        )
        # 活动按更新时间排序、同秒再按 id；固定时钟避免跨秒时旧请求的移交标记排到新请求前。
        with patch.object(db, "now", return_value="2026-11-09 01:00:00"):
            request_id = int(download_dispatcher.create_request(item, "", "", origin="agent")["id"])
            download_requests_repo.update_download_request(request_id, targets="qb", status="failed", qb_status="failed")
            with patch.object(download_dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "a" * 40}) as submit:
                result = download_dispatcher.resubmit_download_request(
                    request_id, "qb", qb_runtime_config={"url": "http://fixture.invalid"}
                )
        self.assertTrue(result["ok"], result)
        submit.assert_called_once()
        successor_id = result["request_id"]
        first = sdk.downloads.requests(scope="recent", limit=1)
        last = sdk.downloads.requests(scope="recent", limit=1, offset=first["next_offset"])
        self.assertEqual(first["items"][0]["request_number"], successor_id)
        self.assertEqual((first["has_more"], first["next_offset"]), (True, 1))
        self.assertEqual((last["has_more"], last["next_offset"]), (False, None))
        old = last["items"][0]
        self.assertEqual((old["request_number"], old["status"], old["qb_status"]),
                         (request_id, "resubmitted", "resubmitted"))
        search = sdk.activity.search(item.title, limit=1)
        searched_old = sdk.activity.search(item.title, limit=1, offset=search["next_offset"])
        self.assertEqual((search["total"], searched_old["total"]), (2, 2))
        self.assertEqual((searched_old["items"][0]["id"], searched_old["items"][0]["status"]),
                         (request_id, "resubmitted"))
        self.assertEqual([row["request_number"] for row in sdk.downloads.requests()["items"]], [successor_id])
        self.assertEqual(sdk.downloads.requests(scope="attention")["items"], [])
        timeline = sdk.activity.timeline("download", request_id)
        self.assertTrue(timeline["data"]["terminal"])
        self.assertEqual(timeline["status"], "completed")
        self.assertIn("移交", timeline["message"])
        self.assertIn("不代表实际下载或入库完成", timeline["message"])
        self.assertFalse(sdk.activity.timeline("download", successor_id)["data"]["terminal"])

    def test_http_qb_resubmit_preserves_persisted_torrent_identity(self):
        infohash = "7" * 40
        item = download_dispatcher.DownloadInput(
            kind="http",
            title="RSS episode",
            source_value="https://example.invalid/download?id=episode",
            identity_hint=f"btih:{infohash}",
        )
        created = download_dispatcher.create_request(
            item,
            "",
            "rss-original",
            origin="rss:1",
        )
        request_id = int(created["id"])
        download_requests_repo.update_download_request(
            request_id,
            targets="qb",
            status="manual_review",
            qb_status="manual_review",
            qb_task_id=infohash,
        )
        values = {
            "QB_URL": "http://qb.local",
            "QB_USERNAME": "",
            "QB_PASSWORD": "",
            "QB_API_KEY": "token",
        }

        with (
            patch.object(
                download_dispatcher,
                "get",
                side_effect=lambda key, default="": values.get(key, default),
            ),
            patch.object(download_dispatcher, "QBittorrentClient") as client_cls,
        ):
            client_cls.return_value.add_torrent_detailed.return_value = TorrentAddResult(True)
            result = download_dispatcher.resubmit_download_request(request_id, "qb")

        self.assertTrue(result["ok"])
        successor = download_requests_repo.get_download_request(int(result["request_id"]))
        assert successor is not None
        self.assertEqual(successor["qb_task_id"], infohash)
        self.assertEqual(successor["qb_status"], "submitted")
        canonical = download_dispatcher.request_key(
            download_dispatcher.DownloadInput(
                kind="magnet",
                title="",
                source_value=f"magnet:?xt=urn:btih:{infohash}",
            )
        )
        get_download_request_by_request_key_result = download_requests_repo.get_download_request_by_request_key(canonical)
        assert get_download_request_by_request_key_result is not None
        self.assertEqual(
            int(get_download_request_by_request_key_result["id"]),
            int(result["request_id"]),
        )
        task = SimpleNamespace(hash=infohash, name="qB canonical title")
        self.assertIs(DownloadTracker._match_qb(successor, [task]), task)

    def test_http_qb_resubmit_recovers_torrent_identity_from_url(self):
        infohash = "8" * 40
        item = download_dispatcher.DownloadInput(
            kind="http",
            title="Legacy RSS episode",
            source_value=f"https://mikanani.me/Download/day/{infohash}.torrent",
        )
        created = download_dispatcher.create_request(
            item,
            "",
            "rss-legacy",
            origin="rss:1",
        )
        request_id = int(created["id"])
        download_requests_repo.update_download_request(
            request_id,
            targets="qb",
            status="manual_review",
            qb_status="manual_review",
            qb_task_id="",
        )
        values = {
            "QB_URL": "http://qb.local",
            "QB_USERNAME": "",
            "QB_PASSWORD": "",
            "QB_API_KEY": "token",
        }

        with (
            patch.object(
                download_dispatcher,
                "get",
                side_effect=lambda key, default="": values.get(key, default),
            ),
            patch.object(download_dispatcher, "QBittorrentClient") as client_cls,
        ):
            client_cls.return_value.add_torrent_detailed.return_value = TorrentAddResult(True)
            result = download_dispatcher.resubmit_download_request(request_id, "qb")

        self.assertTrue(result["ok"])
        successor = download_requests_repo.get_download_request(int(result["request_id"]))
        assert successor is not None
        self.assertEqual(successor["qb_task_id"], infohash)
        self.assertEqual(successor["qb_status"], "submitted")

    def test_cleaned_torrent_is_recovered_from_qb_5_export_for_resubmit(self):
        item = download_dispatcher.torrent_download_input(
            "recover-from-qb.torrent",
            TORRENT_DATA,
        )
        _title, torrent_id = download_dispatcher.parse_torrent_metadata(TORRENT_DATA)
        request_id, created = download_requests_repo.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
            torrent_data=item.torrent_data,
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id,
            targets="qb",
            status="failed",
            qb_status="failed",
            qb_task_id=torrent_id,
            torrent_data=None,
        )
        values = {
            "QB_URL": "http://qb.local",
            "QB_USERNAME": "",
            "QB_PASSWORD": "",
            "QB_API_KEY": "token",
        }
        dispatch_result = {
            "ok": True,
            "status": "submitted",
            "succeeded": ["qb"],
            "failed": [],
            "error": "",
        }

        with (
            patch.object(
                download_dispatcher,
                "get",
                side_effect=lambda key, default="": values.get(key, default),
            ),
            patch.object(
                download_dispatcher.QBittorrentClient,
                "export_torrent",
                return_value=TORRENT_DATA,
            ) as export_torrent,
            patch.object(
                download_dispatcher,
                "_dispatch_claimed_targets",
                return_value=dispatch_result,
            ),
        ):
            result = download_dispatcher.resubmit_download_request(request_id, "qb")

        self.assertTrue(result["ok"])
        export_torrent.assert_called_once_with(torrent_id)
        successor = download_requests_repo.get_download_request(int(result["request_id"]))
        assert successor is not None
        self.assertEqual(successor["kind"], "torrent")
        self.assertEqual(successor["torrent_data"], TORRENT_DATA)

    def test_cleaned_torrent_without_qb_task_falls_back_to_valid_magnet(self):
        item = download_dispatcher.torrent_download_input(
            "recover-from-magnet.torrent",
            TORRENT_DATA,
        )
        request_id, created = download_requests_repo.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
            torrent_data=None,
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id,
            targets="qb",
            status="failed",
            qb_status="failed",
        )
        dispatch_result = {
            "ok": True,
            "status": "submitted",
            "succeeded": ["qb"],
            "failed": [],
            "error": "",
        }

        with (
            patch.object(download_dispatcher, "get", return_value="http://qb.local"),
            patch.object(
                download_dispatcher,
                "_dispatch_claimed_targets",
                return_value=dispatch_result,
            ),
        ):
            capabilities = download_dispatcher.download_resubmit_capabilities(
                download_requests_repo.get_download_request(request_id)
            )
            result = download_dispatcher.resubmit_download_request(request_id, "qb")

        self.assertTrue(capabilities["qb"]["enabled"])
        self.assertTrue(result["ok"])
        successor = download_requests_repo.get_download_request(int(result["request_id"]))
        assert successor is not None
        self.assertEqual(successor["kind"], "magnet")
        self.assertIsNone(successor["torrent_data"])

    def test_failed_manual_review_successor_preserves_original_attention(self):
        item = download_dispatcher.DownloadInput(
            kind="magnet",
            title="Uncertain qB request",
            source_value="magnet:?xt=urn:btih:2123456789abcdef0123456789abcdef01234567",
        )
        request_id, created = download_requests_repo.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id,
            targets="qb",
            status="manual_review",
            qb_status="manual_review",
        )
        capabilities = {
            "qb": {"enabled": True, "reason": ""},
            "guangya": {"enabled": False, "reason": "unsupported"},
            "both": {"enabled": False, "reason": "unsupported"},
        }

        def fail_dispatch(row, claimed_targets: tuple[str, ...]):
            successor_id = int(row["id"])
            targets = "both" if len(claimed_targets) == 2 else claimed_targets[0]
            download_requests_repo.update_download_request(
                successor_id,
                targets=targets,
                status="failed",
                qb_status="failed",
                error="qB unavailable",
            )
            return {
                "ok": False,
                "request_id": successor_id,
                "status": "failed",
                "succeeded": [],
                "failed": ["qb"],
                "results": {},
                "error": "qB unavailable",
            }

        with (
            patch.object(download_dispatcher, "download_resubmit_capabilities", return_value=capabilities),
            patch.object(download_dispatcher, "_dispatch_claimed_targets", side_effect=fail_dispatch),
        ):
            result = download_dispatcher.resubmit_download_request(request_id, "qb")

        self.assertFalse(result["ok"])
        self.assertTrue(result["source_attention_preserved"])
        self.assertIn("原请求仍保留在待处理列表", result["error"])
        original = download_requests_repo.get_download_request(request_id)
        successor_id = int(result["request_id"])
        successor = download_requests_repo.get_download_request(successor_id)
        assert original is not None
        self.assertEqual(original["status"], "manual_review")
        self.assertEqual(original["qb_status"], "manual_review")
        assert successor is not None
        self.assertEqual(successor["status"], "failed")
        attention_ids = {
            int(row["id"])
            for row in download_requests_repo.list_download_requests_requiring_attention(limit=100, offset=0)
        }
        self.assertIn(request_id, attention_ids)
        self.assertIn(successor_id, attention_ids)

    def test_resubmit_capabilities_block_only_backend_still_active(self):
        item = download_dispatcher.DownloadInput(
            kind="magnet",
            title="Partially uncertain request",
            source_value="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
        )
        request_id, created = download_requests_repo.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id,
            targets="both",
            status="manual_review",
            qb_status="outcome_unknown",
            gy_status="manual_review",
        )

        with (
            patch.object(download_dispatcher, "get", return_value="http://qb.local"),
            patch.object(download_dispatcher, "analyze_offline_url") as analyze,
        ):
            analyze.return_value.allowed = True
            analyze.return_value.reason = ""
            capabilities = download_dispatcher.download_resubmit_capabilities(
                download_requests_repo.get_download_request(request_id)
            )

        self.assertFalse(capabilities["qb"]["enabled"])
        self.assertIn("请勿重复提交", capabilities["qb"]["reason"])
        self.assertTrue(capabilities["guangya"]["enabled"])
        self.assertFalse(capabilities["both"]["enabled"])
        self.assertIn("不能同时重复提交", capabilities["both"]["reason"])

    def test_resubmit_capabilities_never_retry_a_completed_backend(self):
        item = download_dispatcher.DownloadInput(
            kind="magnet",
            title="Completed download with post-processing failure",
            source_value="magnet:?xt=urn:btih:1234567890abcdef1234567890abcdef12345678",
        )
        request_id, created = download_requests_repo.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id,
            targets="both",
            status="completed",
            qb_status="completed",
            gy_status="failed",
            local_import_status="failed",
        )

        with (
            patch.object(download_dispatcher, "get", return_value="http://qb.local"),
            patch.object(download_dispatcher, "analyze_offline_url") as analyze,
        ):
            analyze.return_value.allowed = True
            analyze.return_value.reason = ""
            capabilities = download_dispatcher.download_resubmit_capabilities(
                download_requests_repo.get_download_request(request_id)
            )

        self.assertFalse(capabilities["qb"]["enabled"])
        self.assertIn("已完成", capabilities["qb"]["reason"])
        self.assertTrue(capabilities["guangya"]["enabled"])
        self.assertFalse(capabilities["both"]["enabled"])
        self.assertIn("已完成", capabilities["both"]["reason"])

    def test_explicit_history_resubmit_can_retry_a_completed_backend(self):
        item = download_dispatcher.DownloadInput(
            kind="magnet",
            title="Deleted remote task",
            source_value="magnet:?xt=urn:btih:4234567890abcdef1234567890abcdef12345678",
        )
        request_id, created = download_requests_repo.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
            origin="indexer:nyaa",
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id,
            targets="guangya",
            status="completed",
            gy_status="completed",
        )

        with (
            patch.object(download_dispatcher, "get", return_value="http://qb.local"),
            patch.object(download_dispatcher, "analyze_offline_url") as analyze,
        ):
            analyze.return_value.allowed = True
            analyze.return_value.reason = ""
            capabilities = download_dispatcher.download_resubmit_capabilities(
                download_requests_repo.get_download_request(request_id),
                allow_completed=True,
            )

        self.assertTrue(capabilities["guangya"]["enabled"])

        dispatch_result = {
            "ok": True,
            "status": "submitted",
            "succeeded": ["guangya"],
            "failed": [],
            "error": "",
        }
        with (
            patch.object(download_dispatcher, "get", return_value="http://qb.local"),
            patch.object(download_dispatcher, "analyze_offline_url") as analyze,
            patch.object(download_dispatcher, "_dispatch_claimed_targets", return_value=dispatch_result),
        ):
            analyze.return_value.allowed = True
            analyze.return_value.reason = ""
            result = download_dispatcher.resubmit_download_request(
                request_id,
                "guangya",
                allow_completed=True,
                origin="indexer:nyaa",
            )

        self.assertTrue(result["ok"])
        self.assertNotEqual(result["request_id"], request_id)
        successor = download_requests_repo.get_download_request(int(result["request_id"]))
        assert successor is not None
        self.assertEqual(successor["origin"], "indexer:nyaa")
        get_download_request_result = download_requests_repo.get_download_request(request_id)
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result["status"], "completed")

    def test_resubmit_capabilities_never_retry_a_resubmitted_backend(self):
        item = download_dispatcher.DownloadInput(
            kind="magnet",
            title="Already resubmitted download",
            source_value="magnet:?xt=urn:btih:876543210fedcba9876543210fedcba987654321",
        )
        request_id, created = download_requests_repo.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id,
            targets="both",
            status="manual_review",
            qb_status="failed",
            gy_status="resubmitted",
        )

        with (
            patch.object(download_dispatcher, "get", return_value="http://qb.local"),
            patch.object(download_dispatcher, "analyze_offline_url") as analyze,
        ):
            analyze.return_value.allowed = True
            analyze.return_value.reason = ""
            capabilities = download_dispatcher.download_resubmit_capabilities(
                download_requests_repo.get_download_request(request_id)
            )

        self.assertTrue(capabilities["qb"]["enabled"])
        self.assertFalse(capabilities["guangya"]["enabled"])
        self.assertIn("已重新提交", capabilities["guangya"]["reason"])
        self.assertFalse(capabilities["both"]["enabled"])
        self.assertIn("已重新提交", capabilities["both"]["reason"])

    def test_clear_attention_preserves_original_failure_and_audit_data(self):
        request_id = _create_interrupted_request()
        before = download_requests_repo.get_download_request(request_id)

        result = download_requests_repo.clear_download_request_attentions([request_id], batch=False)

        self.assertEqual(result["cleared"], [request_id])
        self.assertEqual(download_requests_repo.count_download_requests_requiring_attention(), 0)
        self.assertEqual(db.get_dashboard_automation_summary()["downloads_review"], 0)
        after = download_requests_repo.get_download_request(request_id)
        assert after is not None
        assert before is not None
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["organize_status"], before["organize_status"])
        self.assertEqual(after["strm_status"], before["strm_status"])
        self.assertEqual(after["organize_error"], before["organize_error"])
        self.assertTrue(after["attention_cleared_at"])
        self.assertIn("原状态", after["attention_clear_note"])
        self.assertEqual(
            download_requests_repo.clear_download_request_attentions([request_id], batch=False)["already_cleared"],
            [request_id],
        )


class DownloadAttentionApiTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        from tests.support import initialized_web_runtime
        self.enterContext(initialized_web_runtime())
        with db.get_conn() as conn:
            conn.execute("DELETE FROM download_log")
            conn.execute("DELETE FROM download_requests")
        self.client = TestClient(create_app(), raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.client.close()

    @staticmethod
    def _csrf_token(response) -> str:
        match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', response.text)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def _headers(self) -> dict[str, str]:
        login = self.client.get("/login")
        username, password = web_credentials()
        response = self.client.post(
            "/login",
            data={
                "csrf_token": self._csrf_token(login),
                "username": username,
                "password": password,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        return {"X-CSRF-Token": self._csrf_token(self.client.get("/downloads"))}

    def test_issues_api_exposes_actionable_stages_without_source_payload(self):
        request_id = _create_interrupted_request()
        response = self.client.get("/api/downloads/issues", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        item = payload["items"][0]
        self.assertEqual(item["id"], request_id)
        self.assertEqual([stage["key"] for stage in item["stages"]], ["organize", "strm"])
        self.assertIn("需人工核验", item["stages"][0]["error"])
        self.assertEqual(set(item["retry_targets"]), {"qb", "guangya", "both"})
        self.assertNotIn("source_value", item)
        self.assertNotIn("torrent_data", item)

    def test_resubmit_api_dispatches_selected_target(self):
        request_id = _create_interrupted_request()
        capabilities = {
            "qb": {"enabled": True, "reason": ""},
            "guangya": {"enabled": True, "reason": ""},
            "both": {"enabled": True, "reason": ""},
        }
        result = {
            "ok": True,
            "duplicate": False,
            "source_request_id": request_id,
            "request_id": request_id + 1,
            "status": "submitted",
            "succeeded": ["qb", "guangya"],
            "failed": [],
            "error": "",
        }
        with (
            patch("app.routes.downloads_api.download_resubmit_capabilities", return_value=capabilities),
            patch("app.routes.downloads_api.resubmit_download_request", return_value=result) as resubmit,
        ):
            response = self.client.post(
                f"/api/downloads/issues/{request_id}/resubmit",
                headers=self._headers(),
                json={"target": "both"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "both")
        self.assertEqual(payload["succeeded"], ["qb", "guangya"])
        resubmit.assert_called_once_with(request_id, "both")

    def test_issues_api_does_not_duplicate_backend_failure_as_request_failure(self):
        request_id, created = download_requests_repo.create_download_request(
            "attention:backend-failure",
            "magnet",
            title="Backend failure",
            source_value="magnet:?xt=urn:btih:backend-failure",
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id,
            status="failed",
            gy_status="failed",
            error="guangya: 资源中没有符合下载规则的文件：解析器标记为排除",
        )

        response = self.client.get("/api/downloads/issues", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual([stage["key"] for stage in item["stages"]], ["guangya"])
        self.assertEqual(item["stages"][0]["label"], "光鸭")
        self.assertEqual(
            item["stages"][0]["error"],
            "资源中没有符合下载规则的文件：解析器标记为排除",
        )

    def test_issues_api_exposes_retained_staging_cleanup(self):
        request_id, created = download_requests_repo.create_download_request(
            "attention:staging-api",
            "magnet",
            title="Residual staging",
            source_value="magnet:?xt=urn:btih:staging-api",
        )
        self.assertTrue(created)
        download_requests_repo.update_download_request(
            request_id,
            status="completed",
            gy_status="completed",
            organize_status="completed",
            strm_status="completed",
            gy_staging_cleanup_status="retained",
            gy_staging_cleanup_error="隔离目录仍有 1 项未整理或未识别：sample.txt",
        )

        response = self.client.get("/api/downloads/issues", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual([stage["key"] for stage in item["stages"]], ["staging_cleanup"])
        self.assertEqual(item["stages"][0]["label"], "暂存清理")
        self.assertIn("sample.txt", item["stages"][0]["error"])

    def test_clear_issue_only_hides_attention_without_deleting_request(self):
        request_id = _create_interrupted_request()

        response = self.client.post(
            f"/api/downloads/issues/{request_id}/clear",
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        assert (download_requests_repo.get_download_request(request_id)) is not None
        self.assertEqual(download_requests_repo.count_download_requests_requiring_attention(), 0)
        repeated = self.client.post(
            f"/api/downloads/issues/{request_id}/clear",
            headers=self._headers(),
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.json()["already_cleared"])


class DownloadAttentionUiContractTests(unittest.TestCase):
    def test_dashboard_and_download_page_expose_pending_issue_destination(self):
        dashboard = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
        downloads = ((ROOT / "app/templates/downloads.html").read_text(encoding="utf-8") + (ROOT / "app/static/js/downloads.js").read_text(encoding="utf-8"))

        self.assertIn("?view=issues", dashboard)
        self.assertIn("项需处理", dashboard)
        for contract in (
            'id="tabIssuesBtn"',
            'id="viewIssues"',
            'id="issueList"',
            'id="tabIssuesBadge"',
            "function loadIssues(page=1)",
            "async function resubmitIssue(requestId,target)",
            "重新下载到",
            "data-issue-resubmit",
            "/resubmit",
            "async function clearIssue(requestId)",
            "data-issue-clear",
            "/clear",
            "不会删除下载任务、文件或日志",
            "download-attention-operation",
            '<th style="width:42%;">问题</th>',
            'colspan="4"',
            "new URLSearchParams(window.location.search).get('view')",
        ):
            self.assertIn(contract, downloads)
        self.assertNotIn(">异常阶段</th>", downloads)
        self.assertNotIn("项需要确认", dashboard)


if __name__ == "__main__":
    unittest.main()


def test_strm_refresh_pending_remains_visible_until_request_reconciles_completed():
    from app.routes import downloads_api
    from tests.support import isolated_test_database

    with isolated_test_database("refresh-attention.db"):
        request_id, _created = download_requests_repo.create_download_request("refresh-attention", "magnet")
        download_requests_repo.update_download_request(
            request_id, status="completed", gy_status="completed", organize_status="completed",
            strm_status="partial", strm_error="STRM 已完成，媒体库刷新等待后台重试",
            gy_staging_cleanup_status="completed",
        )
        assert download_requests_repo.count_download_requests_requiring_attention(request_id) == 1
        stages = downloads_api._attention_json(download_requests_repo.get_download_request(request_id))["stages"]
        assert any(stage["key"] == "strm" and stage["status"] == "partial" for stage in stages)
        download_requests_repo.update_download_request(request_id, strm_status="completed", strm_error="")
        assert download_requests_repo.count_download_requests_requiring_attention(request_id) == 0
