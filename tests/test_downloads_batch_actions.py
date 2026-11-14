"""下载页 qBittorrent 批量任务操作契约。"""
from __future__ import annotations

import base64
import hashlib
import re
import sqlite3
from contextlib import contextmanager
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.repositories import download_requests as download_requests_repo
from app.config import web_credentials
from app.main import create_app
from app.modules.qb_control import QBControlConflict
from tests.support import InitializedWebTestCase, isolated_test_database


class LoggedInDownloadsApiCase(InitializedWebTestCase):
    """下载 API 测试共用的登录会话与 CSRF 头；本类不含测试方法。"""

    client: TestClient

    def setUp(self) -> None:
        from tests.support import initialized_web_runtime
        self.enterContext(initialized_web_runtime())
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


class DownloadBatchActionApiTests(LoggedInDownloadsApiCase):

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
        with patch(
            "app.repositories.local_media.list_local_media_qb_write_conflicts",
            return_value=[],
        ), patch("app.routes.downloads_api._qb", return_value=client):
            response = self.client.post(
                "/api/downloads/qb/resume",
                headers=headers,
                json={"hashes": [first, second, first.lower()]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True, "action": "resume", "accepted": 2})
        client.resume_torrents.assert_called_once_with(f"{first.lower()}|{second}")

    def test_batch_resume_keeps_safety_check_and_qb_call_in_one_lease(self):
        headers = self._headers()
        torrent_hash = "c" * 40
        state = {"leased": False}
        client = Mock()

        @contextmanager
        def lease():
            self.assertFalse(state["leased"])
            state["leased"] = True
            try:
                yield
            finally:
                state["leased"] = False

        def check_allowed(hashes, *, operation):
            self.assertTrue(state["leased"])
            self.assertEqual((hashes, operation), ([torrent_hash], "resume"))

        def resume(hashes):
            self.assertTrue(state["leased"])
            self.assertEqual(hashes, torrent_hash)

        client.resume_torrents.side_effect = resume
        with patch(
            "app.routes.downloads_api.qb_control_write_lease", side_effect=lease,
        ), patch(
            "app.routes.downloads_api.assert_qb_control_allowed",
            side_effect=check_allowed,
        ), patch("app.routes.downloads_api._qb", return_value=client):
            response = self.client.post(
                "/api/downloads/qb/resume",
                headers=headers,
                json={"hashes": [torrent_hash]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(state["leased"])
        client.resume_torrents.assert_called_once_with(torrent_hash)

    def test_batch_resume_and_delete_fail_closed_when_safety_state_is_unavailable(self):
        headers = self._headers()
        torrent_hash = "d" * 40
        for action in ("resume", "delete"):
            with self.subTest(action=action), patch(
                "app.repositories.local_media.list_local_media_qb_write_conflicts",
                side_effect=sqlite3.OperationalError("no such table: local_media_tasks"),
            ), patch("app.routes.downloads_api._qb") as qb:
                response = self.client.post(
                    f"/api/downloads/qb/{action}",
                    headers=headers,
                    json={"hashes": [torrent_hash]},
                )

            self.assertEqual(response.status_code, 503)
            self.assertIn("安全状态暂不可用", response.json()["error"])
            qb.assert_not_called()

    def test_batch_pause_is_allowed_without_reading_local_safety_state(self):
        headers = self._headers()
        torrent_hash = "e" * 40
        client = Mock()
        with patch(
            "app.repositories.local_media.list_local_media_qb_write_conflicts",
        ) as safety_state, patch(
            "app.routes.downloads_api._qb", return_value=client,
        ):
            response = self.client.post(
                "/api/downloads/qb/pause",
                headers=headers,
                json={"hashes": [torrent_hash]},
            )

        self.assertEqual(response.status_code, 200)
        safety_state.assert_not_called()
        client.pause_torrents.assert_called_once_with(torrent_hash)

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
        with isolated_test_database(), patch(
            "app.repositories.local_media.list_local_media_qb_write_conflicts",
            return_value=[],
        ), patch("app.routes.downloads_api._qb", return_value=client):
            response = self.client.post(
                "/api/downloads/qb/delete",
                headers=headers,
                json={"hashes": [torrent_hash], "delete_files": True},
            )

        self.assertEqual(response.status_code, 200)
        client.delete_torrents.assert_called_once_with(torrent_hash, delete_files=False)

    def test_batch_resume_rejects_active_local_media_write(self):
        headers = self._headers()
        torrent_hash = "4" * 40
        with patch(
            "app.routes.downloads_api.assert_qb_control_allowed",
            side_effect=QBControlConflict("所选下载任务正在执行本地整理写入"),
        ), patch("app.routes.downloads_api._qb") as qb:
            response = self.client.post(
                "/api/downloads/qb/resume",
                headers=headers,
                json={"hashes": [torrent_hash]},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("本地整理", response.json()["error"])
        qb.assert_not_called()

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

    def test_web_delete_closes_bound_download_before_qb_call(self):
        headers = self._headers()
        torrent_hash = "7" * 40
        with isolated_test_database():
            request_id, _ = download_requests_repo.create_download_request("web-delete", "magnet", title="Web delete")
            download_requests_repo.update_download_request(request_id, status="downloading", qb_status="downloading", qb_task_id=torrent_hash)
            client = Mock()

            def delete(hashes, *, delete_files):
                self.assertEqual(hashes, torrent_hash)
                self.assertFalse(delete_files)
                get_download_request_result = download_requests_repo.get_download_request(request_id)
                assert get_download_request_result is not None
                self.assertEqual(get_download_request_result["qb_status"], "cancelled")
                return True

            client.delete_torrents.side_effect = delete
            with patch("app.routes.downloads_api._qb", return_value=client):
                response = self.client.post("/api/downloads/qb/delete", headers=headers, json={"hashes": [torrent_hash]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(download_requests_repo.list_active_download_requests(), [])

    def test_web_delete_timeout_reports_unknown_not_success(self):
        headers = self._headers()
        torrent_hash = "8" * 40
        with isolated_test_database():
            client = Mock()
            client.delete_torrents.side_effect = TimeoutError("test")
            with patch("app.routes.downloads_api._qb", return_value=client):
                response = self.client.post("/api/downloads/qb/delete", headers=headers, json={"hashes": [torrent_hash]})
        self.assertEqual(response.status_code, 502)
        self.assertIn("qB 任务移除未确认", response.json()["error"])
        self.assertNotIn("success", response.json())


def _torrent_bytes(name: bytes = "测试.S01E01.mkv".encode()) -> bytes:
    from tests.test_torrent_manifest import _torrent

    return _torrent({b"length": 4_096, b"name": name, b"piece length": 16_384, b"pieces": b"0" * 20})


class DirectDownloadApiTests(LoggedInDownloadsApiCase):
    @staticmethod
    def _request_count() -> int:
        from app import database as db

        with db.get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0]

    def test_preview_reads_torrent_or_link_without_creating_requests(self):
        headers = self._headers()
        response = self.client.post("/api/downloads/direct/preview", headers=headers,
                                    files={"file": ("测试.torrent", _torrent_bytes(), "application/x-bittorrent")})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual((payload["kind"], payload["title"], payload["file_count"], payload["total_size"]),
                         ("torrent", "测试.S01E01.mkv", 1, 4_096))
        self.assertEqual(set(payload["backends"]), {"qb", "guangya"})
        self.assertNotIn("tracker", response.text)
        magnet = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Show&tr=" + "x" * 1500
        response = self.client.post("/api/downloads/direct/preview", headers=headers, data={"url": magnet})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["kind"], "magnet")
        self.assertEqual(self._request_count(), 0)

    def test_invalid_inputs_are_rejected_before_any_request(self):
        headers = self._headers()
        magnet = "magnet:?xt=urn:btih:" + "a" * 40
        cases: tuple[tuple[dict[str, str] | None, dict[str, tuple[str, bytes, str]] | None], ...] = (
            (None, {"file": ("a.txt", b"hello", "text/plain")}),
            (None, {"file": ("bad.torrent", b"not bencode", "application/x-bittorrent")}),
            (None, {"file": ("huge.torrent", b"0" * (4 * 1024 * 1024 + 1), "application/x-bittorrent")}),
            ({"url": "https://example.com/page"}, None),
            ({"url": magnet}, {"file": ("x.torrent", _torrent_bytes(), "application/x-bittorrent")}),
        )
        for data, files in cases:
            with self.subTest(data=data, files=files and list(files["file"][:1])):
                response = self.client.post("/api/downloads/direct/preview", headers=headers, data=data, files=files)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.client.post("/api/downloads/direct/preview",
                                          data={"url": "magnet:?xt=urn:btih:" + "a" * 40}).status_code, 403)
        self.assertEqual(self._request_count(), 0)

    def test_submit_uses_the_shared_direct_download_service(self):
        headers = self._headers()
        result = {"request_id": 7, "status": "submitted", "summary": "下载请求 #7 已提交"}
        with patch("app.indexers.downloads.submit_direct_download", return_value=result) as submit:
            response = self.client.post("/api/downloads/direct/submit", headers=headers, data={"target": "qb"},
                                        files={"file": ("测试.torrent", _torrent_bytes(), "application/x-bittorrent")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["request_id"], 7)
        item, target = submit.call_args.args
        self.assertEqual((item.kind, item.title, target, submit.call_args.kwargs["origin"]), ("torrent", "测试.S01E01.mkv", "qb", "web"))
        with patch("app.indexers.downloads.submit_direct_download", side_effect=ValueError("下载目标无效")):
            response = self.client.post("/api/downloads/direct/submit", headers=headers,
                                        data={"target": "nope", "url": "magnet:?xt=urn:btih:" + "b" * 40})
        self.assertEqual(response.status_code, 400)
        self.assertIn("下载目标无效", response.text)

    def test_confirmed_torrent_keeps_bytes_and_deduplicates_equivalent_magnets(self):
        from tests.test_torrent_manifest import _bencode, _torrent

        headers = self._headers()
        title = "测试 100% + S01E01.mkv"
        v1 = {b"name": b"legacy-\xff", b"name.utf-8": title.encode(), b"length": 4_096,
              b"piece length": 16_384, b"pieces": b"0" * 20}
        v2 = {b"name": b"v2", b"meta version": 2, b"piece length": 16_384,
              b"file tree": {b"Demo.mkv": {b"": {b"length": 4_096, b"pieces root": b"1" * 32}}}}
        hybrid = {**v2, b"name": b"hybrid", b"pieces": b"0" * 20,
                  b"files": [{b"length": 4_096, b"path": [b"Demo.mkv"]}]}
        for count, info in enumerate((v1, v2, hybrid), 1):
            payload = _torrent(info)
            upload = {"file": ("上传 名称.TORRENT", payload, "application/x-bittorrent")}
            raw_info = _bencode(info)
            hashes = (["urn:btih:" + base64.b32encode(hashlib.sha1(raw_info).digest()).decode()]
                      if info is not v2 else [])
            if info is not v1:
                hashes.append("urn:btmh:1220" + hashlib.sha256(raw_info).hexdigest())
            with self.subTest(version=info.get(b"name")), \
                 patch("app.indexers.downloads.download_target_readiness", return_value={"qb": True}), \
                 patch("app.modules.download_dispatcher._submit_qb", return_value={"ok": True}) as backend:
                preview = self.client.post("/api/downloads/direct/preview", headers=headers, files=upload)
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual((preview.json()["file_count"], preview.json()["total_size"]), (1, 4_096))
                if info is v1:
                    self.assertEqual(preview.json()["title"], title)
                self.assertEqual(self._request_count(), count - 1)
                backend.assert_not_called()
                response = self.client.post("/api/downloads/direct/submit", headers=headers,
                                            data={"target": "qb"}, files=upload)
                self.assertEqual(response.status_code, 200, response.text)
                first = response.json()
                self.assertEqual(first["status"], "submitted")
                row = download_requests_repo.get_download_request(first["request_id"])
                assert row is not None
                self.assertEqual(row["torrent_data"], payload)
                self.assertEqual(backend.call_args.args[0]["torrent_data"], payload)
                duplicate = self.client.post("/api/downloads/direct/submit", headers=headers,
                                             data={"target": "qb"}, files=upload)
                self.assertEqual(duplicate.json()["status"], "duplicate")
                for xt in hashes:
                    duplicate = self.client.post("/api/downloads/direct/submit", headers=headers,
                                                 data={"target": "qb", "url": "magnet:?xt=" + xt})
                    self.assertEqual(duplicate.status_code, 200, duplicate.text)
                    self.assertEqual(duplicate.json()["status"], "duplicate")
                    self.assertEqual(duplicate.json()["request_id"], first["request_id"])
                backend.assert_called_once()
                self.assertEqual(self._request_count(), count)

    def test_partial_or_unknown_submission_is_not_implicitly_retried(self):
        headers = self._headers()
        for status, receipt in (("partial", {"ok": False, "error": "解析失败"}),
                                ("manual_review", RuntimeError("回执丢失"))):
            upload = {"file": ("fixture.torrent", _torrent_bytes(status.encode()), "application/x-bittorrent")}
            with self.subTest(status=status), \
                 patch("app.indexers.downloads.download_target_readiness", return_value={"qb": True, "guangya": True}), \
                 patch("app.indexers.downloads.guangya_download_credential_generation", return_value=1), \
                 patch("app.modules.download_dispatcher._submit_qb", return_value={"ok": True}) as qb, \
                 patch("app.modules.download_dispatcher._submit_guangya") as gy:
                if isinstance(receipt, Exception):
                    gy.side_effect = receipt
                else:
                    gy.return_value = receipt
                first = self.client.post("/api/downloads/direct/submit", headers=headers,
                                         data={"target": "both"}, files=upload)
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(first.json()["status"], status)
                repeated = self.client.post("/api/downloads/direct/submit", headers=headers,
                                            data={"target": "guangya"}, files=upload)
                self.assertEqual(repeated.status_code, 200, repeated.text)
                self.assertEqual(repeated.json()["status"], "duplicate")
                self.assertEqual(repeated.json()["request_id"], first.json()["request_id"])
                qb.assert_called_once()
                gy.assert_called_once()

    def test_uploaded_streams_are_closed_after_success_or_rejection(self):
        from starlette.datastructures import UploadFile

        headers = self._headers()
        streams = []
        close = UploadFile.close

        async def record_close(upload):
            streams.append(upload.file)
            await close(upload)

        with patch.object(UploadFile, "close", record_close):
            for payload, expected_status in ((_torrent_bytes(), 200), (b"invalid", 400),
                                             (b"0" * (4 * 1024 * 1024 + 1), 400)):
                with self.subTest(size=len(payload)):
                    response = self.client.post(
                        "/api/downloads/direct/preview", headers=headers,
                        files={"file": ("fixture.torrent", payload, "application/x-bittorrent")},
                    )
                    self.assertEqual(response.status_code, expected_status, response.text)
                    self.assertTrue(streams[-1].closed)
        self.assertEqual(len(streams), 3)
        self.assertEqual(self._request_count(), 0)
