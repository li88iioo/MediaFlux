from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class LoginWallpaperServiceTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        db.kv_set("login_wallpaper.tmdb.daily.v1", "")

    @staticmethod
    def _config(mode: str = "tmdb", api_key: str = "tmdb-key"):
        values = {
            "LOGIN_WALLPAPER_MODE": mode,
            "TMDB_API_KEY": api_key,
        }
        return lambda key, default="": values.get(key, default)

    def test_default_mode_skips_tmdb_and_cache(self):
        from app.modules.login_wallpaper import get_login_wallpaper

        with patch("app.modules.login_wallpaper.config.get", side_effect=self._config("default")), \
             patch("app.modules.login_wallpaper.TMDBClient") as client:
            result = get_login_wallpaper(now=100)

        self.assertIsNone(result)
        client.assert_not_called()
        self.assertEqual(db.kv_get("login_wallpaper.tmdb.daily.v1"), "")

    def test_tmdb_mode_requires_api_key(self):
        from app.modules.login_wallpaper import get_login_wallpaper

        with patch("app.modules.login_wallpaper.config.get", side_effect=self._config(api_key="")), \
             patch("app.modules.login_wallpaper.TMDBClient") as client:
            result = get_login_wallpaper(now=100)

        self.assertIsNone(result)
        client.assert_not_called()

    def test_popular_movie_is_cached_for_24_hours(self):
        from app.modules.login_wallpaper import (
            get_login_wallpaper,
            refresh_login_wallpaper,
        )

        client = Mock()
        client.get.return_value = {
            "results": [
                {"id": 11, "title": "无图电影"},
                {
                    "id": 22,
                    "title": "流光之城",
                    "backdrop_path": "/city.jpg",
                    "poster_path": "/poster.jpg",
                },
            ]
        }
        with patch("app.modules.login_wallpaper.config.get", side_effect=self._config()):
            first = refresh_login_wallpaper(
                now=1_000,
                chooser=lambda rows: rows[-1],
                client_factory=lambda **_kwargs: client,
            )
            client.get.side_effect = AssertionError("cache should avoid TMDB")
            second = get_login_wallpaper(now=1_001, allow_refresh=False)

        expected = {
            "tmdb_id": 22,
            "title": "流光之城",
            "image_url": "https://image.tmdb.org/t/p/w1280/city.jpg",
            "tmdb_url": "https://www.themoviedb.org/movie/22",
        }
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        client.get.assert_called_once_with(
            "/movie/popular",
            {"page": 1, "include_adult": "false"},
        )
        cached = json.loads(db.kv_get("login_wallpaper.tmdb.daily.v1"))
        self.assertEqual(cached["expires_at"], 87_400)
        self.assertEqual(cached["wallpaper"]["tmdb_id"], 22)
        self.assertEqual(cached["wallpaper"]["image_path"], "/city.jpg")

    def test_missing_backdrop_falls_back_to_poster(self):
        from app.modules.login_wallpaper import refresh_login_wallpaper

        client = Mock()
        client.get.return_value = {
            "results": [
                {
                    "id": 33,
                    "title": "只有海报",
                    "backdrop_path": None,
                    "poster_path": "/portrait.jpg",
                }
            ]
        }
        with patch("app.modules.login_wallpaper.config.get", side_effect=self._config()):
            result = refresh_login_wallpaper(
                now=2_000,
                client_factory=lambda **_kwargs: client,
            )

        self.assertEqual(
            result["image_url"],
            "https://image.tmdb.org/t/p/w1280/portrait.jpg",
        )

    def test_expired_cache_returns_stale_wallpaper_and_schedules_refresh(self):
        from app.modules.login_wallpaper import get_login_wallpaper

        db.kv_set(
            "login_wallpaper.tmdb.daily.v1",
            json.dumps(
                {
                    "expires_at": 999,
                    "wallpaper": {
                        "tmdb_id": 1,
                        "title": "旧电影",
                        "image_path": "/old.jpg",
                    },
                }
            ),
        )
        with patch("app.modules.login_wallpaper.config.get", side_effect=self._config()), \
             patch("app.modules.login_wallpaper.schedule_login_wallpaper_refresh") as schedule:
            result = get_login_wallpaper(now=1_000)

        self.assertEqual(result["tmdb_id"], 1)
        schedule.assert_called_once()

    def test_invalid_or_failed_tmdb_response_falls_back_to_default(self):
        from app.modules.login_wallpaper import refresh_login_wallpaper

        client = Mock()
        client.get.side_effect = RuntimeError("upstream token=secret")
        with patch("app.modules.login_wallpaper.config.get", side_effect=self._config()):
            result = refresh_login_wallpaper(
                now=3_000,
                client_factory=lambda **_kwargs: client,
            )

        self.assertIsNone(result)
        cached = json.loads(db.kv_get("login_wallpaper.tmdb.daily.v1"))
        self.assertEqual(cached["retry_after"], 3_300)
        self.assertIsNone(cached["wallpaper"])

    def test_cache_miss_returns_immediately_and_starts_single_refresh(self):
        from app.modules.login_wallpaper import get_login_wallpaper

        with patch("app.modules.login_wallpaper.config.get", side_effect=self._config()), \
             patch("app.modules.login_wallpaper.schedule_login_wallpaper_refresh") as schedule:
            result = get_login_wallpaper(now=4_000)

        self.assertIsNone(result)
        schedule.assert_called_once()

    def test_cache_database_error_never_breaks_login(self):
        from app.modules.login_wallpaper import get_login_wallpaper

        with patch("app.modules.login_wallpaper.config.get", side_effect=self._config()), \
             patch("app.modules.login_wallpaper.db.kv_get", side_effect=RuntimeError("locked")), \
             patch("app.modules.login_wallpaper.schedule_login_wallpaper_refresh") as schedule:
            self.assertIsNone(get_login_wallpaper(now=5_000))
        schedule.assert_not_called()

    def test_non_finite_cache_expiry_is_rejected(self):
        from app.modules.login_wallpaper import get_login_wallpaper

        db.kv_set(
            "login_wallpaper.tmdb.daily.v1",
            '{"expires_at":NaN,"retry_after":0,"wallpaper":null}',
        )
        with patch("app.modules.login_wallpaper.config.get", side_effect=self._config()), \
             patch("app.modules.login_wallpaper.schedule_login_wallpaper_refresh"):
            self.assertIsNone(get_login_wallpaper(now=6_000))

    def test_retry_cooldown_suppresses_repeated_refreshes(self):
        from app.modules.login_wallpaper import get_login_wallpaper

        db.kv_set(
            "login_wallpaper.tmdb.daily.v1",
            '{"expires_at":0,"retry_after":7000,"wallpaper":null}',
        )
        with patch("app.modules.login_wallpaper.config.get", side_effect=self._config()), \
             patch("app.modules.login_wallpaper.schedule_login_wallpaper_refresh") as schedule:
            self.assertIsNone(get_login_wallpaper(now=6_500))
        schedule.assert_not_called()

    def test_refresh_scheduler_is_single_flight(self):
        import app.modules.login_wallpaper as wallpaper

        class PendingThread:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                pass

        wallpaper._refreshing = False
        try:
            with patch("app.modules.login_wallpaper.config.get", side_effect=self._config()), \
                 patch("app.modules.login_wallpaper._load_cache", return_value={
                     "available": True,
                     "expires_at": 0,
                     "retry_after": 0,
                     "record": None,
                     "wallpaper": None,
                 }), patch("app.modules.login_wallpaper.threading.Thread", PendingThread):
                self.assertTrue(wallpaper.schedule_login_wallpaper_refresh())
                self.assertFalse(wallpaper.schedule_login_wallpaper_refresh())
        finally:
            wallpaper._refreshing = False


