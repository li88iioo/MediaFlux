from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.runtime_paths import RuntimePaths, configure_runtime_paths


class WebSecretTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = RuntimePaths(
            program_dir=root / "program",
            data_dir=root / "data",
            config_dir=root / "config",
            cache_dir=root / "cache",
            log_dir=root / "logs",
            strm_dir=root / "strm-data",
            trash_dir=root / "trash",
        )
        self.environment = dict(os.environ)
        os.environ.clear()
        os.environ["MEDIAFLUX_TEST_MODE"] = "0"
        configure_runtime_paths(self.paths)
        self._reload_modules()

    def tearDown(self) -> None:
        configure_runtime_paths(None)
        os.environ.clear()
        os.environ.update(self.environment)
        self.temp.cleanup()

    def _reload_modules(self) -> None:
        import app.config as config
        import app.database as database
        import app.modules.first_run as first_run
        import app.modules.web_secret as web_secret

        importlib.reload(config)
        importlib.reload(database)
        importlib.reload(web_secret)
        importlib.reload(first_run)
        first_run._reset_startup_state_for_tests()
        if os.name == "nt":
            permissions_patcher = patch.object(config, "_apply_private_permissions")
            permissions_patcher.start()
            self.addCleanup(permissions_patcher.stop)
        self.config = config
        self.first_run = first_run
        self.web_secret = web_secret

    def test_fresh_production_uses_one_in_memory_secret_until_setup_persists_it(self):
        os.environ["APP_ENV"] = "production"
        self._reload_modules()
        from app.main import create_app
        from app.routes.discovery_image import encode_poster_token

        app = create_app()
        middleware_secret = next(
            middleware.kwargs["secret_key"]
            for middleware in app.user_middleware
            if middleware.cls.__name__ == "SessionMiddleware"
        )
        self.assertEqual(middleware_secret, self.web_secret.get_web_secret())
        self.assertIsInstance(encode_poster_token("tmdb", "poster.jpg"), str)

        self.first_run.initialize_admin("admin", "correct-horse")

        self.assertEqual(
            self.config._read_env_file(self.paths.env_file)["WEB_SECRET_KEY"],
            middleware_secret,
        )

    def test_fallback_secret_survives_module_reload_and_is_private(self):
        first = self.web_secret.get_web_secret()
        fallback = self.paths.config_dir / ".web-secret-key"
        self.assertTrue(fallback.is_file())
        if os.name == "posix":
            self.assertEqual(fallback.stat().st_mode & 0o777, 0o600)

        importlib.reload(self.web_secret)
        self.assertEqual(self.web_secret.get_web_secret(), first)

    def test_existing_development_install_without_secret_persists_generated_secret(self):
        self.paths.config_dir.mkdir(parents=True)
        self.paths.env_file.write_text(
            "MEDIAFLUX_INITIALIZED=1\n"
            "ENV_WEB_PASSPORT=admin\nENV_WEB_PASSWORD=correct-horse\n",
            encoding="utf-8",
        )
        self._reload_modules()
        from app.main import create_app

        create_app()

        self.assertTrue(self.config._read_env_file(self.paths.env_file)["WEB_SECRET_KEY"])

    def test_existing_production_install_without_secret_fails_closed(self):
        self.paths.config_dir.mkdir(parents=True)
        self.paths.env_file.write_text(
            "MEDIAFLUX_INITIALIZED=1\n"
            "ENV_WEB_PASSPORT=admin\nENV_WEB_PASSWORD=correct-horse\n",
            encoding="utf-8",
        )
        os.environ["APP_ENV"] = "production"
        self._reload_modules()
        from app.main import create_app

        with self.assertRaisesRegex(RuntimeError, "WEB_SECRET_KEY"):
            create_app()

    def test_external_secret_is_used_for_session_and_discovery_signing(self):
        os.environ["WEB_SECRET_KEY"] = "external-secret"
        self._reload_modules()
        from app.main import create_app
        from app.routes.discovery_image import _serializer

        app = create_app()
        middleware_secret = next(
            middleware.kwargs["secret_key"]
            for middleware in app.user_middleware
            if middleware.cls.__name__ == "SessionMiddleware"
        )
        self.assertEqual(middleware_secret, "external-secret")
        self.assertEqual(_serializer().secret_key, b"external-secret")


if __name__ == "__main__":
    unittest.main()
