"""下载及后处理异常必须与看板计数共享口径并可被用户找到。"""
from __future__ import annotations

import re
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.config import web_credentials
from app.main import create_app
from app.modules import download_dispatcher
from app.modules.download_tracker import DownloadTracker
from tests.support import IsolatedDatabaseTestCase


ROOT = Path(__file__).resolve().parents[1]


def _create_interrupted_request() -> int:
    request_id, created = db.create_download_request(
        "attention:test-request",
        "torrent",
        title="E.T.外星人.1982.2160p",
        source_value="magnet:?xt=urn:btih:secret-value",
        torrent_data=b"private-torrent-data",
        chat_id="-100123",
        user_id="9988",
        message_id="456",
        origin="telegram",
    )
    if not created:
        raise AssertionError("测试下载请求未创建")
    db.update_download_request(
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
        request_id = _create_interrupted_request()

        self.assertEqual(db.count_download_requests_requiring_attention(), 1)
        rows = db.list_download_requests_requiring_attention(limit=20, offset=0)
        self.assertEqual([int(row["id"]) for row in rows], [request_id])
        self.assertEqual(db.get_dashboard_automation_summary()["downloads_review"], 1)

    def test_retained_staging_directory_is_visible_as_attention(self):
        request_id, created = db.create_download_request(
            "attention:staging-retained",
            "magnet",
            title="Residual staging",
            source_value="magnet:?xt=urn:btih:staging-retained",
        )
        self.assertTrue(created)
        db.update_download_request(
            request_id,
            status="completed",
            gy_status="completed",
            organize_status="completed",
            strm_status="completed",
            gy_staging_cleanup_status="retained",
            gy_staging_cleanup_error="隔离目录仍有 2 项未整理或未识别：sample.txt、poster.jpg",
        )

        self.assertEqual(db.count_download_requests_requiring_attention(), 1)
        rows = db.list_download_requests_requiring_attention(limit=20, offset=0)
        self.assertEqual([int(row["id"]) for row in rows], [request_id])

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
            patch.object(download_dispatcher, "dispatch_request", return_value=dispatch_result),
        ):
            result = download_dispatcher.resubmit_download_request(request_id, "guangya")

        self.assertTrue(result["ok"])
        successor_id = int(result["request_id"])
        self.assertNotEqual(successor_id, request_id)
        successor = db.get_download_request(successor_id)
        self.assertEqual(successor["message_id"], f"resubmit:{request_id}")
        self.assertEqual(successor["origin"], "web")
        self.assertEqual(successor["chat_id"], "-100123")
        self.assertEqual(successor["user_id"], "9988")
        attention_ids = {
            int(row["id"])
            for row in db.list_download_requests_requiring_attention(limit=100, offset=0)
        }
        self.assertNotIn(request_id, attention_ids)
        self.assertNotIn(successor_id, attention_ids)
        original = db.get_download_request(request_id)
        self.assertEqual(original["organize_started"], 1)
        self.assertEqual(original["organize_status"], "resubmitted")
        self.assertEqual(original["strm_status"], "resubmitted")
        self.assertIn(f"请求 #{successor_id}", original["error"])
        active_ids = {int(row["id"]) for row in db.list_active_download_requests()}
        self.assertNotIn(request_id, active_ids)

    def test_manual_review_resubmit_creates_explicit_successor_and_tracks_other_backend(self):
        item = download_dispatcher.DownloadInput(
            kind="magnet",
            title="Recovered request",
            source_value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
        )
        request_id, created = db.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
            chat_id="-100123",
            user_id="9988",
            message_id="456",
        )
        self.assertTrue(created)
        db.update_download_request(
            request_id,
            targets="both",
            status="manual_review",
            qb_status="submitted",
            gy_status="manual_review",
        )
        duplicate_id, duplicate_created = db.create_download_request(
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
            patch.object(download_dispatcher, "dispatch_request", return_value=dispatch_result) as dispatch,
        ):
            result = download_dispatcher.resubmit_download_request(request_id, "guangya")

        self.assertTrue(result["ok"])
        successor_id = int(result["request_id"])
        self.assertNotEqual(successor_id, request_id)
        dispatch.assert_called_once_with(successor_id, "guangya")
        original = db.get_download_request(request_id)
        successor = db.get_download_request(successor_id)
        self.assertEqual(original["status"], "resubmitted")
        self.assertIn(":history:", original["request_key"])
        self.assertEqual(successor["status"], "pending")
        self.assertEqual(successor["chat_id"], "-100123")
        self.assertEqual(successor["user_id"], "9988")
        active_ids = {int(row["id"]) for row in db.list_active_download_requests()}
        self.assertIn(request_id, active_ids)

        tracker = DownloadTracker()
        completed_task = SimpleNamespace(hash="qb-active", progress=1.0, state="uploading")
        db.update_download_request(request_id, qb_task_id="qb-active")
        with (
            patch.object(tracker, "_match_qb", return_value=completed_task),
            patch.object(tracker, "_start_local_import"),
            patch.object(tracker, "_notify_completion"),
        ):
            tracker._update_request(
                db.get_download_request(request_id),
                [completed_task],
                [],
                qb_available=True,
                gy_available=False,
            )
        settled_original = db.get_download_request(request_id)
        self.assertEqual(settled_original["status"], "resubmitted")
        self.assertEqual(settled_original["qb_status"], "completed")
        settled_active_ids = {int(row["id"]) for row in db.list_active_download_requests()}
        self.assertNotIn(request_id, settled_active_ids)

    def test_qb_manual_review_successor_clears_old_attention(self):
        item = download_dispatcher.DownloadInput(
            kind="magnet",
            title="Recovered qB request",
            source_value="magnet:?xt=urn:btih:1123456789abcdef0123456789abcdef01234567",
        )
        request_id, created = db.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
        )
        self.assertTrue(created)
        db.update_download_request(
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
            patch.object(download_dispatcher, "dispatch_request", return_value=dispatch_result),
        ):
            result = download_dispatcher.resubmit_download_request(request_id, "qb")

        self.assertTrue(result["ok"])
        original = db.get_download_request(request_id)
        self.assertEqual(original["status"], "resubmitted")
        self.assertEqual(original["qb_status"], "resubmitted")
        attention_ids = {
            int(row["id"])
            for row in db.list_download_requests_requiring_attention(limit=100, offset=0)
        }
        self.assertNotIn(request_id, attention_ids)
        self.assertIn(
            request_id,
            {int(row["id"]) for row in db.list_active_download_requests()},
        )

    def test_failed_manual_review_successor_preserves_original_attention(self):
        item = download_dispatcher.DownloadInput(
            kind="magnet",
            title="Uncertain qB request",
            source_value="magnet:?xt=urn:btih:2123456789abcdef0123456789abcdef01234567",
        )
        request_id, created = db.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
        )
        self.assertTrue(created)
        db.update_download_request(
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

        def fail_dispatch(successor_id: int, targets: str):
            db.update_download_request(
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
            patch.object(download_dispatcher, "dispatch_request", side_effect=fail_dispatch),
        ):
            result = download_dispatcher.resubmit_download_request(request_id, "qb")

        self.assertFalse(result["ok"])
        self.assertTrue(result["source_attention_preserved"])
        self.assertIn("原请求仍保留在待处理列表", result["error"])
        original = db.get_download_request(request_id)
        successor_id = int(result["request_id"])
        successor = db.get_download_request(successor_id)
        self.assertEqual(original["status"], "manual_review")
        self.assertEqual(original["qb_status"], "manual_review")
        self.assertEqual(successor["status"], "failed")
        attention_ids = {
            int(row["id"])
            for row in db.list_download_requests_requiring_attention(limit=100, offset=0)
        }
        self.assertIn(request_id, attention_ids)
        self.assertIn(successor_id, attention_ids)

    def test_resubmit_capabilities_block_only_backend_still_active(self):
        item = download_dispatcher.DownloadInput(
            kind="magnet",
            title="Partially uncertain request",
            source_value="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
        )
        request_id, created = db.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
        )
        self.assertTrue(created)
        db.update_download_request(
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
                db.get_download_request(request_id)
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
        request_id, created = db.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
        )
        self.assertTrue(created)
        db.update_download_request(
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
                db.get_download_request(request_id)
            )

        self.assertFalse(capabilities["qb"]["enabled"])
        self.assertIn("已完成", capabilities["qb"]["reason"])
        self.assertTrue(capabilities["guangya"]["enabled"])
        self.assertFalse(capabilities["both"]["enabled"])
        self.assertIn("已完成", capabilities["both"]["reason"])

    def test_resubmit_capabilities_never_retry_a_resubmitted_backend(self):
        item = download_dispatcher.DownloadInput(
            kind="magnet",
            title="Already resubmitted download",
            source_value="magnet:?xt=urn:btih:876543210fedcba9876543210fedcba987654321",
        )
        request_id, created = db.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
        )
        self.assertTrue(created)
        db.update_download_request(
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
                db.get_download_request(request_id)
            )

        self.assertTrue(capabilities["qb"]["enabled"])
        self.assertFalse(capabilities["guangya"]["enabled"])
        self.assertIn("已重新提交", capabilities["guangya"]["reason"])
        self.assertFalse(capabilities["both"]["enabled"])
        self.assertIn("已重新提交", capabilities["both"]["reason"])

    def test_clear_attention_preserves_original_failure_and_audit_data(self):
        request_id = _create_interrupted_request()
        before = db.get_download_request(request_id)

        result = db.clear_download_request_attention(request_id)

        self.assertEqual(result, "cleared")
        self.assertEqual(db.count_download_requests_requiring_attention(), 0)
        self.assertEqual(db.get_dashboard_automation_summary()["downloads_review"], 0)
        after = db.get_download_request(request_id)
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["organize_status"], before["organize_status"])
        self.assertEqual(after["strm_status"], before["strm_status"])
        self.assertEqual(after["organize_error"], before["organize_error"])
        self.assertTrue(after["attention_cleared_at"])
        self.assertIn("原状态", after["attention_clear_note"])
        self.assertEqual(db.clear_download_request_attention(request_id), "already_cleared")


class DownloadAttentionApiTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
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
        request_id, created = db.create_download_request(
            "attention:backend-failure",
            "magnet",
            title="Backend failure",
            source_value="magnet:?xt=urn:btih:backend-failure",
        )
        self.assertTrue(created)
        db.update_download_request(
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
        request_id, created = db.create_download_request(
            "attention:staging-api",
            "magnet",
            title="Residual staging",
            source_value="magnet:?xt=urn:btih:staging-api",
        )
        self.assertTrue(created)
        db.update_download_request(
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
        self.assertIsNotNone(db.get_download_request(request_id))
        self.assertEqual(db.count_download_requests_requiring_attention(), 0)
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