class LoginWallpaperConfigTests(unittest.TestCase):
    def test_mode_validation_requires_tmdb_key(self):
        from app.routes.api import _validate_login_wallpaper_updates

        with patch("app.routes.api.config.get", return_value=""):
            with self.assertRaisesRegex(ValueError, "请先配置 TMDB API Key"):
                _validate_login_wallpaper_updates(
                    {"LOGIN_WALLPAPER_MODE": "tmdb"}
                )

        with patch("app.routes.api.config.get", return_value="saved-key"):
            self.assertEqual(
                _validate_login_wallpaper_updates(
                    {"LOGIN_WALLPAPER_MODE": "tmdb"}
                ),
                {"LOGIN_WALLPAPER_MODE": "tmdb"},
            )
            self.assertEqual(
                _validate_login_wallpaper_updates(
                    {
                        "LOGIN_WALLPAPER_MODE": "tmdb",
                        "TMDB_API_KEY": "********",
                    }
                ),
                {"LOGIN_WALLPAPER_MODE": "tmdb"},
            )

    def test_mode_validation_accepts_new_key_and_rejects_unknown_mode(self):
        from app.routes.api import _validate_login_wallpaper_updates

        self.assertEqual(
            _validate_login_wallpaper_updates(
                {
                    "LOGIN_WALLPAPER_MODE": "tmdb",
                    "TMDB_API_KEY": "new-key",
                }
            ),
            {"LOGIN_WALLPAPER_MODE": "tmdb"},
        )
        with self.assertRaisesRegex(ValueError, "登录页壁纸模式无效"):
            _validate_login_wallpaper_updates(
                {"LOGIN_WALLPAPER_MODE": "unknown"}
            )

    def test_enabled_mode_rejects_clearing_existing_tmdb_key(self):
        from app.routes.api import _validate_login_wallpaper_updates

        values = {
            "LOGIN_WALLPAPER_MODE": "tmdb",
            "TMDB_API_KEY": "saved-key",
        }
        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ):
            with self.assertRaisesRegex(ValueError, "请先配置 TMDB API Key"):
                _validate_login_wallpaper_updates({"TMDB_API_KEY": ""})


