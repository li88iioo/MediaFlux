"""光鸭频道监控下线后的行为契约。"""
from __future__ import annotations

import importlib.util
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.main import create_app


class ChannelFeatureRemovalTests(unittest.TestCase):
    PROJECT_ROOT = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch("app.database.DB_PATH", Path(self.temp.name) / "channel-removed.db")
        self.db_patch.start()
        self.env_patch = patch.dict(os.environ, {
            "MEDIAFLUX_INITIALIZED": "1",
            "WEB_SECRET_KEY": "channel-removal-test-secret",
            "ENV_WEB_PASSPORT": "admin",
            "ENV_WEB_PASSWORD": "123456",
        })
        self.env_patch.start()
        db.init_db()
        self.client = TestClient(create_app(), raise_server_exceptions=False)
        self._authenticate()

    def tearDown(self):
        self.client.close()
        self.env_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _csrf(response) -> str:
        match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
        if not match:
            match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        if not match:
            raise AssertionError("missing csrf token")
        return match.group(1)

    def _authenticate(self) -> None:
        page = self.client.get("/login")
        response = self.client.post(
            "/login",
            data={
                "csrf_token": self._csrf(page),
                "username": "admin",
                "password": "123456",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def test_removed_channel_page_api_and_navigation_are_unreachable(self):
        page = self.client.get("/guangya-channels")
        api = self.client.get("/api/guangya/channels")
        guangya = self.client.get("/guangya")

        self.assertEqual(page.status_code, 404)
        self.assertEqual(api.status_code, 404)
        self.assertNotIn("频道监控", guangya.text)
        self.assertNotIn("/guangya-channels", guangya.text)

    def test_removed_channel_runtime_files_and_modules_are_absent(self):
        removed_files = (
            "app/modules/guangya_channel.py",
            "app/modules/guangya_channel_scheduler.py",
            "app/routes/guangya_channel_api.py",
            "app/templates/guangya_channels.html",
        )
        removed_modules = (
            "app.modules.guangya_channel",
            "app.modules.guangya_channel_scheduler",
            "app.routes.guangya_channel_api",
        )

        for relative_path in removed_files:
            with self.subTest(path=relative_path):
                self.assertFalse((self.PROJECT_ROOT / relative_path).exists())
        for module_name in removed_modules:
            with self.subTest(module=module_name):
                self.assertIsNone(importlib.util.find_spec(module_name))

    def test_removed_channel_tables_are_absent_from_formal_schema(self):
        with db.get_conn() as conn:
            names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue({
            "gy_channel_sources", "gy_channel_items", "gy_channel_runs"
        }.isdisjoint(names))


if __name__ == "__main__":
    unittest.main()
