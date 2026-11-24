"""下载页 qBittorrent 批量任务操作契约。"""
from __future__ import annotations

import base64
import hashlib
import re
import socket
import sqlite3
from contextlib import contextmanager
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import config
from app.repositories import download_requests as download_requests_repo
from app.config import web_credentials
from app.main import create_app
from app.modules.qb_control import QBControlConflict
from tests.support import InitializedWebTestCase, isolated_test_database, preserve_web_auth_config


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
            side_effect=preserve_web_auth_config(lambda key, default="": {
                "QB_URL": "http://192.168.1.10:8080",
                "QB_API_KEY": "saved-secret",
            }.get(key, default)),
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

    def test_qb_probe_resolves_the_target_once_and_uses_saved_credentials_at_the_same_url(self):
        headers = self._headers()
        url = "http://qb.example.invalid:8080"
        config.set_and_save({
            "QB_URL": url + "/", "QB_API_KEY": "fixture-key", "QB_PASSWORD": "fixture-password",
        })
        answer = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 8080))
        with patch("socket.getaddrinfo", return_value=[answer]) as resolve, patch(
            "app.routes.downloads_api.QBittorrentClient",
        ) as client:
            client.return_value.test_connection.return_value = {"app": "5", "webapi": "2", "auth_mode": "api_key"}
            response = self.client.post("/api/downloads/qb/test", headers=headers, json={
                "url": url, "username": "user", "password": "********", "api_key": "********",
            })
        self.assertEqual(response.status_code, 200, response.text)
        client.assert_called_once_with(
            url=url, username="user", password="fixture-password", api_key="fixture-key", timeout=8,
        )
        resolve.assert_called_once_with("qb.example.invalid", 8080, type=socket.SOCK_STREAM)

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
        self.assertNotIn("code", response.json())

    def test_submit_reports_known_preflight_rejection_without_creating_a_request(self):
        from app.indexers import downloads

        headers = self._headers()
        url = "magnet:?xt=urn:btih:" + "c" * 40
        with isolated_test_database(), patch.object(downloads, "download_target_readiness", side_effect=[
            {"qb": True, "guangya": False}, {"qb": False},
        ]), patch("app.modules.download_dispatcher._submit_qb") as backend:
            preview = self.client.post("/api/downloads/direct/preview", headers=headers, data={"url": url})
            self.assertEqual(preview.status_code, 200, preview.text)
            response = self.client.post("/api/downloads/direct/submit", headers=headers, data={"url": url, "target": "qb"})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["code"], "download_not_submitted")
            self.assertIn("尚未配置", response.json()["error"])
            self.assertEqual(self._request_count(), 0)
            backend.assert_not_called()

    def test_preview_lists_the_same_targets_as_the_telegram_picker(self):
        headers = self._headers()
        magnet = "magnet:?xt=urn:btih:" + "d" * 40
        sources = [{"id": "adult-1", "name": "成人 A"}, {"id": "adult-2", "name": "成人 A"}]
        from app.indexers.downloads import download_target_labels

        labels = download_target_labels()
        with patch("app.indexers.downloads.download_target_readiness", return_value={"qb": True, "guangya": False}), \
             patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=sources):
            targets = self.client.post("/api/downloads/direct/preview", headers=headers, data={"url": magnet}).json()["targets"]
        self.assertEqual([(t["label"], t["target"], t.get("nsfw_source_id"), t["ready"]) for t in targets], [
            (labels["guangya"], "guangya", None, False),
            ("qBittorrent", "qb", None, True),
            ("光鸭·NSFW · 成人 A #1", "guangya", "adult-1", False),
            ("光鸭·NSFW · 成人 A #2", "guangya", "adult-2", False),
            (labels["both"], "both", None, False),
        ])
        # 未配置 NSFW 来源时与 TG 一样显示占位，但 Web 不可选。
        with patch("app.indexers.downloads.download_target_readiness", return_value={"qb": True, "guangya": True}), \
             patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=[]):
            targets = self.client.post("/api/downloads/direct/preview", headers=headers, data={"url": magnet}).json()["targets"]
        self.assertIn({"target": "guangya", "nsfw_source_id": "", "label": "光鸭·NSFW", "ready": False}, targets)
        self.assertEqual(self._request_count(), 0)

    def test_cloud_submit_requires_fresh_confirmation_when_the_route_changes(self):
        headers = self._headers()
        url = "magnet:?xt=urn:btih:" + "1" * 40 + "&dn=fixture-movie"
        rules = {
            "OFFLINE_TARGET_DIR": "primary", "OFFLINE_TARGET_DIR_NAME": "主目录",
            "OFFLINE_SECONDARY_ENABLED": "1", "OFFLINE_SECONDARY_KEYWORDS": "fixture-movie",
            "OFFLINE_SECONDARY_DIR": "secondary", "OFFLINE_SECONDARY_DIR_NAME": "分流目录",
        }
        with (
            patch.dict("os.environ", rules),
            patch("app.indexers.downloads.download_target_readiness", return_value={"qb": True, "guangya": True}),
            patch("app.indexers.downloads.guangya_download_credential_generation", return_value=1),
            patch("app.modules.download_dispatcher._submit_qb", return_value={"ok": True}),
            patch("app.modules.download_dispatcher._submit_guangya", return_value={"ok": True}) as cloud,
        ):
            preview = self.client.post("/api/downloads/direct/preview", headers=headers, data={"url": url})
            options = {item["target"]: item for item in preview.json()["targets"] if "nsfw_source_id" not in item}
            self.assertEqual(options["guangya"]["label"], "光鸭·分流目录")
            self.assertEqual(options["both"]["label"], "光鸭·分流目录 + qBittorrent")
            data = {"url": url, "target": "both", "expected_gy_target_dir": options["both"]["gy_target_dir"]}
            with patch.dict("os.environ", {"OFFLINE_SECONDARY_ENABLED": "0"}):
                stale = self.client.post("/api/downloads/direct/submit", headers=headers, data=data)
                self.assertEqual(stale.status_code, 400, stale.text)
                self.assertEqual(stale.json()["code"], "download_not_submitted")
                self.assertEqual(self._request_count(), 0)
                cloud.assert_not_called()
                updated = next(item for item in stale.json()["targets"] if item["target"] == "both")
                self.assertEqual(updated["label"], "光鸭·主目录 + qBittorrent")
                fresh = self.client.post("/api/downloads/direct/submit", headers=headers, data={
                    **data, "expected_gy_target_dir": updated["gy_target_dir"],
                })
                self.assertEqual(fresh.json()["status"], "submitted")
                cloud.assert_called_once()
                self.assertEqual(cloud.call_args.kwargs["target_dir_id"], "primary")

    def test_submit_passes_the_nsfw_source_only_when_chosen(self):
        headers = self._headers()
        url = "magnet:?xt=urn:btih:" + "e" * 40
        with patch("app.indexers.downloads.submit_direct_download", return_value={"request_id": 8, "status": "submitted"}) as submit:
            self.client.post("/api/downloads/direct/submit", headers=headers, data={"url": url, "target": "guangya", "nsfw_source_id": "adult-1"})
            self.client.post("/api/downloads/direct/submit", headers=headers, data={"url": url, "target": "guangya"})
        self.assertEqual([call.kwargs["nsfw_source_id"] for call in submit.call_args_list], ["adult-1", None])

    def test_nsfw_destination_is_checked_before_creating_a_request(self):
        from app.indexers import downloads

        url = "magnet:?xt=urn:btih:" + "f" * 40
        sources = [{"id": "adult-1", "name": "NewNsfw"}]
        with isolated_test_database(), \
             patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=sources), \
             patch.object(downloads, "download_target_readiness", return_value={"guangya": True}), \
             patch.object(downloads, "guangya_download_credential_generation", return_value=1), \
             patch.object(downloads, "dispatch_request", return_value={"status": "submitted", "succeeded": ["guangya"]}) as dispatch:
            for target, source_id, message in (("guangya", "removed", "NSFW 来源已变更"), ("both", "adult-1", "NSFW 下载目标无效"),
                                               ("guangya", "", "未配置 NSFW 整理来源")):
                with self.subTest(target=target, source_id=source_id), self.assertRaisesRegex(downloads.DirectDownloadPreconditionError, message):
                    downloads.submit_direct_download(downloads.direct_url_input(url), target, origin="web", nsfw_source_id=source_id)
            self.assertEqual(self._request_count(), 0)
            dispatch.assert_not_called()
            downloads.submit_direct_download(downloads.direct_url_input(url), "guangya", origin="web", nsfw_source_id="adult-1")
        kwargs = dispatch.call_args.kwargs
        self.assertEqual((kwargs["gy_target_dir"], kwargs["gy_target_name"]), ("adult-1", "NewNsfw"))

    def test_empty_nsfw_form_field_never_falls_back_to_the_default_directory(self):
        headers = self._headers()
        url = "magnet:?xt=urn:btih:" + "9" * 40
        cases = (
            ({"url": url, "target": "guangya", "nsfw_source_id": ""}, None),
            ({"url": url, "target": "guangya", "nsfw_source_id": "  "}, None),
            ({"target": "guangya", "nsfw_source_id": ""},
             {"file": ("sample.torrent", _torrent_bytes(), "application/x-bittorrent")}),
        )
        with patch("app.indexers.downloads.download_target_readiness", return_value={"guangya": True}), \
             patch("app.indexers.downloads.guangya_download_credential_generation", return_value=1), \
             patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=[]), \
             patch("app.indexers.downloads.dispatch_request", return_value={"succeeded": ["guangya"]}) as dispatch:
            for data, files in cases:
                with self.subTest(data=data, multipart=files is not None):
                    response = self.client.post("/api/downloads/direct/submit", headers=headers, data=data, files=files)
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.json()["code"], "download_not_submitted")
                    self.assertEqual(self._request_count(), 0)
            dispatch.assert_not_called()

    def test_nsfw_submission_keeps_canonical_directory_and_history_on_duplicates(self):
        headers = self._headers()
        url = "magnet:?xt=urn:btih:" + "8" * 40
        values = {
            "GY_ORGANIZE_NSFW_ENABLED": "true",
            "GY_ORGANIZE_SOURCE_DIRS": '["ordinary",{"id":"adult-a","name":"正式来源 A"},{"id":"adult-b","name":"正式来源 B"}]',
            "GY_ORGANIZE_NSFW_SOURCE_IDS": '["adult-a","adult-b"]',
        }

        def submit_offline(_url, **kwargs):
            self.assertEqual((kwargs["target_dir_id"], kwargs["target_dir_name"]), ("adult-a", "正式来源 A"))
            staging = {"id": "stage-a", "parent_id": "adult-a", "parent_name": "正式来源 A",
                       "name": "MF-task", "isolated": True}
            kwargs["on_staging_created"](staging)
            return {"ok": True, "task_ids": ["cloud-task"], "staging": staging,
                    "decision": {"target_dir_id": "stage-a", "target_dir_name": "正式来源 A / MF-task"}}

        with patch.dict("os.environ", values), \
             patch("app.indexers.downloads.download_target_readiness", return_value={"guangya": True}), \
             patch("app.indexers.downloads.guangya_download_credential_generation", return_value=1), \
             patch("app.modules.download_dispatcher.GuangYaClient") as client, \
             patch("app.modules.download_dispatcher._recover_guangya_magnet_torrent", return_value=None), \
             patch("app.modules.download_dispatcher.submit_offline", side_effect=submit_offline) as offline:
            client.return_value.logged_in = True
            client.return_value.credential_generation = 1
            first = self.client.post("/api/downloads/direct/submit", headers=headers, data={
                "url": url, "target": "guangya", "nsfw_source_id": "adult-a",
                "gy_target_dir": "forged", "gy_target_name": "伪造名称",
            })
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["status"], "submitted")
            request_id = first.json()["request_id"]
            row = download_requests_repo.get_download_request(request_id)
            assert row is not None
            original = dict(row)
            self.assertEqual((original["gy_requested_target_dir"], original["gy_requested_nsfw"],
                              original["gy_staging_parent_dir"], original["gy_target_dir"]),
                             ("adult-a", 1, "adult-a", "stage-a"))
            for source in (None, "adult-b"):
                with self.subTest(duplicate_source=source):
                    data = {"url": url, "target": "guangya"}
                    if source is not None:
                        data["nsfw_source_id"] = source
                    duplicate = self.client.post("/api/downloads/direct/submit", headers=headers, data=data)
                    self.assertEqual(duplicate.status_code, 200, duplicate.text)
                    self.assertEqual((duplicate.json()["status"], duplicate.json()["request_id"]), ("duplicate", request_id))
                    current = download_requests_repo.get_download_request(request_id)
                    assert current is not None
                    self.assertEqual(dict(current), original)
            for change in ({"GY_ORGANIZE_NSFW_ENABLED": "false"}, {"GY_ORGANIZE_SOURCE_DIRS": '[]'},
                           {"GY_ORGANIZE_NSFW_SOURCE_IDS": '["adult-b"]'}):
                with self.subTest(change=change), patch.dict("os.environ", change):
                    stale = self.client.post("/api/downloads/direct/submit", headers=headers, data={
                        "url": "magnet:?xt=urn:btih:" + "7" * 40, "target": "guangya", "nsfw_source_id": "adult-a",
                    })
                    self.assertEqual(stale.status_code, 400, stale.text)
                    self.assertEqual(stale.json()["code"], "download_not_submitted")
            offline.assert_called_once()
            self.assertEqual(self._request_count(), 1)

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
