from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.support import InitializedWebTestCase

from app import database as db
from app import notifier
from app.main import create_app
from app.modules import gcid_import
from app.clients.guangya import GuangYaFile
from app.modules.gcid_manifest import FORMAT_NAME, export_manifest
from app.modules.telegram_notification_center import NotificationPublishResult


TEST_ACCOUNT_KEY = "a" * 64
TEST_FILE_MD5 = "a" * 32


def _digest(payload: dict) -> str:
    canonical = {key: value for key, value in payload.items() if key != "integrity"}
    raw = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _manifest(files: list[dict] | None = None, **overrides) -> dict:
    entries: list[dict] = files if files is not None else [
        {"path": "Movies/A.mkv", "size": 10, "gcid": "gcid-a"},
        {"path": "Movies/B.mkv", "size": 20, "gcid": "gcid-b", "md5": TEST_FILE_MD5},
    ]
    payload = {
        "format": FORMAT_NAME,
        "version": 2,
        "generated_at": "2026-07-26T00:00:00+00:00",
        "source": {"provider": "guangya", "directory_name": "测试源"},
        "file_count": len(entries),
        "total_size": sum(item["size"] for item in entries),
        "files": entries,
    }
    payload.update(overrides)
    payload["integrity"] = {"algorithm": "sha256", "digest": _digest(payload)}
    return payload


@dataclass
class FakeOutcome:
    success: bool
    remote_file_id: str = ""
    error_code: str = ""
    private_response: str = "PRIVATE-RESPONSE-MUST-NOT-LEAK"


