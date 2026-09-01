"""光鸭目录视频标记与统一 Writer 删除 API 回归测试。"""
from __future__ import annotations

import logging
import re
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.clients.guangya import GuangYaFile
from app.config import web_credentials
from app.main import create_app
from app.modules.directory_scrape_errors import DirectoryScrapePublicError
from app.modules.organize import OrganizeRules
from tests.support import InitializedWebTestCase


class GuangYaDirectDeleteApiTests(InitializedWebTestCase):
    def setUp(self):
        self.provider = Mock()
        self.provider.logged_in = True
        self.provider.list_dir.return_value = [
            GuangYaFile(
                "video", "Supergirl.2026.mkv", False, 1024, "etag-video", "root",
                created_at=1_700_000_000, updated_at=1_700_000_100,
                mime_type="video/x-matroska", extension="mkv",
            ),
            GuangYaFile("subtitle", "Supergirl.zh.srt", False, 128, "etag-subtitle", "root"),
            GuangYaFile("junk", "广告.txt", False, 64, "etag-junk", "root", updated_at=77),
            GuangYaFile("folder", "Season 1", True, 0, "etag-folder", "root", updated_at=88),
        ]
        infos = {
            "junk": GuangYaFile(
                "junk", "广告.txt", False, 64, "etag-junk", "root", updated_at=77
            ),
            "folder": GuangYaFile(
                "folder", "Season 1", True, 0, "etag-folder", "root", updated_at=88
            ),
        }
        self.provider.file_info.side_effect = infos.get

        self.manager = Mock()
        self.manager.start_operation.return_value = {
            "ok": True,
            "task_id": "delete-task-1",
            "message": "删除光鸭目录项已启动",
        }
        self.client_patch = patch(
            "app.routes.guangya_api.GuangYaClient",
            return_value=self.provider,
        )
        self.manager_patch = patch(
            "app.modules.organize_tasks.get_organize_manager",
            return_value=self.manager,
        )
        self.rules_patch = patch(
            "app.modules.organize.OrganizeRules.from_config",
            return_value=OrganizeRules(video_exts="mkv"),
        )
        self.client_patch.start()
        self.manager_patch.start()
        self.rules_patch.start()

        self.client = TestClient(
            create_app(start_background=False),
            raise_server_exceptions=False,
        )
        login = self.client.get("/login")
        username, password = web_credentials()
        response = self.client.post(
            "/login",
            data={
                "csrf_token": self._csrf(login.text),
                "username": username,
                "password": password,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/guangya")
        self.headers = {"X-CSRF-Token": self._csrf(page.text)}

    def tearDown(self):
        self.client.close()
        self.rules_patch.stop()
        self.manager_patch.stop()
        self.client_patch.stop()

    @staticmethod
    def _csrf(html: str) -> str:
        match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', html)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    @staticmethod
    def _delete_payload(file_id: str, *, is_dir: bool = False) -> dict:
        return {
            "file_id": file_id,
            "expected_name": "Season 1" if is_dir else "广告.txt",
            "expected_parent_id": "root",
            "expected_is_dir": is_dir,
            "expected_etag": "etag-folder" if is_dir else "etag-junk",
            "expected_updated_at": 88 if is_dir else 77,
        }

    def _scheduled_callback(self):
        return self.manager.start_operation.call_args.args[2]

    def test_directory_listing_marks_only_configured_video_extensions(self):
        response = self.client.get("/api/guangya/dirs?parent_id=root")

        self.assertEqual(response.status_code, 200)
        rows = {item["name"]: item for item in response.json()}
        self.assertTrue(rows["Supergirl.2026.mkv"]["is_video"])
        self.assertFalse(rows["Supergirl.zh.srt"]["is_video"])
        self.assertFalse(rows["广告.txt"]["is_video"])
        self.assertFalse(rows["Season 1"]["is_video"])
        self.assertEqual(rows["Supergirl.2026.mkv"]["etag"], "etag-video")
        self.assertEqual(rows["Supergirl.2026.mkv"]["parent_id"], "root")
        self.assertEqual(rows["Supergirl.2026.mkv"]["created_at"], 1_700_000_000)
        self.assertEqual(rows["Supergirl.2026.mkv"]["updated_at"], 1_700_000_100)
        self.assertEqual(rows["Supergirl.2026.mkv"]["mime_type"], "video/x-matroska")
        self.assertEqual(rows["Supergirl.2026.mkv"]["extension"], "mkv")
        self.assertEqual(rows["广告.txt"]["extension"], "txt")

    def test_directory_listing_hides_provider_error_from_response_and_logs(self):
        secret = "opaque-directory-provider-secret"
        self.provider.list_dir.side_effect = RuntimeError(secret)
        records: list[logging.LogRecord] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        route_logger = logging.getLogger("app.routes.guangya_api")
        handler = CaptureHandler()
        route_logger.addHandler(handler)
        try:
            response = self.client.get("/api/guangya/dirs?parent_id=root")
        finally:
            route_logger.removeHandler(handler)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "光鸭目录读取失败"})
        self.assertNotIn(secret, response.text)
        messages = "\n".join(record.getMessage() for record in records)
        self.assertIn("RuntimeError", messages)
        self.assertNotIn(secret, messages)

    def test_delete_item_submits_single_writer_task_and_callback_audits_provider(self):
        for file_id, is_dir in (("junk", False), ("folder", True)):
            with self.subTest(file_id=file_id):
                self.manager.start_operation.reset_mock()
                self.manager.start_operation.return_value = {
                    "ok": True,
                    "task_id": f"delete-{file_id}",
                    "message": "删除光鸭目录项已启动",
                }
                response = self.client.post(
                    "/api/guangya/delete-item",
                    json=self._delete_payload(file_id, is_dir=is_dir),
                    headers=self.headers,
                )

                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json()["task_id"], f"delete-{file_id}")
                args = self.manager.start_operation.call_args
                self.assertEqual(args.args[:2], ("删除光鸭目录项", "Season 1" if is_dir else "广告.txt"))
                self.assertFalse(args.kwargs["queue_if_busy"])
                self.assertEqual(args.kwargs["dedupe_key"], f"guangya-direct-delete:{file_id}")

                result = self._scheduled_callback()()
                self.assertTrue(result["ok"])
                self.assertEqual(result["is_dir"], is_dir)
                self.provider.delete.assert_called_once_with([file_id])
                with db.get_conn() as conn:
                    audit = conn.execute(
                        "SELECT status,trigger,file_id FROM organize_delete_audit "
                        "WHERE id=?",
                        (result["audit_id"],),
                    ).fetchone()
                self.assertEqual(tuple(audit), ("success", "web_direct_delete", file_id))
                self.provider.delete.reset_mock()

    def test_delete_callback_hides_provider_error_and_records_safe_audit(self):
        self.provider.delete.side_effect = RuntimeError("opaque-provider-secret")
        response = self.client.post(
            "/api/guangya/delete-item",
            json=self._delete_payload("junk"),
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202)

        with self.assertRaisesRegex(RuntimeError, "稍后重试") as raised:
            self._scheduled_callback()()
        self.assertNotIn("opaque-provider-secret", str(raised.exception))
        with db.get_conn() as conn:
            audit = conn.execute(
                "SELECT status,error FROM organize_delete_audit "
                "WHERE trigger='web_direct_delete' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(audit["status"], "failed")
        self.assertNotIn("opaque-provider-secret", str(audit["error"]))

    def test_delete_item_validates_request_and_rejects_busy_writer(self):
        missing_id = self.client.post(
            "/api/guangya/delete-item", json={}, headers=self.headers
        )
        self.assertEqual(missing_id.status_code, 400)

        root_item = self.client.post(
            "/api/guangya/delete-item", json={"file_id": "0"}, headers=self.headers
        )
        self.assertEqual(root_item.status_code, 400)

        invalid_type = self.client.post(
            "/api/guangya/delete-item",
            json={"file_id": "junk", "expected_is_dir": "false"},
            headers=self.headers,
        )
        self.assertEqual(invalid_type.status_code, 400)

        self.manager.start_operation.return_value = {
            "ok": False,
            "error": "网盘整理任务正在运行",
        }
        busy = self.client.post(
            "/api/guangya/delete-item",
            json=self._delete_payload("junk"),
            headers=self.headers,
        )
        self.assertEqual(busy.status_code, 409)
        self.provider.delete.assert_not_called()

    def test_delete_callback_revalidates_login_existence_and_snapshot(self):
        scenarios = (
            ("logged_out", {"logged_in": False}, "重新登录"),
            ("missing", {"file_info": None}, "已不存在"),
            (
                "renamed",
                {"file_info": GuangYaFile("junk", "新名称.txt", False, 64, "etag-junk", "root", updated_at=77)},
                "名称已变化",
            ),
        )
        for name, state, expected in scenarios:
            with self.subTest(name=name):
                self.provider.logged_in = bool(state.get("logged_in", True))
                if "file_info" in state:
                    self.provider.file_info.side_effect = None
                    self.provider.file_info.return_value = state["file_info"]
                else:
                    self.provider.file_info.side_effect = lambda key: {
                        "junk": GuangYaFile("junk", "广告.txt", False, 64, "etag-junk", "root", updated_at=77)
                    }.get(key)
                self.manager.start_operation.reset_mock()
                self.manager.start_operation.return_value = {
                    "ok": True, "task_id": f"task-{name}", "message": "started"
                }
                response = self.client.post(
                    "/api/guangya/delete-item",
                    json=self._delete_payload("junk"),
                    headers=self.headers,
                )
                self.assertEqual(response.status_code, 202)
                with self.assertRaisesRegex(DirectoryScrapePublicError, expected):
                    self._scheduled_callback()()
                self.provider.delete.assert_not_called()
                self.provider.logged_in = True


if __name__ == "__main__":
    unittest.main()
