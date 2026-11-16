from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
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


def _digest(payload: dict) -> str:
    canonical = {key: value for key, value in payload.items() if key != "integrity"}
    raw = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _manifest(files: list[dict] | None = None, **overrides) -> dict:
    entries: list[dict] = files if files is not None else [
        {"path": "Movies/A.mkv", "size": 10, "gcid": "gcid-a"},
        {"path": "Movies/B.mkv", "size": 20, "gcid": "gcid-b"},
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
    available = True
    unavailable_reason = ""

    def __init__(self, outcomes: dict[str, list[FakeOutcome]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[dict] = []

    def import_file(self, *, target_dir_id: str, path: str, size: int, gcid: str):
        self.calls.append({
            "target_dir_id": target_dir_id,
            "path": path,
            "size": size,
            "gcid": gcid,
        })
        queued = self.outcomes.get(path)
        if queued:
            return queued.pop(0)
        return FakeOutcome(True, remote_file_id=f"remote:{path}")


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
        preview = self.preview(headers)

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

    def test_production_default_run_and_retry_fail_closed_with_503(self):
        headers = self.authenticate()
        preview = self.preview(headers)
        body = {
            "preview_id": preview["preview_id"],
            "target_dir_id": preview["target_dir_id"],
            "manifest_digest": preview["manifest_digest"],
            "confirm": "GCID",
            "operation_token": "run-unavailable",
        }

        run = self.client.post(
            "/api/tools/gcid/import/run", json=body, headers=headers
        )
        self.assertEqual(run.status_code, 503)
        self.assertIn("能力不可用", run.json()["error"])
        self.assertEqual(db.list_gcid_import_tasks(), [])

        fake = FakeImporter({
            "Movies/B.mkv": [FakeOutcome(False, error_code="private-denied")],
        })
        with patch(
            "app.modules.gcid_import.get_private_importer",
            return_value=fake,
            create=True,
        ):
            partial = self.client.post(
                "/api/tools/gcid/import/run",
                json={**body, "operation_token": "run-for-retry"},
                headers=headers,
            )
        task_id = partial.json()["task"]["id"]
        retry = self.client.post(
            f"/api/tools/gcid/import/{task_id}/retry",
            json={"confirm": "GCID", "operation_token": "retry-unavailable"},
            headers=headers,
        )
        self.assertEqual(retry.status_code, 503)
        self.assertIn("能力不可用", retry.json()["error"])
        self.assertEqual(len(fake.calls), 2)

    def test_fake_importer_success_is_idempotent_and_history_is_sanitized(self):
        headers = self.authenticate()
        preview = self.preview(headers)
        fake = FakeImporter()
        events = []
        body = {
            "preview_id": preview["preview_id"],
            "target_dir_id": preview["target_dir_id"],
            "manifest_digest": preview["manifest_digest"],
            "confirm": "GCID",
            "operation_token": "run-success-once",
        }

        thread_keys = []
        with patch(
            "app.modules.gcid_import.get_private_importer",
            return_value=fake,
            create=True,
        ), patch(
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
            replay = self.client.post(
                "/api/tools/gcid/import/run", json=body, headers=headers
            )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["task"]["status"], "success")
        self.assertFalse(first.json()["replayed"])
        self.assertTrue(replay.json()["replayed"])
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

    def test_partial_success_retry_only_failed_items_preserves_success_item_id(self):
        headers = self.authenticate()
        preview = self.preview(headers)
        fake = FakeImporter({
            "Movies/B.mkv": [
                FakeOutcome(False, error_code="PRIVATE secret-token response"),
                FakeOutcome(True, remote_file_id="remote-b"),
            ],
        })
        run_body = {
            "preview_id": preview["preview_id"],
            "target_dir_id": preview["target_dir_id"],
            "manifest_digest": preview["manifest_digest"],
            "confirm": "GCID",
            "operation_token": "run-partial",
        }
        with patch(
            "app.modules.gcid_import.get_private_importer",
            return_value=fake,
            create=True,
        ):
            run = self.client.post(
                "/api/tools/gcid/import/run", json=run_body, headers=headers
            )
            self.assertEqual(run.status_code, 200, run.text)
            task = run.json()["task"]
            self.assertEqual(task["status"], "partial_success")
            self.assertEqual(task["success_count"], 1)
            self.assertEqual(task["failed_count"], 1)
            items_before = db.list_gcid_import_items(task["id"])
            success_before = next(row for row in items_before if row["status"] == "success")

            retry_body = {"confirm": "GCID", "operation_token": "retry-partial-once"}
            retry = self.client.post(
                f"/api/tools/gcid/import/{task['id']}/retry",
                json=retry_body,
                headers=headers,
            )
            replay = self.client.post(
                f"/api/tools/gcid/import/{task['id']}/retry",
                json=retry_body,
                headers=headers,
            )

        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["task"]["status"], "success")
        self.assertFalse(retry.json()["replayed"])
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual([call["path"] for call in fake.calls], [
            "Movies/A.mkv", "Movies/B.mkv", "Movies/B.mkv",
        ])
        items_after = db.list_gcid_import_items(task["id"])
        success_after = next(row for row in items_after if row["path"] == success_before["path"])
        self.assertEqual(success_after["id"], success_before["id"])
        self.assertTrue(all(row["status"] == "success" for row in items_after))
        persisted = json.dumps([dict(row) for row in items_after], ensure_ascii=False)
        self.assertNotIn("PRIVATE secret-token response", persisted)
        self.assertNotIn("PRIVATE-RESPONSE-MUST-NOT-LEAK", persisted)

    def test_run_requires_manifest_digest_binding(self):
        headers = self.authenticate()
        preview = self.preview(headers)
        fake = FakeImporter()
        with patch(
            "app.modules.gcid_import.get_private_importer",
            return_value=fake,
            create=True,
        ):
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
        body = {
            "preview_id": preview["preview_id"],
            "target_dir_id": preview["target_dir_id"],
            "manifest_digest": preview["manifest_digest"],
            "confirm": "GCID",
            "operation_token": "run-stale-precheck-race",
        }
        with patch(
            "app.modules.gcid_import.get_private_importer",
            return_value=fake,
            create=True,
        ), patch("app.modules.gcid_import._task_by_operation_token", return_value=None):
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
        with patch(
            "app.modules.gcid_import.get_private_importer",
            return_value=fake,
            create=True,
        ):
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
        gcid_script = Path("app/templates/_gcid_scripts.html").read_text(encoding="utf-8")
        self.assertIn("textContent", gcid_script)
        self.assertNotIn("innerHTML", gcid_script)
        self.assertIn('aria-live="polite"', html)

        css = Path("app/static/css/main.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.gcid-import-preview[^{}]*\{[^}]*min-height")
        self.assertRegex(css, r"\.gcid-import-history[^{}]*\{[^}]*min-height")
        self.assertIn(".gcid-stable-button", css)

    def test_official_export_passes_rendered_page_validation_and_server_preview(self):
        headers = self.authenticate()
        client = type("ExportClient", (), {
            "list_dir": lambda _self, _directory: [
                GuangYaFile("file-1", "Film.mkv", False, 42, "gcid-film", "source")
            ],
        })()
        manifest = export_manifest(client, "source", "导出来源")
        html = self.client.get("/guangya/more?view=gcid").text
        validator = re.search(r"    function clientValidateManifest\(data\).*", html)
        self.assertIsNotNone(validator)
        schema = re.search(r"    const manifestSchema=.*", html)
        schema_data = re.search(r'data-gcid-manifest-schema="([^"]+)"', html)
        self.assertIsNotNone(schema_data)
        script = "\n".join((
            "const document={querySelector:()=>({dataset:{gcidManifestSchema:"
            + json.dumps(unescape(schema_data.group(1)) if schema_data else "{}") + "}})};",
            schema.group(0) if schema else "",
            validator.group(0) if validator else "",
            "const cases=JSON.parse(require('node:fs').readFileSync(0,'utf8'));",
            "console.log(JSON.stringify(cases.map(data=>{try{clientValidateManifest(data);return true;}catch(_){return false;}})));",
        ))
        cases = [manifest, {**manifest, "format": "mediaflux.gcid"}, {**manifest, "version": 1}]
        result = subprocess.run(
            ["node", "-e", script], input=json.dumps(cases), text=True,
            capture_output=True, check=True,
        )
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
