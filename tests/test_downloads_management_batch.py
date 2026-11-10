"""下载管理待处理与日志批量操作契约。"""
from __future__ import annotations

from tests.support import assert_present

import re
import sqlite3
import threading
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.config import web_credentials
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class DownloadManagementBatchApiTests(IsolatedDatabaseTestCase):
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

    @staticmethod
    def _attention(key: str) -> int:
        request_id, created = download_requests_repo.create_download_request(
            key,
            "magnet",
            title=key,
            source_value=f"magnet:?xt=urn:btih:{'a' * 40}",
            origin="web",
        )
        if not created:
            raise AssertionError("测试下载请求未创建")
        download_requests_repo.update_download_request(
            request_id,
            status="failed",
            qb_status="failed",
            error="qB: test failure",
        )
        return request_id

    def test_batch_clear_issues_preserves_requests_logs_tasks_and_files_boundary(self):
        first = self._attention("batch-clear:first")
        second = self._attention("batch-clear:second")
        normal, created = download_requests_repo.create_download_request(
            "batch-clear:normal",
            "magnet",
            title="normal",
            source_value=f"magnet:?xt=urn:btih:{'b' * 40}",
        )
        self.assertTrue(created)
        log_id = download_requests_repo.add_download_log("qb", title="audit", request_id=first)

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
        assert (download_requests_repo.get_download_request(first)) is not None
        assert (download_requests_repo.get_download_request(second)) is not None
        self.assertEqual(download_requests_repo.count_download_requests_requiring_attention(), 0)
        self.assertEqual(download_requests_repo.count_download_logs(), 1)
        self.assertEqual(download_requests_repo.list_download_logs()[0]["id"], log_id)
        self.assertIn("批量", assert_present(download_requests_repo.get_download_request(first))["attention_clear_note"])

    def test_batch_clear_uses_bounded_sql_and_preserves_all_other_fields(self):
        request_ids = [self._attention(f"query-budget:{index}") for index in range(80)]
        self.assertEqual(
            download_requests_repo.clear_download_request_attentions(request_ids[30:50])["cleared"],
            request_ids[30:50],
        )
        for request_id in request_ids[50:]:
            download_requests_repo.update_download_request(request_id, status="pending", qb_status="")
        before = {request_id: dict(assert_present(download_requests_repo.get_download_request(request_id))) for request_id in request_ids}
        log_id = download_requests_repo.add_download_log("qb", title="审计记录", request_id=request_ids[0])
        headers = self._headers()
        statements = []
        original_connect = db._connect

        def connect():
            conn = original_connect()
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(db, "_connect", side_effect=connect):
            response = self.client.post(
                "/api/downloads/issues/batch/clear", headers=headers,
                json={"request_ids": request_ids + list(range(999900, 999920))},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True, "requested": 100, "cleared": 30,
            "already_cleared": 20, "skipped": 50,
        })
        for request_id, original in before.items():
            get_download_request_result_3 = download_requests_repo.get_download_request(request_id)
            assert get_download_request_result_3 is not None
            current = dict(get_download_request_result_3)
            if request_id in request_ids[:30]:
                self.assertTrue(current["attention_cleared_at"])
                self.assertEqual(current["attention_clear_note"], "用户已批量将本记录移出待处理；原状态、错误与下载日志均保留")
                for field in ("attention_cleared_at", "attention_clear_note", "updated_at"):
                    current[field] = original[field]
            self.assertEqual(current, original)
        self.assertEqual([row["id"] for row in download_requests_repo.list_download_logs()], [log_id])
        queries = [sql for sql in statements if "download_requests" in sql.lower()
                   and sql.lstrip().upper().startswith(("SELECT", "UPDATE"))]
        self.assertLessEqual(len(queries), 2, f"100条记录执行了{len(queries)}条下载请求SQL")

    def test_single_clear_preserves_response_note_and_missing_status(self):
        request_id = self._attention("single-clear:contract")
        headers = self._headers()
        path = f"/api/downloads/issues/{request_id}/clear"
        first = self.client.post(path, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), {
            "ok": True, "request_id": request_id,
            "already_cleared": False, "message": "已移出待处理",
        })
        get_download_request_result = download_requests_repo.get_download_request(request_id)
        assert get_download_request_result is not None
        self.assertEqual(
            get_download_request_result["attention_clear_note"],
            "用户已将本记录移出待处理；原状态、错误与下载日志均保留",
        )
        get_download_request_result_4 = download_requests_repo.get_download_request(request_id)
        assert get_download_request_result_4 is not None
        snapshot = dict(get_download_request_result_4)
        repeated = self.client.post(path, headers=headers)
        self.assertEqual(repeated.json(), {**first.json(), "already_cleared": True})
        get_download_request_result_5 = download_requests_repo.get_download_request(request_id)
        assert get_download_request_result_5 is not None
        self.assertEqual(dict(get_download_request_result_5), snapshot)
        for missing in (0, -1, 999999):
            with self.subTest(missing=missing):
                response = self.client.post(f"/api/downloads/issues/{missing}/clear", headers=headers)
                self.assertEqual(response.status_code, 404)

    def test_batch_clear_sql_failure_rolls_back_every_record(self):
        ids = [self._attention(f"clear-rollback:{index}") for index in range(3)]
        before = [dict(assert_present(download_requests_repo.get_download_request(request_id))) for request_id in ids]
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER reject_attention_clear BEFORE UPDATE OF attention_cleared_at "
                f"ON download_requests WHEN NEW.id={ids[1]} "
                "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END"
            )
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic write failure"):
                download_requests_repo.clear_download_request_attentions(ids)
            self.assertEqual([dict(assert_present(download_requests_repo.get_download_request(request_id))) for request_id in ids], before)
        finally:
            with db.get_conn() as conn:
                conn.execute("DROP TRIGGER reject_attention_clear")

    def test_single_and_batch_clear_share_one_serialized_decision(self):
        ids = [self._attention(f"clear-race:{index}") for index in range(5)]
        gate = threading.Barrier(3)
        results, errors = [], []

        def clear(batch):
            try:
                gate.wait(timeout=5)
                results.append((batch, download_requests_repo.clear_download_request_attentions(
                    ids if batch else ids[:1], batch=batch,
                )))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=clear, args=(batch,)) for batch in (False, True)]
        for thread in threads:
            thread.start()
        gate.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sum(len(result["cleared"]) for _, result in results), 5)
        self.assertEqual(sum(len(result["already_cleared"]) for _, result in results), 1)
        for batch, result in results:
            for request_id in result["cleared"]:
                get_download_request_result_2 = download_requests_repo.get_download_request(request_id)
                assert get_download_request_result_2 is not None
                self.assertEqual(
                    get_download_request_result_2["attention_clear_note"],
                    f"用户已{'批量' if batch else ''}将本记录移出待处理；原状态、错误与下载日志均保留",
                )

    def test_batch_clear_logs_deletes_only_selected_audit_rows(self):
        request_id = self._attention("batch-log:request")
        first = download_requests_repo.add_download_log("qb", title="first", request_id=request_id)
        second = download_requests_repo.add_download_log("guangya", title="second", request_id=request_id)
        third = download_requests_repo.add_download_log("qb", title="third", request_id=request_id)

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
        self.assertEqual([int(row["id"]) for row in download_requests_repo.list_download_logs()], [third])
        assert (download_requests_repo.get_download_request(request_id)) is not None
        self.assertEqual(download_requests_repo.count_download_requests_requiring_attention(), 1)

    def test_log_page_is_clamped_after_last_page_batch_cleanup(self):
        log_ids = [download_requests_repo.add_download_log("qb", title=f"log-{index}") for index in range(21)]
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

    def test_filtered_log_clear_spans_pages_and_preserves_new_logs_requests_and_files(self):
        request_id = self._attention("filtered-clear")
        original = dict(assert_present(download_requests_repo.get_download_request(request_id)))
        media = db.resolve_db_path().with_name("preserved.mkv")
        media.write_bytes(b"downloaded media")
        matching = [
            download_requests_repo.add_download_log("guangya_share", status="failed", request_id=request_id,
                                title=title, path=path, backend_task_id=backend, error=error)
            for title, path, backend, error in (
                ("needle", "", "", ""), ("", "needle", "", ""),
                ("", "", "needle", ""), ("", "", "", "needle"),
            )
        ]
        matching.extend(download_requests_repo.add_download_log("guangya_share", title="needle", status="failed",
                                            request_id=request_id) for _ in range(101))
        preserved = [
            download_requests_repo.add_download_log("qb", title="needle", status="failed"),
            download_requests_repo.add_download_log("guangya_share", title="needle", status="success"),
            download_requests_repo.add_download_log("guangya_share", title="other", status="failed"),
        ]
        filters = {"source": "guangya_share", "status": "failed", "keyword": " needle "}
        headers = self._headers()
        page = self.client.get("/api/downloads/logs", headers=headers,
                               params={**filters, "page": 3, "page_size": 20}).json()
        self.assertEqual((page["total"], page["pages"], page["page"]), (105, 6, 3))
        self.assertEqual(page["through_id"], matching[-1])
        self.assertGreater(page["through_id"], page["items"][0]["id"])
        newer = download_requests_repo.add_download_log("guangya_share", title="needle", status="failed")

        with patch("app.routes.downloads_api.remove_qb_tasks") as remove, patch(
            "app.routes.downloads_api._qb"
        ) as backend:
            response = self.client.post("/api/downloads/logs/batch/clear", headers=headers,
                                        json={"scope": "filtered", **filters,
                                              "through_id": page["through_id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "requested": 105, "deleted": 105, "missing": 0})
        self.assertEqual({row["id"] for row in download_requests_repo.list_download_logs()}, {*preserved, newer})
        self.assertEqual(dict(assert_present(download_requests_repo.get_download_request(request_id))), original)
        self.assertEqual(media.read_bytes(), b"downloaded media")
        remove.assert_not_called()
        backend.assert_not_called()
        after = self.client.get("/api/downloads/logs", headers=headers,
                                params={**filters, "page": 3, "page_size": 20}).json()
        self.assertEqual((after["total"], after["pages"], after["page"]), (1, 1, 1))
        self.assertEqual(after["through_id"], newer)

    def test_filtered_log_clear_requires_explicit_filters_and_integer_snapshot(self):
        log_id = download_requests_repo.add_download_log("qb", title="keep")
        headers = self._headers()
        valid = {"scope": "filtered", "source": "", "status": "", "keyword": "", "through_id": log_id}
        invalid: list[dict[str, Any]] = [
            {key: value for key, value in valid.items() if key != missing}
            for missing in valid
        ]
        invalid.extend({**valid, key: value} for key, value in (
            ("source", None), ("status", False), ("keyword", []),
            ("through_id", True), ("through_id", "1"), ("through_id", 1.0),
            ("through_id", -1), ("through_id", 2**63), ("scope", "all"),
            ("log_ids", [log_id]), ("sources", "qb"),
        ))
        invalid.extend(({"log_ids": [log_id], "keyword": "keep"}, {"log_ids": [log_id], "scpoe": "filtered"}))
        for body in invalid:
            with self.subTest(body=body):
                response = self.client.post("/api/downloads/logs/batch/clear", headers=headers, json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(download_requests_repo.count_download_logs(), 1)

    def test_empty_snapshot_and_explicit_unfiltered_clear_do_not_delete_newer_logs(self):
        headers = self._headers()
        filters = {"scope": "filtered", "source": "", "status": "", "keyword": ""}
        empty = self.client.get("/api/downloads/logs", headers=headers).json()
        self.assertEqual((empty["through_id"], empty["total"], empty["pages"], empty["page"]), (0, 0, 0, 1))
        first = download_requests_repo.add_download_log("qb", title="first")
        cleared = self.client.post("/api/downloads/logs/batch/clear", headers=headers,
                                   json={**filters, "through_id": 0})
        self.assertEqual(cleared.json(), {"ok": True, "requested": 0, "deleted": 0, "missing": 0})
        self.assertEqual(download_requests_repo.count_download_logs(), 1)
        snapshot = self.client.get("/api/downloads/logs", headers=headers).json()
        self.assertEqual(snapshot["through_id"], first)
        newer = download_requests_repo.add_download_log("guangya", title="newer", status="success")
        response = self.client.post("/api/downloads/logs/batch/clear", headers=headers,
                                    json={**filters, "through_id": snapshot["through_id"]})
        self.assertEqual(response.json()["deleted"], 1)
        self.assertEqual([row["id"] for row in download_requests_repo.list_download_logs()], [newer])

    def test_log_page_metadata_and_items_share_a_read_snapshot(self):
        first = download_requests_repo.add_download_log("qb", title="visible")
        headers = self._headers()
        original_connect = db._connect
        inserted = []

        def interleave(statement):
            if statement.startswith("SELECT * FROM download_log") and not inserted:
                with db.get_conn() as writer:
                    cursor = writer.execute(
                        "INSERT INTO download_log(source,title,created_at) VALUES('qb','newer',?)", (db.now(),),
                    )
                    inserted.append(cursor.lastrowid)

        def connect():
            conn = original_connect()
            conn.set_trace_callback(interleave)
            return conn

        with patch.object(db, "_connect", side_effect=connect):
            response = self.client.get("/api/downloads/logs", headers=headers)
        self.assertEqual(response.status_code, 200)
        page = response.json()
        self.assertEqual(len(inserted), 1)
        self.assertEqual((page["total"], page["through_id"]), (1, first))
        self.assertEqual([item["id"] for item in page["items"]], [first])
        self.assertEqual(download_requests_repo.count_download_logs(), 2)

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
