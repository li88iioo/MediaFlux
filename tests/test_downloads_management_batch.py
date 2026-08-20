"""下载管理待处理与日志批量操作契约。"""
from __future__ import annotations

import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.config import web_credentials
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class DownloadManagementBatchApiTests(IsolatedDatabaseTestCase):
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

    @staticmethod
    def _attention(key: str) -> int:
        request_id, created = db.create_download_request(
            key,
            "magnet",
            title=key,
            source_value=f"magnet:?xt=urn:btih:{'a' * 40}",
            origin="web",
        )
        if not created:
            raise AssertionError("测试下载请求未创建")
        db.update_download_request(
            request_id,
            status="failed",
            qb_status="failed",
            error="qB: test failure",
        )
        return request_id

    def test_batch_clear_issues_preserves_requests_logs_tasks_and_files_boundary(self):
        first = self._attention("batch-clear:first")
        second = self._attention("batch-clear:second")
        normal, created = db.create_download_request(
            "batch-clear:normal",
            "magnet",
            title="normal",
            source_value=f"magnet:?xt=urn:btih:{'b' * 40}",
        )
        self.assertTrue(created)
        log_id = db.add_download_log("qb", title="audit", request_id=first)

        response = self.client.post(
            "/api/downloads/issues/batch/clear",
            headers=self._headers(),
            json={"request_ids": [first, second, first, normal, 999999]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True,
            "requested": 4,
            "cleared": 2,
            "already_cleared": 0,
            "skipped": 2,
        })
        self.assertIsNotNone(db.get_download_request(first))
        self.assertIsNotNone(db.get_download_request(second))
        self.assertEqual(db.count_download_requests_requiring_attention(), 0)
        self.assertEqual(db.count_download_logs(), 1)
        self.assertEqual(db.list_download_logs()[0]["id"], log_id)
        self.assertIn("批量", db.get_download_request(first)["attention_clear_note"])

    def test_batch_clear_logs_deletes_only_selected_audit_rows(self):
        request_id = self._attention("batch-log:request")
        first = db.add_download_log("qb", title="first", request_id=request_id)
        second = db.add_download_log("guangya", title="second", request_id=request_id)
        third = db.add_download_log("qb", title="third", request_id=request_id)

        response = self.client.post(
            "/api/downloads/logs/batch/clear",
            headers=self._headers(),
            json={"log_ids": [first, second, first, 999999]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True,
            "requested": 3,
            "deleted": 2,
            "missing": 1,
        })
        self.assertEqual([int(row["id"]) for row in db.list_download_logs()], [third])
        self.assertIsNotNone(db.get_download_request(request_id))
        self.assertEqual(db.count_download_requests_requiring_attention(), 1)

    def test_log_page_is_clamped_after_last_page_batch_cleanup(self):
        log_ids = [db.add_download_log("qb", title=f"log-{index}") for index in range(21)]
        headers = self._headers()
        before = self.client.get(
            "/api/downloads/logs?page=2&page_size=20",
            headers=headers,
        )
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.json()["page"], 2)
        self.assertEqual(len(before.json()["items"]), 1)
        last_page_id = int(before.json()["items"][0]["id"])
        self.assertEqual(last_page_id, log_ids[0])

        cleared = self.client.post(
            "/api/downloads/logs/batch/clear",
            headers=headers,
            json={"log_ids": [last_page_id]},
        )
        self.assertEqual(cleared.status_code, 200)

        after = self.client.get(
            "/api/downloads/logs?page=2&page_size=20",
            headers=headers,
        )
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.json()["page"], 1)
        self.assertEqual(after.json()["pages"], 1)
        self.assertEqual(len(after.json()["items"]), 20)

    def test_batch_resubmit_contains_partial_failures_and_skips(self):
        succeeded_id = self._attention("batch-resubmit:succeeded")
        disabled_id = self._attention("batch-resubmit:disabled")
        partial_id = self._attention("batch-resubmit:partial")
        failed_id = self._attention("batch-resubmit:failed")

        def capabilities(row):
            enabled = int(row["id"]) != disabled_id
            reason = "原资源不支持此目标" if not enabled else ""
            return {
                "qb": {"enabled": enabled, "reason": reason},
                "guangya": {"enabled": enabled, "reason": reason},
                "both": {"enabled": enabled, "reason": reason},
            }

        def resubmit(request_id: int, target: str):
            self.assertEqual(target, "both")
            if request_id == succeeded_id:
                return {
                    "ok": True,
                    "request_id": 101,
                    "status": "submitted",
                    "succeeded": ["qb", "guangya"],
                    "failed": [],
                    "error": "",
                }
            if request_id == partial_id:
                return {
                    "ok": True,
                    "request_id": 102,
                    "status": "submitted",
                    "succeeded": ["qb"],
                    "failed": ["guangya"],
                    "error": "guangya failed",
                }
            raise RuntimeError("private backend failure")

        with (
            patch("app.routes.downloads_api.download_resubmit_capabilities", side_effect=capabilities),
            patch("app.routes.downloads_api.resubmit_download_request", side_effect=resubmit) as submit,
        ):
            response = self.client.post(
                "/api/downloads/issues/batch/resubmit",
                headers=self._headers(),
                json={
                    "request_ids": [
                        succeeded_id,
                        disabled_id,
                        partial_id,
                        failed_id,
                        999999,
                    ],
                    "target": "both",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["requested"], 5)
        self.assertEqual(payload["succeeded"], 1)
        self.assertEqual(payload["partial"], 1)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["skipped"], 2)
        self.assertEqual(
            [item["outcome"] for item in payload["items"]],
            ["succeeded", "skipped", "partial", "failed", "skipped"],
        )
        self.assertEqual(
            [call.args for call in submit.call_args_list],
            [(succeeded_id, "both"), (partial_id, "both"), (failed_id, "both")],
        )
        self.assertNotIn("private backend failure", str(payload))

    def test_batch_endpoints_validate_ids_and_target_before_processing(self):
        headers = self._headers()
        cases = (
            ("/api/downloads/issues/batch/clear", {"request_ids": []}, "至少选择一条记录"),
            ("/api/downloads/logs/batch/clear", {"log_ids": [True]}, "记录 ID 格式无效"),
            (
                "/api/downloads/issues/batch/resubmit",
                {"request_ids": [1], "target": "invalid"},
                "下载目标无效",
            ),
            (
                "/api/downloads/issues/batch/clear",
                {"request_ids": list(range(1, 102))},
                "单次最多操作 100 条记录",
            ),
        )
        for path, body, message in cases:
            with self.subTest(path=path, message=message):
                response = self.client.post(path, headers=headers, json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], message)
