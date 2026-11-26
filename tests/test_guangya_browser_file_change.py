"""光鸭浏览器写入 API 与实际 Writer 的隔离回归测试。"""
from __future__ import annotations

import re
from dataclasses import replace
from threading import Event
from unittest.mock import Mock, call, patch

from fastapi.testclient import TestClient

from app.clients.guangya import (
    GuangYaClient,
    GuangYaFile,
    GuangYaReadRejected,
    GuangYaWriteRejected,
)
from app.config import web_credentials
from app.main import create_app
from app.modules.organize_tasks import OrganizeTaskManager
from app.repositories import download_requests
from tests.support import InitializedWebTestCase, isolated_test_database


class GuangYaBrowserFileChangeTests(InitializedWebTestCase):
    def setUp(self):
        self.enterContext(isolated_test_database("guangya-browser-file-change.db"))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("禁止外联")))
        self.enterContext(patch("app.clients.guangya.sleep"))
        self.files = {row.file_id: row for row in (
            GuangYaFile("source", "来件", True),
            GuangYaFile("target", "归档", True),
            GuangYaFile("movie", " 影片.mkv ", False, 128, "media-etag", "source"),
            GuangYaFile("folder", "Season 1", True, parent_id="source"),
            GuangYaFile("nested", "nested", True, parent_id="folder"),
            GuangYaFile("episode", "E01.mkv", False, 64, "episode-etag", "nested"),
        )}
        self.provider = Mock(spec=GuangYaClient, logged_in=True, credential_generation=1)
        self.read_generation = self.enterContext(patch(
            "app.routes.guangya_api.read_guangya_credential_generation", return_value=1,
        ))
        self.provider.file_info.side_effect = self.files.get
        self.provider.list_dir.side_effect = self._list_dir
        self.provider.create_dir.side_effect = self._mkdir
        self.provider.move.side_effect = self._move
        self.provider.copy.side_effect = self._copy
        self.provider.task_status.return_value = {"data": {"status": 2}}
        self.enterContext(patch("app.routes.guangya_api.GuangYaClient", return_value=self.provider))

        self.manager = OrganizeTaskManager()
        self.finished = Event()
        self.enterContext(patch.object(self.manager, "_wake_download_tracker", self.finished.set))
        self.enterContext(patch("app.modules.organize_tasks.get_organize_manager", return_value=self.manager))

        self.client = TestClient(create_app(start_background=False))
        self.addCleanup(self.client.close)
        self.addCleanup(self.manager.shutdown, timeout=5)
        username, password = web_credentials()
        login = self.client.post(
            "/login", follow_redirects=False,
            data={"username": username, "password": password, "csrf_token": self._csrf("/login")},
        )
        self.assertEqual(login.status_code, 302)
        self.headers = {"X-CSRF-Token": self._csrf("/guangya")}

    def _csrf(self, path):
        match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', self.client.get(path).text)
        self.assertIsNotNone(match)
        assert match is not None
        return match.group(1)

    def _list_dir(self, parent_id):
        return [row for row in self.files.values() if row.parent_id == parent_id]

    def _mkdir(self, name, parent_id):
        self.files["created"] = GuangYaFile("created", name, True, parent_id=parent_id)
        return "created"

    def _move(self, file_ids, parent_id):
        file_id, = file_ids
        self.files[file_id] = replace(self.files[file_id], parent_id=parent_id)
        return True

    def _copy(self, file_ids, parent_id):
        file_id, = file_ids
        copied_id = f"copy-{file_id}"
        self.files[copied_id] = replace(self.files[file_id], file_id=copied_id, parent_id=parent_id)
        return "provider-copy-task"

    def _payload(self, operation, file_id="movie", target_dir_id="target", **fields):
        item = self.files[file_id]
        return {
            "operation": operation, "file_id": file_id, "target_dir_id": target_dir_id,
            "expected_parent_id": item.parent_id, "expected_name": item.name, **fields,
        }

    def _post(self, payload):
        return self.client.post("/api/guangya/file-change", json=payload, headers=self.headers)

    def _receipt(self, task_id):
        response = self.client.get("/api/guangya/organize/status", params={"task_ids": task_id})
        self.assertEqual(response.status_code, 200)
        task, = response.json()["tasks"]
        self.assertEqual(task["id"], task_id)
        return task

    def _run(self, payload):
        self.finished.clear()
        response = self._post(payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(self.finished.wait(3), "Writer 未结束")
        return self._receipt(response.json()["task_id"])

    def _assert_no_writes(self):
        self.provider.copy.assert_not_called()
        self.provider.move.assert_not_called()
        self.provider.create_dir.assert_not_called()

    def test_validates_operation_fields_and_root_source_before_writer(self):
        for payload in (
            {"operation": ["copy"], "target_dir_id": "target"},
            self._payload("copy", target_dir_id=""),
            {**self._payload("move"), "file_id": "0"},
            self._payload("move", expected_parent_id=None),
            self._payload("copy", expected_name=""),
            self._payload("copy", "folder", "folder"),
            *({"operation": "mkdir", "target_dir_id": "0", "name": name}
              for name in ("", "..", "path/sub", "path\\sub", "a\0b")),
        ):
            with self.subTest(payload=payload):
                response = self._post(payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertTrue(response.json()["error"])
        self.provider.file_info.assert_not_called()
        self._assert_no_writes()

    def test_copy_move_and_mkdir_accept_account_root(self):
        operations = (
            (self._payload("copy", target_dir_id="0"), "copied", "copy-movie"),
            (self._payload("move", "folder", "0"), "moved", "folder"),
            ({"operation": "mkdir", "target_dir_id": "0", "name": " 新目录 "}, "created", "created"),
        )
        for payload, stat, file_id in operations:
            with self.subTest(operation=payload["operation"]):
                task = self._run(payload)
                self.assertEqual(task["status"], "completed", task)
                self.assertEqual(task["result"], {
                    "ok": True, "operation": payload["operation"], "target_dir_id": "0",
                    "name": self.files[file_id].name, "stats": {stat: 1},
                })
                self.assertEqual(self.files[file_id].parent_id, "0")
        self.provider.copy.assert_called_once_with(["movie"], "0")
        self.provider.move.assert_called_once_with(["folder"], "0")
        self.provider.create_dir.assert_called_once_with("新目录", "0")
        self.assertNotIn(call("0"), self.provider.file_info.call_args_list)

    def test_account_switch_after_mkdir_admission_never_writes_to_replacement_root(self):
        payload = {"operation": "mkdir", "target_dir_id": "0", "name": "原账号目录"}
        replacement = Mock(spec=GuangYaClient, logged_in=True, credential_generation=2)
        replacement_root = []
        replacement.list_dir.side_effect = lambda _parent: replacement_root.copy()

        def create_on_replacement(name, parent_id):
            replacement_root.append(GuangYaFile("new-folder", name, True, parent_id=parent_id))
            return "new-folder"

        replacement.create_dir.side_effect = create_on_replacement
        current = self.provider
        entered, release = Event(), Event()
        run_operation = self.manager._run_operation

        def wait_before_execution(*args):
            entered.set()
            if release.wait(5):
                run_operation(*args)

        with (
            patch("app.routes.guangya_api.GuangYaClient", side_effect=lambda: current),
            patch.object(self.manager, "_run_operation", side_effect=wait_before_execution),
        ):
            try:
                response = self._post(payload)
                self.assertEqual(response.status_code, 202, response.text)
                self.assertTrue(entered.wait(3))
                current = replacement
                self.read_generation.return_value = 2
                self.provider.logged_in = False
                competing = self._post(payload)
            finally:
                release.set()
            self.assertTrue(self.finished.wait(3))

            replacement.create_dir.assert_not_called()
            self._assert_no_writes()
            receipt = self._receipt(response.json()["task_id"])
            self.assertEqual(receipt["status"], "failed")
            self.assertIn("光鸭登录已变化", receipt["error"])
            self.assertEqual(competing.status_code, 409, competing.text)

            retry = self._run(payload)
            self.assertEqual(retry["status"], "completed", retry)
            replacement.create_dir.assert_called_once_with(payload["name"], "0")

    def test_same_parent_move_is_unchanged_and_copy_requires_another_target(self):
        task = self._run(self._payload("move", "target", "0"))
        self.assertEqual(task["status"], "completed", task)
        self.assertEqual(task["result"]["stats"], {"moved": 0, "unchanged": 1})
        task = self._run(self._payload("copy", "target", "0"))
        self.assertEqual(task["status"], "failed", task)
        self.assertIn("与当前目录相同", task["error"])
        self._assert_no_writes()

    def test_conflicts_fail_in_writer_before_any_cloud_write(self):
        self.files["cycle"] = GuangYaFile("cycle", "loop", True, parent_id="cycle")
        self.files["duplicate"] = GuangYaFile("duplicate", "season 1", False, parent_id="target")
        for payload, error in (
            (self._payload("move", "folder", "nested"), "子目录"),
            (self._payload("copy", "folder", "nested"), "子目录"),
            (self._payload("copy", "folder", "cycle"), "父链"),
            (self._payload("move", "folder", "missing"), "目标目录"),
            (self._payload("copy", "folder", "movie"), "不是目录"),
            (self._payload("copy", "folder", expected_name="旧名称"), "名称已变化"),
            (self._payload("move", "folder", expected_parent_id="old-parent"), "所在目录已变化"),
            (self._payload("copy", "folder"), "同名"),
            (self._payload("move", "folder"), "同名"),
            ({"operation": "mkdir", "target_dir_id": "target", "name": "Season 1"}, "同名"),
        ):
            with self.subTest(payload=payload):
                task = self._run(payload)
                self.assertEqual(task["status"], "failed", task)
                self.assertIn(error, task["error"])
        self._assert_no_writes()

    def test_deleted_source_reports_gone(self):
        self.provider.file_info.side_effect = GuangYaReadRejected("file_info", code="149")
        task = self._run(self._payload("move"))
        self.assertEqual(task["status"], "failed", task)
        self.assertIn("源项目已不存在", task["error"])
        self._assert_no_writes()

    def test_directory_copy_verifies_target_entry_and_terminal_task_without_subtree_reads(self):
        payload = self._payload("copy", "folder")
        self.provider.task_status.side_effect = [{"data": {"status": n}} for n in (1, 2)]
        task = self._run(payload)
        self.assertEqual(task["status"], "completed", task)
        self.assertEqual(task["result"]["stats"], {"copied": 1})
        self.assertEqual(self.files["copy-folder"].parent_id, "target")
        self.assertEqual(self.provider.task_status.call_args_list, [call("provider-copy-task")] * 2)
        self.assertEqual(self.provider.list_dir.call_args_list, [call("target"), call("target")])

        repeated = self._run(payload)
        self.assertEqual(repeated["status"], "failed", repeated)
        self.assertIn("同名", repeated["error"])
        self.provider.copy.assert_called_once_with(["folder"], "target")

    def test_running_copy_replays_same_receipt_and_holds_writer(self):
        started, release = Event(), Event()

        def task_status(_task_id):
            started.set()
            self.assertTrue(release.wait(5), "测试未释放云端状态回读")
            return {"data": {"status": 2}}

        self.provider.task_status.side_effect = task_status
        payload = self._payload("copy", "folder")
        first = self._post(payload)
        self.assertEqual(first.status_code, 202, first.text)
        try:
            self.assertTrue(started.wait(3))
            task_id = first.json()["task_id"]
            self.assertEqual(self._receipt(task_id)["status"], "running")
            replay = self._post(payload)
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertEqual(replay.json()["task_id"], task_id)
            self.assertTrue(replay.json()["replayed"])
            competing = self._post({"operation": "mkdir", "target_dir_id": "0", "name": "竞争任务"})
            self.assertEqual(competing.status_code, 409, competing.text)
        finally:
            release.set()
        self.assertTrue(self.finished.wait(3))
        self.assertEqual(self._receipt(task_id)["status"], "completed")
        self.provider.copy.assert_called_once_with(["folder"], "target")
        self.provider.create_dir.assert_not_called()

    def test_accepted_copy_still_running_gets_review_receipt(self):
        self.provider.task_status.return_value = {"data": {"status": 1}}
        task = self._run(self._payload("copy", "folder"))
        self.assertEqual(task["status"], "manual_review", task)
        self.assertIn("已受理复制", task["error"])
        self.assertIn("仍在处理中", task["error"])
        self.provider.copy.assert_called_once_with(["folder"], "target")

    def test_copy_with_terminal_task_requires_matching_target_entry(self):
        self.provider.copy.side_effect = None
        self.provider.copy.return_value = "provider-copy-task"
        task = self._run(self._payload("copy", "folder"))
        self.assertEqual(task["status"], "manual_review", task)
        self.assertIn("暂未确认", task["error"])
        self.provider.copy.assert_called_once_with(["folder"], "target")

    def test_empty_file_copy_requires_a_zero_byte_target(self):
        source = replace(self.files["movie"], size=0, etag="")
        self.files["movie"] = source
        for size, expected in ((8192, "manual_review"), (0, "completed")):
            with self.subTest(size=size):
                self.files.pop("new-copy", None)
                self.provider.copy.reset_mock()

                def copy(file_ids, parent_id):
                    self.files["new-copy"] = replace(source, file_id="new-copy", parent_id=parent_id, size=size)
                    return ""  # 旧版接口没有任务 ID，只能从目标目录核验。

                self.provider.copy.side_effect = copy
                task = self._run(self._payload("copy"))
                self.assertEqual(task["status"], expected, task)
                self.provider.copy.assert_called_once_with(["movie"], "target")

    def test_directory_entry_alone_does_not_prove_copy_completed(self):
        self.provider.task_status.return_value = {"data": {"status": 3}}
        task = self._run(self._payload("copy", "folder"))
        self.assertIn("copy-folder", self.files)
        self.assertEqual(task["status"], "failed", task)
        self.assertIn("复制任务失败", task["error"])

        self.files.pop("copy-folder")
        self.provider.task_status.reset_mock()

        def copy_without_task_id(file_ids, parent_id):
            self._copy(file_ids, parent_id)
            return ""

        self.provider.copy.side_effect = copy_without_task_id
        task = self._run(self._payload("copy", "folder"))
        self.assertIn("copy-folder", self.files)
        self.assertEqual(task["status"], "manual_review", task)
        self.provider.task_status.assert_not_called()

    def test_provider_rejection_and_lost_response_have_distinct_safe_receipts(self):
        self.provider.move.side_effect = GuangYaWriteRejected("move", message="opaque-provider-secret")
        task = self._run(self._payload("move"))
        self.assertEqual(task["status"], "failed", task)
        self.assertIn("拒绝了写入", task["error"])
        self.assertNotIn("opaque-provider-secret", str(task))
        self.provider.move.assert_called_once_with(["movie"], "target")

        self.provider.create_dir.side_effect = TimeoutError("opaque-provider-secret")
        task = self._run({"operation": "mkdir", "target_dir_id": "0", "name": "目录"})
        self.assertEqual(task["status"], "manual_review", task)
        self.assertIn("核对目标目录", task["error"])
        self.assertNotIn("opaque-provider-secret", str(task))
        self.provider.create_dir.assert_called_once_with("目录", "0")

    def test_move_respects_persisted_active_download_source_protection(self):
        request_id, created = download_requests.create_download_request("browser-move-active", "magnet")
        self.assertTrue(created)
        download_requests.update_download_request(
            request_id, targets="guangya", status="submitted", gy_status="downloading", gy_isolated=1,
            gy_target_dir="folder", gy_staging_parent_dir="source", gy_staging_name="Season 1",
        )
        for file_id in ("episode", "folder", "source"):
            with self.subTest(file_id=file_id):
                task = self._run(self._payload("move", file_id))
                self.assertEqual(task["status"], "failed", task)
                self.assertIn(f"下载请求 #{request_id}", task["error"])
                self.assertIn("再移动", task["error"])
        self.provider.move.assert_not_called()

        task = self._run(self._payload("move", "movie"))
        self.assertEqual(task["status"], "completed", task)
        self.provider.move.assert_called_once_with(["movie"], "target")
