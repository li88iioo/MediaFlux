"""光鸭目录视频标记与目录项直接删除 API 回归测试。"""
from __future__ import annotations

import logging
import re
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tests.support import InitializedWebTestCase

from app.clients.guangya import GuangYaFile
from app.config import web_credentials
from app.main import create_app
from app.modules.organize import OrganizeRules


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
            GuangYaFile("junk", "广告.txt", False, 64, "etag-junk", "root"),
            GuangYaFile("folder", "Season 1", True, 0, "", "root"),
        ]
        infos = {
            "junk": GuangYaFile("junk", "广告.txt", False, 64, "etag-junk", "root"),
            "folder": GuangYaFile("folder", "Season 1", True, 0, "", "root"),
        }
        self.provider.file_info.side_effect = infos.get

        self.client_patch = patch(
            "app.routes.guangya_api.GuangYaClient",
            return_value=self.provider,
        )
        self.rules_patch = patch(
            "app.modules.organize.OrganizeRules.from_config",
            return_value=OrganizeRules(video_exts="mkv"),
        )
        self.client_patch.start()
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
        self.client_patch.stop()

    @staticmethod
    def _csrf(html: str) -> str:
        match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', html)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def test_directory_listing_marks_only_configured_video_extensions(self):
        response = self.client.get("/api/guangya/dirs?parent_id=root")

        self.assertEqual(response.status_code, 200)
        rows = {item["name"]: item for item in response.json()}
        self.assertTrue(rows["Supergirl.2026.mkv"]["is_video"])
        self.assertFalse(rows["Supergirl.zh.srt"]["is_video"])
        self.assertFalse(rows["广告.txt"]["is_video"])
        self.assertFalse(rows["Season 1"]["is_video"])
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

    def test_delete_item_calls_provider_once_for_file_and_directory(self):
        for file_id, is_dir in (("junk", False), ("folder", True)):
            with self.subTest(file_id=file_id):
                response = self.client.post(
                    "/api/guangya/delete-item",
                    json={"file_id": file_id},
                    headers=self.headers,
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json(),
                    {"ok": True, "file_id": file_id, "is_dir": is_dir},
                )
                self.provider.delete.assert_called_once_with([file_id])
                self.provider.delete.reset_mock()

    def test_delete_item_hides_provider_error(self):
        self.provider.delete.side_effect = RuntimeError("opaque-provider-secret")
        failed = self.client.post(
            "/api/guangya/delete-item",
            json={"file_id": "junk"},
            headers=self.headers,
        )
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(failed.json(), {"error": "光鸭删除项目失败"})
        self.assertNotIn("opaque-provider-secret", failed.text)

    def test_delete_item_validates_login_id_and_existence(self):
        missing_id = self.client.post(
            "/api/guangya/delete-item",
            json={},
            headers=self.headers,
        )
        self.assertEqual(missing_id.status_code, 400)

        root_item = self.client.post(
            "/api/guangya/delete-item",
            json={"file_id": "0"},
            headers=self.headers,
        )
        self.assertEqual(root_item.status_code, 400)
        self.provider.delete.assert_not_called()

        missing_file = self.client.post(
            "/api/guangya/delete-item",
            json={"file_id": "missing"},
            headers=self.headers,
        )
        self.assertEqual(missing_file.status_code, 404)

        self.provider.logged_in = False
        logged_out = self.client.post(
            "/api/guangya/delete-item",
            json={"file_id": "junk"},
            headers=self.headers,
        )
        self.assertEqual(logged_out.status_code, 503)


if __name__ == "__main__":
    unittest.main()
