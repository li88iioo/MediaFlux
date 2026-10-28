"""清理和列表必须使用同一范围，真实临时数据库验证，不接触媒体。"""
import json
import re
import unittest
from typing import Any
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import JSONResponse
from fastapi.testclient import TestClient

from app import config
from app import database as db
from app.main import create_app
from app.routes.logs_api import clear_organize_log_records
from tests.support import initialized_web_runtime, isolated_test_database


class OrganizeLogClearTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())

    def cloud(self, status="failed", name="Match", **kwargs):
        return db.add_organize_log("guangya", "/" + name, "/target", name, status,
                                   original_name=name + ".mkv", **kwargs)

    def local(self, status="failed", name="Match", owner="admin"):
        source = db.create_local_media_source(f"local-{status}-{name}", "", "", "/downloads", owner=owner)
        task = db.create_local_media_task(source, "", f"/downloads/{status}-{name}", owner=owner, trigger="manual")
        db.update_local_media_task(task, owner=owner, status=status)
        return task

    def test_filters_match_listing_across_pages_and_retry_is_idempotent(self):
        ids = [self.cloud(name=f"Match-{i}") for i in range(205)]
        kept = [self.cloud("success"), self.cloud(name="Other"), self.local()]
        filters = {"origin": "guangya", "status": "failed", "keyword": "Match"}
        self.assertEqual(db.count_organize_timeline(**filters), 205)
        self.assertEqual(len(db.list_organize_timeline(**filters)), 20)
        result = db.clear_organize_logs(**filters)
        self.assertEqual(result["deleted"], 205)
        self.assertTrue(all(db.get_organize_log(i) is None for i in ids))
        self.assertTrue(all(db.get_organize_log(i) is not None for i in kept[:2]))
        self.assertIsNotNone(db.get_local_media_task(kept[2], owner="admin"))
        self.assertEqual(db.clear_organize_logs(**filters)["deleted"], 0)

    def test_status_mapping_and_keyword_are_shared_with_listing(self):
        for status in ("failed", "partial_failed", "interrupted", "revert_failed", "deleted", "success", "skipped", "reverted"):
            self.cloud(status, name=status)
        self.local("completed", "local-completed")
        self.local("failed", "local-failed")
        selected = db.list_organize_timeline(status="issues", limit=100)
        result = db.clear_organize_logs(status="issues")
        self.assertEqual(result["deleted"], len(selected))
        self.assertEqual(db.count_organize_timeline(status="failed"), 0)
        self.assertEqual({row["raw_status"] for row in db.list_organize_timeline(status="deleted")}, {"deleted"})
        self.assertEqual(db.clear_organize_logs(status="deleted")["deleted"], 1)
        self.assertEqual(db.clear_organize_logs(status="deleted")["deleted"], 0)
        self.assertEqual(db.clear_organize_logs(origin="local", status="success")["deleted"], 1)
        self.assertIsNotNone(db.list_organize_timeline(status="success")[0])

    def test_pending_processing_and_unmapped_states_are_kept(self):
        kept = [self.cloud("manual"), self.cloud("reorganizing"), self.cloud("planned")]
        kept.append(self.cloud("skipped", "legacy", error="等待人工确认"))
        tasks = [self.local("requires_manual"), self.local("moving")]
        self.cloud("success", "done")
        result = db.clear_organize_logs()
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["skipped_busy"], 6)
        self.assertTrue(all(db.get_organize_log(i) is not None for i in kept))
        self.assertTrue(all(db.get_local_media_task(i, owner="admin") is not None for i in tasks))
        self.assertEqual(db.clear_organize_logs(status="manual")["deleted"], 0)

    def test_active_confirmation_protects_terminal_records_until_finished(self):
        cloud = self.cloud("failed", "review")
        token_log = self.cloud("success", "new", operation_token="recognition-confirm:review-token")
        local = self.local("failed", "local-review")
        db.create_organize_confirmation(
            token="review-token", fingerprint="review", chat_id="", source_name="", directory_path="/",
            payload={"files": [{"file_id": "review"}], "local_task_id": local}, expires_at="2099-01-01 00:00:00",
        )
        result = db.clear_organize_logs()
        self.assertEqual((result["deleted"], result["skipped_busy"]), (0, 3))
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET status='completed'")
        self.assertEqual(db.clear_organize_logs()["deleted"], 3)
        self.assertIsNone(db.get_organize_log(cloud))
        self.assertIsNone(db.get_organize_log(token_log))
        self.assertIsNotNone(db.get_organize_confirmation("review-token"))

    def test_local_page_clear_and_timeline_share_confirmation_protection(self):
        pending = self.local("requires_manual", "pending")
        referenced = self.local("failed", "reviewing")
        finished = self.local("completed", "finished")
        db.create_organize_confirmation(
            token="local-pending", fingerprint="local-pending", chat_id="", source_name="", directory_path="/",
            payload={"local_task_id": pending}, expires_at="2099-01-01 00:00:00",
        )
        db.create_organize_confirmation(
            token="local-review", fingerprint="local-review", chat_id="", source_name="", directory_path="/",
            payload={"local_task_id": referenced}, expires_at="2099-01-01 00:00:00",
        )
        result = db.delete_local_media_tasks([pending, referenced, finished, 999999])
        self.assertEqual(result, {"requested": 4, "deleted": 1, "skipped_busy": 2, "missing": 1})
        self.assertEqual(db.clear_organize_logs(origin="local")["skipped_busy"], 2)
        self.assertIsNotNone(db.get_local_media_task(pending))
        self.assertIsNotNone(db.get_local_media_task(referenced))
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET status='completed'")
        self.assertEqual(db.delete_local_media_tasks([referenced])["deleted"], 1)
        # 确认已结束后仍可显式放弃人工任务；批量清空不会替用户做此决定。
        self.assertEqual(db.clear_organize_logs(origin="local")["deleted"], 0)
        self.assertEqual(db.delete_local_media_tasks([pending])["deleted"], 1)

    def test_local_repository_does_not_coerce_invalid_task_ids(self):
        task = self.local("failed", "kept")
        invalid_ids: list[Any] = [True, 1.9, "1", -1, 0]
        for value in invalid_ids:
            with self.subTest(value=value), self.assertRaises(ValueError):
                db.delete_local_media_tasks([value])
        self.assertIsNotNone(db.get_local_media_task(task))

    def test_children_outside_filter_and_other_owner_survive(self):
        parent = self.cloud("failed", "parent")
        child = self.cloud("success", "child", parent_log_id=parent)
        own, other = self.local(), self.local(owner="another-user")
        self.assertEqual(db.clear_organize_logs(status="failed")["deleted"], 2)
        self.assertIsNone(db.get_local_media_task(own, owner="admin"))
        self.assertIsNotNone(db.get_local_media_task(other, owner="another-user"))
        child_row = db.get_organize_log(child)
        assert child_row is not None
        self.assertIsNone(child_row["parent_log_id"])
        self.assertEqual(child_row["status"], "success")

    def test_database_failure_rolls_back_every_selected_row(self):
        first, second = self.cloud(name="first"), self.cloud(name="second")
        with db.get_conn() as conn:
            conn.execute("CREATE TRIGGER reject_clear BEFORE DELETE ON organize_log BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(Exception):
            db.clear_organize_logs()
        self.assertIsNotNone(db.get_organize_log(first))
        self.assertIsNotNone(db.get_organize_log(second))

    def test_api_rejects_invalid_filters_without_broadening_scope(self):
        kept = self.cloud()
        request = Request({"type": "http", "method": "DELETE", "path": "/api/logs/organize", "headers": []})
        with patch("app.routes.logs_api.require_api_login"):
            for field, value in (("origin", "unknown"), ("status", "unknown"), ("status", False), ("origin", []), ("q", None)):
                with self.subTest(field=field, value=value):
                    result = clear_organize_log_records(request, {"confirm": "CLEAR", field: value})
                    assert isinstance(result, JSONResponse)
                    self.assertEqual(result.status_code, 400)
                    self.assertIsNotNone(db.get_organize_log(kept))
            result = clear_organize_log_records(request, {"confirm": "CLEAR", "origin": "guangya", "status": "failed", "q": " Match "})
        assert isinstance(result, dict)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["q"], "Match")
        self.assertIn("未删除任何云端或本地媒体文件", result["message"])
        self.assertNotIn("error", json.dumps(result))

    def test_authenticated_http_listing_and_clear_use_the_same_real_database_scope(self):
        self.enterContext(initialized_web_runtime())
        selected = self.cloud(name="HTTP Match")
        cloud_other = self.cloud("success", "HTTP Match")
        local_other = self.local(name="HTTP Match")
        client = TestClient(create_app(start_background=False))
        self.addCleanup(client.close)

        def csrf(html):
            match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', html)
            assert match is not None
            return match.group(1)

        token = csrf(client.get('/login').text)
        username, password = config.web_credentials()
        login = client.post('/login', data={"username": username, "password": password, "csrf_token": token}, follow_redirects=False)
        self.assertEqual(login.status_code, 302)
        headers = {"X-CSRF-Token": csrf(client.get('/').text)}
        filters = {"origin": "guangya", "status": "failed", "q": "HTTP Match"}
        before = client.get('/api/logs/organize/timeline', params=filters)
        self.assertEqual(before.status_code, 200)
        self.assertEqual([item['id'] for item in before.json()['items']], [selected])
        result = client.request('DELETE', '/api/logs/organize', headers=headers, json={"confirm": "CLEAR", **filters})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['deleted'], before.json()['total'])
        self.assertEqual(client.get('/api/logs/organize/timeline', params=filters).json()['total'], 0)
        self.assertIsNotNone(db.get_organize_log(cloud_other))
        self.assertIsNotNone(db.get_local_media_task(local_other, owner="admin"))
