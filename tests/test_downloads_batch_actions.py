"""下载页 qBittorrent 批量任务操作契约。"""
from __future__ import annotations

import re
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.config import web_credentials
from app.main import create_app
from tests.support import InitializedWebTestCase


class DownloadBatchActionApiTests(InitializedWebTestCase):
    def setUp(self) -> None:
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
        page = self.client.get("/downloads")
        return {"X-CSRF-Token": self._csrf_token(page)}

    def test_qb_connection_test_uses_current_draft_without_saving(self):
        headers = self._headers()
        with patch(
            "app.routes.downloads_api.QBittorrentClient.test_connection",
            return_value={
                "app": "5.2.3",
                "webapi": "2.11.4",
                "auth_mode": "api_key",
            },
        ) as test_connection:
            response = self.client.post(
                "/api/downloads/qb/test",
                headers=headers,
                json={
                    "url": "http://192.168.1.20:8080",
                    "username": "",
                    "password": "",
                    "api_key": "draft-key",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["app_version"], "5.2.3")
        self.assertEqual(response.json()["auth_mode"], "api_key")
        test_connection.assert_called_once_with()

    def test_qb_connection_test_never_reuses_saved_secret_for_new_url(self):
        headers = self._headers()
        with patch(
            "app.routes.downloads_api.config.get",
            side_effect=lambda key, default="": {
                "QB_URL": "http://192.168.1.10:8080",
                "QB_API_KEY": "saved-secret",
            }.get(key, default),
        ), patch("app.routes.downloads_api.QBittorrentClient") as client:
            response = self.client.post(
                "/api/downloads/qb/test",
                headers=headers,
                json={
                    "url": "http://192.168.1.20:8080",
                    "username": "",
                    "password": "",
                    "api_key": "********",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("重新输入", response.json()["error"])
        client.assert_not_called()

    def test_batch_resume_deduplicates_hashes_and_reports_accepted_count(self):
        headers = self._headers()
        client = Mock()
        first = "A" * 40
        second = "b" * 64
        with patch("app.routes.downloads_api._qb", return_value=client):
            response = self.client.post(
                "/api/downloads/qb/resume",
                headers=headers,
                json={"hashes": [first, second, first.lower()]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True, "action": "resume", "accepted": 2})
        client.resume_torrents.assert_called_once_with(f"{first.lower()}|{second}")

    def test_batch_pause_rejects_non_array_hashes(self):
        headers = self._headers()
        client = Mock()
        first = "1" * 40
        second = "2" * 40
        with patch("app.routes.downloads_api._qb", return_value=client):
            response = self.client.post(
                "/api/downloads/qb/pause",
                headers=headers,
                json={"hashes": f"{first}|{second}"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "任务 hashes 必须是数组")
        client.pause_torrents.assert_not_called()

    def test_batch_delete_never_deletes_downloaded_files(self):
        headers = self._headers()
        client = Mock()
        torrent_hash = "3" * 40
        with patch("app.routes.downloads_api._qb", return_value=client):
            response = self.client.post(
                "/api/downloads/qb/delete",
                headers=headers,
                json={"hashes": [torrent_hash], "delete_files": True},
            )

        self.assertEqual(response.status_code, 200)
        client.delete_torrents.assert_called_once_with(torrent_hash, delete_files=False)

    def test_batch_rejects_invalid_or_excessive_hashes_before_qb_connection(self):
        headers = self._headers()
        for payload, message in (
            ({"hashes": []}, "至少选择一个任务"),
            ({"hashes": ["not-a-hash"]}, "任务 hash 格式无效"),
            ({"hashes": ["a" * 40] * 201}, "单次最多操作 200 个任务"),
        ):
            with self.subTest(payload=payload), patch("app.routes.downloads_api._qb") as qb:
                response = self.client.post(
                    "/api/downloads/qb/pause",
                    headers=headers,
                    json=payload,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], message)
                qb.assert_not_called()

    def test_batch_rejects_unknown_action_before_qb_connection(self):
        headers = self._headers()
        with patch("app.routes.downloads_api._qb") as qb:
            response = self.client.post(
                "/api/downloads/qb/recheck",
                headers=headers,
                json={"hashes": ["a" * 40]},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "不支持的操作")
        qb.assert_not_called()
