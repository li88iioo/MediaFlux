"""Web 与 Agent 取消积压共享冻结服务；接口需登录和显式确认。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config
from app import database as db
from app.main import create_app
from app.repositories import strm as strm_repo
from tests.support import IsolatedDatabaseTestCase
from tests.test_config_empty_values_api import login
from tests.test_strm_index_diagnostics import _api_client
from tests.test_strm_metadata_queue import _job


class MetadataManagementApiTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_metadata_queue")

    def test_disabled_queue_preview_confirm_replay_and_files_preserved(self):
        strm_repo.enqueue_strm_metadata_jobs([_job()])
        with tempfile.TemporaryDirectory() as root:
            existing = Path(root) / "present.nfo"
            existing.write_text("keep")
            # 暂停伴随同步仍保留有效来源；空来源列表会在启动时按 S02 退役待办。
            config.set_and_save({
                "GY_STRM_SOURCE_DIRS": '[{"id":"source","name":"整理"}]',
                "STRM_ROOT": root,
                "STRM_METADATA_ENABLED": "false",
            })
            before = strm_repo.list_strm_metadata_queue()
            with TestClient(create_app(start_background=False)) as client:
                headers = login(client)
                self.assertEqual(strm_repo.list_strm_metadata_queue(), before)
                response = client.post(
                    "/api/strm/metadata/cancel-pending/preview",
                    json={},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["preview"]["count"], 1)
                self.assertFalse(response.json()["preview"]["enabled"])
                self.assertEqual(strm_repo.count_strm_metadata_jobs()["queued"], 1)
                token = response.json()["confirmation_token"]
                invalid = client.post(
                    "/api/strm/metadata/cancel-pending",
                    json={"confirmation_token": token, "confirm": False},
                    headers=headers,
                )
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual(strm_repo.list_strm_metadata_queue(), before)
                confirmed = client.post(
                    "/api/strm/metadata/cancel-pending",
                    json={"confirmation_token": token, "confirm": True},
                    headers=headers,
                )
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                self.assertEqual(confirmed.json()["cancelled"], 1)
                self.assertEqual(confirmed.json()["files_deleted"], 0)
                self.assertFalse(confirmed.json()["enabled"])
                self.assertEqual(strm_repo.count_strm_metadata_jobs()["cancelled"], 1)
                self.assertEqual(strm_repo.count_strm_metadata_jobs()["queued"], 0)
                replay = client.post(
                    "/api/strm/metadata/cancel-pending",
                    json={"confirmation_token": token, "confirm": True},
                    headers=headers,
                )
                self.assertEqual(replay.status_code, 409)
            self.assertEqual(existing.read_text(), "keep")

    def test_status_reports_worker_without_raw_path_or_identifier(self):
        raw = {
            "enabled": False,
            "worker_running": True,
            "consumer_active": True,
            "running": 0,
            "queued": 10,
            "current_job_id": 999,
            "last_error_type": "private",
        }
        with (
            tempfile.TemporaryDirectory() as root,
            _api_client(Path(root)) as (client, _csrf),
            patch(
                "app.modules.strm_metadata_worker.get_strm_metadata_worker"
            ) as worker,
        ):
            worker.return_value.status.return_value = raw
            response = client.get("/api/strm/metadata/status")
            self.assertEqual(response.status_code, 200)
            value = response.json()
            self.assertEqual(value["state"], "paused")
            self.assertTrue(value["worker_running"])
            self.assertNotIn("current_job_id", value)
            self.assertNotIn("last_error_type", value)

    def test_anonymous_cannot_read_or_prepare(self):
        with (
            tempfile.TemporaryDirectory() as root,
            _api_client(Path(root), login=False) as (client, _csrf),
        ):
            self.assertEqual(client.get("/api/strm/metadata/status").status_code, 401)
            self.assertIn(
                client.post(
                    "/api/strm/metadata/cancel-pending/preview", json={}
                ).status_code,
                (401, 403),
            )

    def test_no_snapshot_no_mutation(self):
        strm_repo.enqueue_strm_metadata_jobs([_job()])
        with tempfile.TemporaryDirectory() as root:
            config.set_and_save({
                "GY_STRM_SOURCE_DIRS": '[{"id":"source","name":"整理"}]',
                "STRM_ROOT": root,
                "STRM_METADATA_ENABLED": "false",
            })
            before = strm_repo.list_strm_metadata_queue()
            with TestClient(create_app(start_background=False)) as client:
                headers = login(client)
                self.assertEqual(strm_repo.list_strm_metadata_queue(), before)
                response = client.post(
                    "/api/strm/metadata/cancel-pending",
                    json={"confirm": True, "confirmation_token": "forged"},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(strm_repo.list_strm_metadata_queue(), before)
        self.assertEqual(strm_repo.count_strm_metadata_jobs()["queued"], 1)
