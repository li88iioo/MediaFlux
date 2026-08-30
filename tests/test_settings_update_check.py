from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.update_check import UpdateInfo


class SettingsUpdateCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(start_background=False)
        self.client = TestClient(self.app)

    def test_update_check_api_requires_login(self) -> None:
        response = self.client.get("/api/update/check")
        self.assertEqual(response.status_code, 401)

    def test_update_check_api_returns_update_info_when_logged_in(self) -> None:
        mock_info = UpdateInfo(
            current_version="0.1.0",
            latest_version="0.1.1",
            update_available=True,
            prerelease=False,
            release_url="https://github.com/li88iioo/MediaFlux/releases/tag/v0.1.1",
            published_at="2026-08-18T12:00:00Z",
            recommended_asset_name="MediaFlux-0.1.1-source.tar.gz",
            recommended_asset_url="https://github.com/li88iioo/MediaFlux/releases/download/v0.1.1/MediaFlux-0.1.1-source.tar.gz",
        )
        with patch("app.routes.api.require_api_login"), patch(
            "app.modules.update_check.check_for_updates", return_value=mock_info
        ):
            response = self.client.get("/api/update/check")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("success"))
            update = payload.get("update", {})
            self.assertEqual(update.get("current_version"), "0.1.0")
            self.assertEqual(update.get("latest_version"), "0.1.1")
            self.assertTrue(update.get("update_available"))

    def test_settings_page_renders_version_and_update_button(self) -> None:
        with patch("app.routes.pages.require_page_login", return_value=None):
            response = self.client.get("/settings")
            self.assertEqual(response.status_code, 200)
            self.assertIn("telemetryStatusBadge", response.text)
            self.assertIn("telemetryCheckUpdateBtn", response.text)
            self.assertIn("data-current-version", response.text)


    def test_update_badge_restore_is_generation_guarded(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app/static/js/settings.js").read_text(encoding="utf-8")
        self.assertIn("let updateCheckGeneration=0", source)
        self.assertIn("let updateBadgeRestoreTimer=null", source)
        self.assertIn("clearUpdateBadgeRestore()", source)
        self.assertIn("window.clearTimeout(updateBadgeRestoreTimer)", source)
        self.assertIn("if(generation!==updateCheckGeneration)return", source)
        self.assertIn("const defaultUpdateBadge=", source)


if __name__ == "__main__":
    unittest.main()