class FakeImporter:
    def __init__(self, outcomes: dict[str, list[FakeOutcome]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[dict] = []

    def import_file(self, *, target_dir_id: str, path: str, size: int, gcid: str, md5: str = ""):
        self.calls.append({
            "target_dir_id": target_dir_id,
            "path": path,
            "size": size,
            "gcid": gcid,
            "md5": md5,
        })
        queued = self.outcomes.get(path)
        if queued:
            return queued.pop(0)
        return FakeOutcome(True, remote_file_id=f"remote:{path}")


def mock_importer(fake, *, account_key=TEST_ACCOUNT_KEY):
    return patch.object(
        gcid_import, "_open_importer", side_effect=lambda: nullcontext((fake, account_key)),
    )


class GCIDImportAPITests(InitializedWebTestCase):
    def setUp(self) -> None:
        # Notifications outlive client/reset/DB cleanup, as in the old tearDown.
        notifications = ExitStack()
        self.addCleanup(notifications.close)
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.temp = tempfile.TemporaryDirectory()
        resources.callback(self.temp.cleanup)
        resources.enter_context(patch("app.database.DB_PATH", Path(self.temp.name) / "api.db"))
        resources.enter_context(patch.dict(os.environ, {
            "MEDIAFLUX_INITIALIZED": "1",
            "WEB_SECRET_KEY": "task8-test-secret",
            "ENV_WEB_PASSPORT": "admin",
            "ENV_WEB_PASSWORD": "123456",
        }))
        self.cloud_client = resources.enter_context(
            patch.object(gcid_import, "GuangYaClient", spec=True),
        ).return_value
        self.cloud_client.logged_in = True
        self.cloud_client.share_account_key.return_value = TEST_ACCOUNT_KEY
        self.cloud_client.file_info.side_effect = lambda file_id: GuangYaFile(
            file_id, "目标目录", is_dir=True,
        )
        resources.enter_context(patch.object(
            gcid_import, "GuangyaGCIDImporter",
            side_effect=AssertionError("GCID 写入测试须注入隔离的写入器"),
        ))
        notifications.enter_context(patch(
            "app.modules.telegram_notification_center.send_event_result",
            return_value=notifier.TelegramSendResult(ok=True),
        ))
        reset = getattr(gcid_import, "reset_runtime_state", None)
        if reset:
            resources.callback(reset)
            reset()
        db.init_db()
        self.client_context = TestClient(create_app(), raise_server_exceptions=False)
        resources.callback(self.client_context.close)
        self.client = resources.enter_context(self.client_context)

    @staticmethod
    def _csrf(response) -> str:
        match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
        if not match:
            match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        if not match:
            raise AssertionError("missing csrf token")
        return match.group(1)

    def authenticate(self, client: TestClient | None = None) -> dict[str, str]:
        client = client or self.client
        login_page = client.get("/login")
        response = client.post(
            "/login",
            data={
                "csrf_token": self._csrf(login_page),
                "username": "admin",
                "password": "123456",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        return {"X-CSRF-Token": self._csrf(client.get("/guangya/more?view=gcid"))}

    def preview(self, headers: dict[str, str], *, target: str = "target-1") -> dict:
        response = self.client.post(
            "/api/tools/gcid/import/preview",
            json={"manifest": _manifest(), "target_dir_id": target},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def _run_body(preview: dict, operation_token: str) -> dict:
        return {
            "preview_id": preview["preview_id"],
            "target_dir_id": preview["target_dir_id"],
            "manifest_digest": preview["manifest_digest"],
            "confirm": "GCID",
            "operation_token": operation_token,
        }

    @staticmethod
    def _rendered_gcid_script(html: str) -> str:
        return next(
            script for script in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.DOTALL)
            if "[data-gcid-manifest-schema]" in script
        )

    def test_import_endpoints_require_login_and_csrf(self):
        anonymous = self.client.post(
            "/api/tools/gcid/import/preview",
            json={"manifest": _manifest(), "target_dir_id": "target-1"},
        )
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(
            self.client.get("/api/tools/gcid/import/tasks").status_code,
            401,
        )

        self.authenticate()
        denied = self.client.post(
            "/api/tools/gcid/import/preview",
            json={"manifest": _manifest(), "target_dir_id": "target-1"},
        )
        self.assertEqual(denied.status_code, 403)

    def test_preview_is_read_only_and_returns_safe_tree_bound_to_owner_target_digest(self):
        headers = self.authenticate()
        root_preview = self.preview(headers, target="0")
        self.assertEqual(root_preview["target_dir_id"], "0")
        self.cloud_client.file_info.assert_not_called()
        preview = self.preview(headers)
        self.cloud_client.file_info.assert_called_once_with("target-1")

        self.assertEqual(preview["file_count"], 2)
        self.assertEqual(preview["total_size"], 30)
        self.assertEqual(preview["target_dir_id"], "target-1")
        self.assertRegex(preview["manifest_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(preview["tree"][0]["name"], "Movies")
        self.assertNotIn("gcid-a", json.dumps(preview, ensure_ascii=False))
        self.assertEqual(db.list_gcid_import_tasks(), [])

        drift = self.client.post(
            "/api/tools/gcid/import/run",
            json={
                "preview_id": preview["preview_id"],
                "target_dir_id": "target-2",
                "manifest_digest": preview["manifest_digest"],
                "confirm": "GCID",
                "operation_token": "run-target-drift",
            },
            headers=headers,
        )
        self.assertEqual(drift.status_code, 409)
        self.assertIn("目标目录", drift.json()["error"])

        second_context = TestClient(create_app(), raise_server_exceptions=False)
        with second_context as second:
            second_headers = self.authenticate(second)
            wrong_owner = second.post(
                "/api/tools/gcid/import/run",
                json={
                    "preview_id": preview["preview_id"],
                    "target_dir_id": "target-1",
                    "manifest_digest": preview["manifest_digest"],
                    "confirm": "GCID",
                    "operation_token": "run-wrong-owner",
                },
                headers=second_headers,
            )
        self.assertEqual(wrong_owner.status_code, 409)
        self.assertIn("当前用户", wrong_owner.json()["error"])

    def test_preview_rejects_unverified_target_before_storing_snapshot(self):
        headers = self.authenticate()
        cases = (
            ("missing", None, 400),
            ("file", GuangYaFile("target-1", "Film.mkv", is_dir=False), 400),
            ("wrong-id", GuangYaFile("Target-1", "另一个目录", is_dir=True), 400),
            ("lookup-error", RuntimeError("isolated target lookup error"), 503),
        )
        with patch.object(
            gcid_import._preview_store, "create", wraps=gcid_import._preview_store.create,
        ) as create:
            for name, lookup_result, status in cases:
                with self.subTest(name=name), patch.object(
                    self.cloud_client, "file_info", side_effect=[lookup_result],
                ) as lookup:
                    response = self.client.post(
                        "/api/tools/gcid/import/preview",
                        json={"manifest": _manifest(), "target_dir_id": "target-1"},
                        headers=headers,
                    )
                    self.assertEqual(response.status_code, status, response.text)
                    data = response.json()
                    self.assertIn("目标目录", data["error"])
                    self.assertNotIn("preview_id", data)
                    lookup.assert_called_once_with("target-1")
            create.assert_not_called()

    def test_capabilities_reflect_cloud_login_state(self):
        self.authenticate()
        for logged_in in (False, True):
            with self.subTest(logged_in=logged_in):
                self.cloud_client.logged_in = logged_in
                response = self.client.get("/api/tools/gcid/capabilities")
                self.assertEqual(response.status_code, 200, response.text)
                capabilities = response.json()
                self.assertIs(capabilities["export"], logged_in)
                self.assertIs(capabilities["cloud_only_import"], logged_in)
                self.assertIs(capabilities["validate"], True)

    def test_offline_preview_is_allowed_but_run_and_retry_require_cloud_login(self):
        self.cloud_client.logged_in = False
        headers = self.authenticate()
        preview = self.preview(headers)
        body = self._run_body(preview, "run-unavailable")

        run = self.client.post(
            "/api/tools/gcid/import/run", json=body, headers=headers
        )
        self.assertEqual(run.status_code, 503, run.text)
        self.assertIn("登录", run.json()["error"])
        self.cloud_client.share_account_key.assert_not_called()
        self.cloud_client.file_info.assert_not_called()
        self.assertEqual(db.list_gcid_import_tasks(), [])

        self.cloud_client.logged_in = True
        fake = FakeImporter({
            "Movies/B.mkv": [FakeOutcome(False, error_code="private-denied")],
        })
        with mock_importer(fake):
            stale = self.client.post("/api/tools/gcid/import/run", json=body, headers=headers)
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertEqual(fake.calls, [])
            self.assertEqual(db.list_gcid_import_tasks(), [])
            partial = self.client.post(
                "/api/tools/gcid/import/run",
                json=self._run_body(self.preview(headers), "run-for-retry"),
                headers=headers,
            )
        self.assertEqual(partial.status_code, 200, partial.text)
        self.assertEqual(partial.json()["task"]["status"], "partial_success")
        task_id = partial.json()["task"]["id"]
        self.cloud_client.logged_in = False
        before = db.list_gcid_import_items(task_id)
        retry = self.client.post(
            f"/api/tools/gcid/import/{task_id}/retry",
            json={"confirm": "GCID", "operation_token": "retry-unavailable"},
            headers=headers,
        )
        self.assertEqual(retry.status_code, 503, retry.text)
        self.assertIn("登录", retry.json()["error"])
        self.assertEqual(db.list_gcid_import_items(task_id), before)
        self.assertEqual(len(fake.calls), 2)

    def test_run_refuses_a_writer_for_a_different_cloud_account(self):
        headers = self.authenticate()
        body = self._run_body(self.preview(headers), "run-after-account-switch")
        fake = FakeImporter()
        with mock_importer(fake, account_key="b" * 64):
            run = self.client.post("/api/tools/gcid/import/run", json=body, headers=headers)
        self.assertEqual(run.status_code, 409, run.text)
        self.assertIn("账号", run.json()["error"])
        self.assertEqual(fake.calls, [])
        self.assertEqual(db.list_gcid_import_tasks(), [])

    def test_fake_importer_success_is_idempotent_and_history_is_sanitized(self):
        headers = self.authenticate()
        preview = self.preview(headers)
        fake = FakeImporter()
        events = []
        body = self._run_body(preview, "run-success-once")

        thread_keys = []
        with mock_importer(fake) as open_writer, patch(
            "app.modules.telegram_notification_center.publish_notification_thread",
            side_effect=lambda key, event, **_kwargs: (
                thread_keys.append(key)
                or events.append(event)
                or NotificationPublishResult(True, delivered=True, status="sent")
            ),
        ):
            first = self.client.post(
                "/api/tools/gcid/import/run", json=body, headers=headers
            )
            self.cloud_client.logged_in = False
            replay = self.client.post(
                "/api/tools/gcid/import/run", json=body, headers=headers
            )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["task"]["status"], "success")
        self.assertFalse(first.json()["replayed"])
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["task"], first.json()["task"])
        open_writer.assert_called_once_with()
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(events), 2)
        self.assertEqual(thread_keys, [thread_keys[0], thread_keys[0]])
        self.assertTrue(thread_keys[0].startswith("gcid:"))
        rendered_events = json.dumps(
            [{"title": event.title, "fields": list(event.fields), "lines": list(event.lines)} for event in events],
            ensure_ascii=False,
        )
        self.assertIn("进行中", rendered_events)
        self.assertIn("成功", rendered_events)
        self.assertNotIn("gcid-a", rendered_events)
        self.assertNotIn(body["operation_token"], rendered_events)

        history = self.client.get("/api/tools/gcid/import/tasks")
        self.assertEqual(history.status_code, 200)
        history_text = history.text
        self.assertEqual(history.json()["tasks"][0]["status"], "success")
        self.assertNotIn("gcid-a", history_text)
        self.assertNotIn(body["operation_token"], history_text)
        self.assertNotIn("PRIVATE-RESPONSE", history_text)

    def test_clear_history_removes_finished_items_and_keeps_inflight_tasks(self):
        endpoint = "/api/tools/gcid/import/tasks"
        self.assertIn(self.client.delete(endpoint).status_code, (401, 403))
        headers = self.authenticate()
        preview = self.preview(headers)
        body = self._run_body(preview, "once-before-clear")
        fake = FakeImporter()
        with mock_importer(fake):
            completed = self.client.post("/api/tools/gcid/import/run", json=body, headers=headers)
            self.assertEqual(completed.status_code, 200, completed.text)
            finished_id = completed.json()["task"]["id"]
            calls = len(fake.calls)
            statuses = ["previewed", "running", "failed", "partial_success"]
            ids = []
            for status in statuses:
                task_id = db.create_gcid_import_task(
                    operation_token=status, manifest_digest="a" * 64, target_dir_id="target",
                    file_count=1, total_size=1, items=[{"path": "file", "size": 1, "gcid": "gcid"}],
                )
                db.update_gcid_import_task(task_id, status=status)
                ids.append(task_id)

            self.assertEqual(self.client.delete(endpoint).status_code, 403)
            self.assertEqual(self.client.delete(endpoint, headers=headers).json(), {"deleted": 3, "retained": 2})
            self.assertEqual(self.client.delete(endpoint, headers=headers).json(), {"deleted": 0, "retained": 2})
            self.assertEqual({t["id"] for t in self.client.get(endpoint).json()["tasks"]}, set(ids[:2]))
            self.assertTrue(all(db.list_gcid_import_items(i) for i in ids[:2]))
            self.assertFalse(any(db.list_gcid_import_items(i) for i in [finished_id, *ids[2:]]))
            replay = self.client.post("/api/tools/gcid/import/run", json=body, headers=headers)
            self.assertEqual(replay.status_code, 400, replay.text)
            self.assertIn("已清除", replay.json()["error"])
            self.assertEqual(len(fake.calls), calls)
            self.assertEqual(len(self.client.get(endpoint).json()["tasks"]), 2)

    def test_partial_success_retry_only_failed_items_preserves_success_item_id(self):
        headers = self.authenticate()
        preview = self.preview(headers)
        fake = FakeImporter({
            "Movies/B.mkv": [FakeOutcome(False, error_code="PRIVATE secret-token response")],
        })
        with mock_importer(fake):
            run = self.client.post(
                "/api/tools/gcid/import/run",
                json=self._run_body(preview, "run-partial"),
                headers=headers,
            )
        self.assertEqual(run.status_code, 200, run.text)
        task = run.json()["task"]
        self.assertEqual(task["status"], "partial_success")
        self.assertEqual(task["success_count"], 1)
        self.assertEqual(task["failed_count"], 1)
        task_before = db.get_gcid_import_task(task["id"])
        assert task_before is not None
        self.assertEqual(task_before["target_account_key"], TEST_ACCOUNT_KEY)
        items_before = db.list_gcid_import_items(task["id"])
        success_before = next(row for row in items_before if row["status"] == "success")
        failed_before = next(row for row in items_before if row["status"] == "failed")
        self.assertEqual(failed_before["md5"], TEST_FILE_MD5)
        self.assertNotIn("PRIVATE secret-token response", run.text)
        self.assertNotIn("PRIVATE secret-token response", failed_before["error"])

        retry_path = f"/api/tools/gcid/import/{task['id']}/retry"
        retry_body = {"confirm": "GCID", "operation_token": "retry-partial-once"}
        other_account = FakeImporter()
        with mock_importer(other_account, account_key="b" * 64):
            rejected = self.client.post(retry_path, json=retry_body, headers=headers)
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertIn("账号", rejected.json()["error"])
        self.assertEqual(other_account.calls, [])
        self.assertEqual(db.get_gcid_import_task(task["id"]), task_before)
        self.assertEqual(db.list_gcid_import_items(task["id"]), items_before)

        gcid_import.reset_runtime_state()
        retry_writer = FakeImporter({"Movies/B.mkv": [FakeOutcome(True, remote_file_id="remote-b")]})
        with mock_importer(retry_writer) as open_writer:
            retry = self.client.post(retry_path, json=retry_body, headers=headers)
            replay = self.client.post(retry_path, json=retry_body, headers=headers)

        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["task"]["status"], "success")
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertFalse(retry.json()["replayed"])
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["task"], retry.json()["task"])
        open_writer.assert_called_once_with()
        self.assertEqual([(call["path"], call["md5"]) for call in fake.calls], [
            ("Movies/A.mkv", ""), ("Movies/B.mkv", TEST_FILE_MD5),
        ])
        self.assertEqual(retry_writer.calls, [
            {
                "target_dir_id": "target-1", "path": "Movies/B.mkv",
                "size": 20, "gcid": "gcid-b", "md5": TEST_FILE_MD5,
            },
        ])
        items_after = db.list_gcid_import_items(task["id"])
        success_after = next(row for row in items_after if row["path"] == success_before["path"])
        self.assertEqual(success_after["id"], success_before["id"])
        self.assertTrue(all(row["status"] == "success" for row in items_after))
        persisted = json.dumps([dict(row) for row in items_after], ensure_ascii=False)
        self.assertNotIn("PRIVATE secret-token response", persisted)
        self.assertNotIn("PRIVATE-RESPONSE-MUST-NOT-LEAK", persisted)

    def test_legacy_unbound_task_history_is_readable_but_retry_requires_reimport(self):
        headers = self.authenticate()
        task_id = db.create_gcid_import_task(
            operation_token="legacy-import",
            manifest_digest="b" * 64,
            target_dir_id="target-1",
            file_count=1,
            total_size=10,
        )
        db.replace_gcid_import_items(task_id, [
            {"path": "Old.mkv", "size": 10, "gcid": "gcid-old", "status": "failed"},
        ])
        gcid_import._finish_task(task_id)
        items_before = db.list_gcid_import_items(task_id)

        history = self.client.get("/api/tools/gcid/import/tasks")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(
            [(task["id"], task["status"]) for task in history.json()["tasks"]],
            [(task_id, "failed")],
        )

        fake = FakeImporter()
        with mock_importer(fake):
            retry = self.client.post(
                f"/api/tools/gcid/import/{task_id}/retry",
                json={"confirm": "GCID", "operation_token": "retry-legacy"},
                headers=headers,
            )
        self.assertEqual(retry.status_code, 409, retry.text)
        self.assertIn("重新", retry.json()["error"])
        self.assertIn("导入", retry.json()["error"])
        self.assertEqual(fake.calls, [])
        self.assertEqual(db.list_gcid_import_items(task_id), items_before)

    def test_run_requires_manifest_digest_binding(self):
        headers = self.authenticate()
        preview = self.preview(headers)
        fake = FakeImporter()
        with mock_importer(fake):
            response = self.client.post(
                "/api/tools/gcid/import/run",
                json={
                    "preview_id": preview["preview_id"],
                    "target_dir_id": preview["target_dir_id"],
                    "manifest_digest": "",
                    "confirm": "GCID",
                    "operation_token": "run-missing-digest",
                },
                headers=headers,
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("摘要", response.json()["error"])
        self.assertEqual(fake.calls, [])

    def test_atomic_task_claim_prevents_stale_precheck_from_replaying_cloud_writes(self):
        headers = self.authenticate()
        preview = self.preview(headers)
        fake = FakeImporter()
        body = self._run_body(preview, "run-stale-precheck-race")
        with mock_importer(fake), patch.object(gcid_import, "_task_by_operation_token", return_value=None):
            first = self.client.post(
                "/api/tools/gcid/import/run", json=body, headers=headers
            )
            second = self.client.post(
                "/api/tools/gcid/import/run", json=body, headers=headers
            )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(len(fake.calls), 2)

    def test_run_and_retry_require_exact_confirmation_text(self):
        headers = self.authenticate()
        preview = self.preview(headers)
        fake = FakeImporter()
        with mock_importer(fake):
            response = self.client.post(
                "/api/tools/gcid/import/run",
                json={
                    "preview_id": preview["preview_id"],
                    "target_dir_id": preview["target_dir_id"],
                    "manifest_digest": preview["manifest_digest"],
                    "confirm": "gcid",
                    "operation_token": "run-bad-confirmation",
                },
                headers=headers,
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("GCID", response.json()["error"])
        self.assertEqual(fake.calls, [])

    def test_gcid_page_keeps_existing_tools_and_adds_stable_safe_import_workspace(self):
        self.authenticate()
        page = self.client.get("/guangya/more?view=gcid")
        self.assertEqual(page.status_code, 200)
        html = page.text
        for control_id in (
            "gcidChooseDir", "gcidExportBtn", "gcidValidateBtn",
            "gcidImportFile", "gcidImportChooseTarget", "gcidImportPreviewBtn",
            "gcidImportTree", "gcidImportConfirm", "gcidImportRunBtn",
            "gcidImportProgress", "gcidImportHistory", "gcidImportRefresh",
        ):
            self.assertIn(f'id="{control_id}"', html)
        self.assertIn("/api/tools/gcid/import/preview", html)
        self.assertIn("/api/tools/gcid/import/run", html)
        self.assertIn("/api/tools/gcid/import/tasks", html)
        gcid_script = self._rendered_gcid_script(html)
        self.assertIn("textContent", gcid_script)
        self.assertNotIn("innerHTML", gcid_script)
        self.assertIn('aria-live="polite"', html)

        css = Path("app/static/css/main.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.gcid-import-preview[^{}]*\{[^}]*min-height")
        self.assertRegex(css, r"\.gcid-import-history[^{}]*\{[^}]*min-height")
        self.assertIn(".gcid-stable-button", css)

    def test_official_export_passes_rendered_page_validation_and_server_preview(self):
        headers = self.authenticate()
        self.cloud_client.list_dir.return_value = [
            GuangYaFile("file-1", "Film.mkv", False, 42, "gcid-film", "source", md5=TEST_FILE_MD5),
        ]
        manifest = export_manifest(self.cloud_client, "source", "导出来源")
        self.assertEqual(manifest["files"][0]["md5"], TEST_FILE_MD5)
        html = self.client.get("/guangya/more?view=gcid").text
        schema_data = re.search(r'data-gcid-manifest-schema="([^"]+)"', html)
        assert schema_data is not None
        script = """
const {pageScript, schema, cases} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const nodes = new Map();
function control() {
    return {
        dataset: {}, value: '', handlers: {}, classList: {toggle() {}},
        addEventListener(event, handler) { this.handlers[event] = handler; },
        appendChild() {}, replaceChildren() {}, setAttribute() {},
    };
}
globalThis.document = {
    querySelector: () => ({dataset: {gcidManifestSchema: schema}}),
    getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, control());
        return nodes.get(id);
    },
    createElement: control,
    createDocumentFragment: control,
};
globalThis.window = globalThis;
globalThis.window.addEventListener = () => {};
globalThis.openGuangYaDirectoryPicker = ({onSelect}) => onSelect({id: 'target-1', name: '目标'});
globalThis.fetch = async url => {
    const replies = {
        '/api/tools/gcid/capabilities': {export: true, validate: true, cloud_only_import: true},
        '/api/tools/gcid/export': {task: null},
        '/api/tools/gcid/import/tasks': {tasks: []},
    };
    if (!Object.hasOwn(replies, url)) throw new Error('Unexpected request: ' + url);
    return {ok: true, json: async () => replies[url]};
};
require('node:vm').runInThisContext(pageScript, {filename: 'gcid-page.js'});
(async () => {
    await new Promise(setImmediate);
    const picker = document.getElementById('gcidImportChooseTarget');
    picker.handlers.click({currentTarget: picker});
    const input = document.getElementById('gcidImportFile');
    const preview = document.getElementById('gcidImportPreviewBtn');
    const results = [];
    for (const data of cases) {
        input.files = [{name: 'manifest.json', text: async () => JSON.stringify(data)}];
        await input.handlers.change({target: input, currentTarget: input});
        results.push(!preview.disabled);
    }
    console.log(JSON.stringify(results));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        cases = [manifest, {**manifest, "format": "mediaflux.gcid"}, {**manifest, "version": 1}]
        result = subprocess.run(
            ["node", "-e", script],
            input=json.dumps({
                "pageScript": self._rendered_gcid_script(html),
                "schema": unescape(schema_data.group(1)),
                "cases": cases,
            }),
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [True, False, False])
        preview = self.client.post(
            "/api/tools/gcid/import/preview",
            json={"manifest": manifest, "target_dir_id": "target-1"}, headers=headers,
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["file_count"], 1)
        self.assertEqual(preview.json()["total_size"], 42)
        self.assertEqual(db.list_gcid_import_tasks(), [])


class GCIDImportNotificationTests(unittest.TestCase):
    def test_failed_samples_are_bounded_and_private_errors_are_never_forwarded(self):
        captured = []
        samples = [
            {"path": f"Dir/File-{index}.mkv", "error": f"PRIVATE RESPONSE {index}"}
            for index in range(5)
        ]
        with patch(
            "app.modules.telegram_notification_center.publish_notification_thread",
            side_effect=lambda _key, event, **_kwargs: (
                captured.append(event)
                or NotificationPublishResult(True, delivered=True, status="sent")
            ),
        ):
            notifier.notify_gcid_import_finished(
                task_id=9,
                status="partial_success",
                success_count=2,
                failed_count=5,
                failed_samples=samples,
            )
        self.assertEqual(len(captured), 1)
        event = captured[0]
        self.assertEqual(event.layout, "relaxed")
        self.assertEqual(len(event.lines), 3)
        rendered = json.dumps({
            "title": event.title,
            "fields": list(event.fields),
            "lines": list(event.lines),
        }, ensure_ascii=False)
        self.assertNotIn("PRIVATE RESPONSE", rendered)
        self.assertIn("File-0.mkv", rendered)
        self.assertNotIn("Dir/", rendered)
        self.assertIn("导入失败", rendered)


if __name__ == "__main__":
    unittest.main()
