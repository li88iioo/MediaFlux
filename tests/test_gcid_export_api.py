"""GCID 后台导出的会话归属、任务进度与下载确认契约。"""
from __future__ import annotations

import json
import os
from threading import Event
from time import monotonic, sleep
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.clients.guangya import GuangYaClient, GuangYaFile
from app.main import create_app
from app.modules import gcid_import
from app.modules.gcid_manifest import MAX_GCID_REQUEST_BYTES, export_manifest
from app.routes import tools_api
from tests.support import InitializedWebTestCase
from tests.test_config_empty_values_api import login

EXPORT = "/api/tools/gcid/export"
SOURCE = {"source_dir_id": "source-1", "source_name": "剧集"}
OTHER_SOURCE = {"source_dir_id": "source-2", "source_name": "电影"}
MD5 = "a" * 32


class GCIDExportAPITests(InitializedWebTestCase):
    @classmethod
    def setUpClass(cls):
        cls.enterClassContext(patch.dict(os.environ, {
            "ENV_WEB_PASSPORT": "export-test-user",
            "ENV_WEB_PASSWORD": "export-test-password",
            "WEB_SECRET_KEY": "gcid-export-isolated-test-session",
        }))
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.create_connection", "socket.getaddrinfo"):
            cls.enterClassContext(patch(target, side_effect=AssertionError("导出测试禁止外部网络")))
        super().setUpClass()

    def setUp(self):
        factory = self.enterContext(patch.object(tools_api, "GuangYaClient", spec=GuangYaClient))
        self.cloud = factory.return_value
        self.cloud.logged_in = True
        self.cloud.list_dir.side_effect = {
            "source-1": [GuangYaFile("season", "Season", True, parent_id="source-1")],
            "season": [GuangYaFile("file-1", "Episode.mkv", False, 42, "gcid-episode", "season", md5=MD5)],
        }.__getitem__
        self.cloud.generate_gcid_json.side_effect = self._generate
        self.client = self.enterContext(TestClient(create_app(), raise_server_exceptions=False))

    def _generate(self, source_id, source_name, *, on_progress):
        return export_manifest(self.cloud, source_id, source_name, on_progress=on_progress)

    def _start(self, headers):
        response = self.client.post(EXPORT, json=SOURCE, headers=headers)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["task"]

    def _task(self):
        response = self.client.get(EXPORT)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["task"]

    def _wait_for_terminal(self, status):
        deadline = monotonic() + 5
        task = self._task()
        while task is not None and task["status"] == "running" and monotonic() < deadline:
            sleep(0.01)
            task = self._task()
        assert task is not None
        self.assertEqual(task["status"], status, task)
        return task

    def test_export_requires_web_login_csrf_and_cloud_login_for_start(self):
        for method, path in (
            ("GET", EXPORT),
            ("POST", EXPORT),
            ("GET", EXPORT + "/unavailable/download"),
            ("DELETE", EXPORT + "/unavailable"),
        ):
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, json=SOURCE if method == "POST" else None)
                self.assertEqual(response.status_code, 401, response.text)

        headers = login(self.client)
        denied = self.client.post(EXPORT, json=SOURCE)
        self.assertEqual(denied.status_code, 403, denied.text)
        self.cloud.logged_in = False
        offline = self.client.post(EXPORT, json=SOURCE, headers=headers)
        self.assertEqual(offline.status_code, 503, offline.text)
        self.assertIn("登录", offline.json()["error"])
        self.assertIsNone(self._task())
        self.cloud.generate_gcid_json.assert_not_called()

    def test_export_replays_one_task_until_owner_acknowledges_valid_download(self):
        headers = login(self.client)
        other_session = TestClient(self.client.app, raise_server_exceptions=False)
        self.addCleanup(other_session.close)
        other_headers = login(other_session)
        self.assertNotEqual(headers, other_headers)
        self.assertIsNone(self._task())

        entered = Event()
        release = Event()

        def blocking_export(source_id, source_name, *, on_progress):
            manifest = self._generate(source_id, source_name, on_progress=on_progress)
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test did not release export worker")
            return manifest

        self.cloud.generate_gcid_json.side_effect = blocking_export
        try:
            task_id = self._start(headers)["id"]
            task_path = f"{EXPORT}/{task_id}"
            download = task_path + "/download"
            self.assertTrue(entered.wait(5), "export worker did not report progress")
            running = self._task()
            self.assertEqual(
                {key: running[key] for key in ("id", "status", "directory_count", "file_count",
                                               "source_dir_id", "source_name")},
                {**SOURCE, "id": task_id, "status": "running", "directory_count": 2, "file_count": 1},
            )
            self.assertFalse(running["error"])
            pending = self.client.get(download)
            self.assertEqual(pending.status_code, 409, pending.text)
            self.assertEqual(self._start(headers)["id"], task_id)
            conflict = self.client.post(EXPORT, json=OTHER_SOURCE, headers=headers)
            self.assertEqual(conflict.status_code, 409, conflict.text)

            other_status = other_session.get(EXPORT)
            self.assertEqual(other_status.status_code, 200, other_status.text)
            self.assertIsNone(other_status.json()["task"])
        finally:
            release.set()

        ready = self._wait_for_terminal("ready")
        self.assertEqual(ready["id"], task_id)
        self.assertEqual((ready["directory_count"], ready["file_count"]), (2, 1))
        self.cloud.logged_in = False
        self.assertEqual(self._start(headers)["id"], task_id)
        conflict = self.client.post(EXPORT, json=OTHER_SOURCE, headers=headers)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        foreign_download = other_session.get(download)
        self.assertEqual(foreign_download.status_code, 404, foreign_download.text)
        foreign_ack = other_session.delete(task_path, headers=other_headers)
        self.assertEqual(foreign_ack.status_code, 200, foreign_ack.text)
        self.assertEqual(self._task(), ready)
        denied = self.client.delete(task_path)
        self.assertEqual(denied.status_code, 403, denied.text)

        first = self.client.get(download)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.headers["content-type"].startswith("application/json"))
        self.assertIn("attachment", first.headers["content-disposition"])
        self.assertIn(".json", first.headers["content-disposition"])
        second = self.client.get(download)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.content, first.content)
        manifest = first.json()
        self.assertEqual(manifest["files"], [
            {"path": "Season/Episode.mkv", "size": 42, "gcid": "gcid-episode", "md5": MD5},
        ])
        self.assertEqual(manifest["source"]["directory_id"], SOURCE["source_dir_id"])
        validation = self.client.post("/api/tools/gcid/validate", json=manifest, headers=headers)
        self.assertEqual(validation.status_code, 200, validation.text)
        self.assertIs(validation.json()["valid"], True)

        received = self.client.delete(task_path, headers=headers)
        self.assertEqual(received.status_code, 200, received.text)
        self.assertIsNone(self._task())
        self.assertEqual(self.client.get(download).status_code, 404)
        repeated_ack = self.client.delete(task_path, headers=headers)
        self.assertEqual(repeated_ack.status_code, 200, repeated_ack.text)
        self.cloud.generate_gcid_json.assert_called_once()

    def test_large_export_and_legacy_indented_manifest_can_be_imported_directly(self):
        remote = {
            parent: [GuangYaFile(f"dir-{i}", "目录" * 35 + str(i), True, parent_id=parent)]
            for i, parent in enumerate(["source-1", *[f"dir-{j}" for j in range(9)]])
        }
        remote["dir-9"] = [
            GuangYaFile(str(i), f"{i:05d}.mkv", False, 42, "gcid-episode", "dir-9", md5=MD5)
            for i in range(10_000)
        ]
        self.cloud.list_dir.side_effect = remote.__getitem__
        self.cloud.share_account_key.return_value = "a" * 64
        headers = {**login(self.client), "Content-Type": "application/json"}

        task = self._start(headers)
        self._wait_for_terminal("ready")
        downloaded = self.client.get(f"{EXPORT}/{task['id']}/download")
        self.assertEqual(downloaded.status_code, 200)
        self.assertGreater(len(downloaded.content), 20 * 1024 * 1024)
        manifest = downloaded.json()
        legacy = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        self.assertLess(len(downloaded.content), len(legacy))

        with patch.object(gcid_import, "GuangYaClient", return_value=self.cloud):
            for content in (downloaded.content, legacy):
                with self.subTest(byte_count=len(content)):
                    wrapped = b'{"manifest":' + content + b',"target_dir_id":"0"}'
                    self.assertLess(len(wrapped), MAX_GCID_REQUEST_BYTES)
                    preview = self.client.post("/api/tools/gcid/import/preview", content=wrapped, headers=headers)
                    self.assertEqual(preview.status_code, 200, preview.text)
                    result = preview.json()
                    self.assertEqual(result["manifest_digest"], manifest["integrity"]["digest"])
                    self.assertEqual(result["file_count"], 10_000)
                    self.assertTrue(result["tree_truncated"])
                    children = result["tree"]
                    for _ in range(10):
                        directory, = children
                        self.assertEqual(directory["file_count"], 10_000)
                        children = directory["children"]
                    self.assertEqual(len(children), 590)

    def test_failed_worker_can_be_retried_by_post(self):
        headers = login(self.client)
        self.cloud.generate_gcid_json.side_effect = RuntimeError("isolated export failure")
        first_id = self._start(headers)["id"]
        failed = self._wait_for_terminal("failed")
        self.assertEqual(failed["id"], first_id)
        self.assertTrue(failed["error"])

        self.cloud.generate_gcid_json.side_effect = self._generate
        retried_id = self._start(headers)["id"]
        ready = self._wait_for_terminal("ready")
        self.assertEqual(ready["id"], retried_id)
        self.assertEqual(ready["file_count"], 1)
        self.assertEqual(self.cloud.generate_gcid_json.call_count, 2)
        received = self.client.delete(f"{EXPORT}/{retried_id}", headers=headers)
        self.assertEqual(received.status_code, 200, received.text)