class LoginWallpaperPageTests(unittest.TestCase):
    def setUp(self):
        self.credentials = {
            "MEDIAFLUX_INITIALIZED": os.environ.get("MEDIAFLUX_INITIALIZED"),
            "ENV_WEB_PASSPORT": os.environ.get("ENV_WEB_PASSPORT"),
            "ENV_WEB_PASSWORD": os.environ.get("ENV_WEB_PASSWORD"),
        }
        os.environ["MEDIAFLUX_INITIALIZED"] = "1"
        os.environ["ENV_WEB_PASSPORT"] = "admin"
        os.environ["ENV_WEB_PASSWORD"] = "123456"
        from app.modules import first_run
        first_run._reset_startup_state_for_tests()
        self.client = TestClient(create_app(), raise_server_exceptions=False)

    def tearDown(self):
        for key, value in self.credentials.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from app.modules import first_run
        first_run._reset_startup_state_for_tests()

    @staticmethod
    def _csrf_token(response) -> str:
        match = re.search(
            r'name="csrf_token" (?:content|value)="([^"]+)"',
            response.text,
        )
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def _authenticated_headers(self) -> dict[str, str]:
        from app.config import web_credentials

        page = self.client.get("/login")
        username, password = web_credentials()
        response = self.client.post(
            "/login",
            data={
                "csrf_token": self._csrf_token(page),
                "username": username,
                "password": password,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        settings = self.client.get("/settings")
        return {"X-CSRF-Token": self._csrf_token(settings)}

    def test_login_page_renders_wallpaper_and_tmdb_link(self):
        wallpaper = {
            "tmdb_id": 22,
            "title": "流光之城",
            "image_url": "https://image.tmdb.org/t/p/w1280/city.jpg",
            "tmdb_url": "https://www.themoviedb.org/movie/22",
        }
        with patch(
            "app.routes.auth.get_login_wallpaper",
            return_value=wallpaper,
        ):
            response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        self.assertIn("login-page--wallpaper", response.text)
        self.assertIn("--login-wallpaper:", response.text)
        self.assertIn(wallpaper["image_url"], response.text)
        self.assertIn(wallpaper["tmdb_url"], response.text)
        self.assertIn("流光之城", response.text)
        self.assertIn('target="_blank"', response.text)
        self.assertIn('rel="noopener noreferrer"', response.text)
        self.assertIn('data-lucide="clapperboard"', response.text)

    def test_invalid_login_reuses_wallpaper_context(self):
        wallpaper = {
            "tmdb_id": 22,
            "title": "流光之城",
            "image_url": "https://image.tmdb.org/t/p/w1280/city.jpg",
            "tmdb_url": "https://www.themoviedb.org/movie/22",
        }
        with patch(
            "app.routes.auth.get_login_wallpaper",
            return_value=wallpaper,
        ):
            page = self.client.get("/login")
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": self._csrf_token(page),
                    "username": "invalid",
                    "password": "invalid",
                },
            )

        self.assertEqual(response.status_code, 401)
        self.assertIn("login-page--wallpaper", response.text)
        self.assertIn(wallpaper["tmdb_url"], response.text)

    def test_csrf_failure_uses_cached_wallpaper_without_refreshing(self):
        wallpaper = {
            "tmdb_id": 22,
            "title": "流光之城",
            "image_url": "https://image.tmdb.org/t/p/w1280/city.jpg",
            "tmdb_url": "https://www.themoviedb.org/movie/22",
        }
        with patch(
            "app.modules.login_wallpaper.get_login_wallpaper",
            return_value=wallpaper,
        ) as get_wallpaper:
            response = self.client.post(
                "/login",
                data={"username": "invalid", "password": "invalid"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertIn("login-page--wallpaper", response.text)
        get_wallpaper.assert_called_once_with(allow_refresh=False)

    def test_default_login_and_settings_contract_remain_available(self):
        with patch("app.routes.auth.get_login_wallpaper", return_value=None):
            login = self.client.get("/login")

        settings = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "templates"
            / "settings.html"
        ).read_text(encoding="utf-8")
        css = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "static"
            / "css"
            / "main.css"
        ).read_text(encoding="utf-8")

        self.assertNotIn("login-page--wallpaper", login.text)
        self.assertIn('data-key="LOGIN_WALLPAPER_MODE"', settings)
        self.assertIn('<option value="default">当前默认</option>', settings)
        self.assertIn('<option value="tmdb">TMDB 每日电影</option>', settings)
        self.assertIn(".login-wallpaper-link", css)
        self.assertIn("https://image.tmdb.org", login.headers["content-security-policy"])

    def test_wallpaper_mode_uses_full_bleed_cinematic_layout(self):
        css = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "static"
            / "css"
            / "main.css"
        ).read_text(encoding="utf-8")

        self.assertIn(".login-page--wallpaper {", css)
        self.assertIn("background-image:", css)
        self.assertIn("background-size: cover", css)
        self.assertIn(
            ".login-page--wallpaper .login-stage::before,\n"
            ".login-page--wallpaper .login-stage::after { display: none; }",
            css,
        )
        self.assertIn(
            ".login-page--wallpaper .login-index,\n"
            ".login-page--wallpaper .login-stage-copy,\n"
            ".login-page--wallpaper .login-signal { display: none; }",
            css,
        )
        self.assertIn(
            ".login-page--wallpaper .login-stage-wordmark { display: none; }",
            css,
        )
        self.assertIn(
            "grid-template-columns: minmax(0,1fr) clamp(380px,29vw,440px)",
            css,
        )
        self.assertIn("backdrop-filter: blur(20px)", css)
        self.assertIn("min-height: 100vh", css)
        self.assertIn("border-radius: 0", css)
        self.assertIn(
            ".login-page--wallpaper .login-card > form",
            css,
        )
        self.assertIn(
            ".login-page--wallpaper .login-card { min-height: auto;",
            css,
        )
        self.assertIn(
            ".login-page--wallpaper .login-card input:-webkit-autofill",
            css,
        )
        wallpaper_card_rule = css.split(
            ".login-page--wallpaper .login-card {", 1
        )[1].split("}", 1)[0]
        self.assertIn("--input-focus: #0e1314", wallpaper_card_rule)
        self.assertIn("-webkit-text-fill-color: #f1f5f2", css)
        self.assertIn(
            "-webkit-box-shadow: 0 0 0 1000px #0c1011 inset",
            css,
        )

    def test_config_api_enforces_tmdb_requirement(self):
        headers = self._authenticated_headers()
        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": "" if key == "TMDB_API_KEY" else default,
        ):
            rejected = self.client.post(
                "/api/config",
                headers=headers,
                json={"LOGIN_WALLPAPER_MODE": "tmdb"},
            )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("请先配置 TMDB API Key", rejected.json()["error"])

        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": "saved-key" if key == "TMDB_API_KEY" else default,
        ), patch("app.routes.api.config.set_and_save") as save:
            accepted = self.client.post(
                "/api/config",
                headers=headers,
                json={"LOGIN_WALLPAPER_MODE": "tmdb"},
            )
        self.assertEqual(accepted.status_code, 200)
        save.assert_called_once_with({"LOGIN_WALLPAPER_MODE": "tmdb"})
