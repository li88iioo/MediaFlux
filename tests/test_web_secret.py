from __future__ import annotations

from starlette.middleware.sessions import SessionMiddleware

import errno
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        first_run.refresh_startup_state()
        if os.name == "nt":
            permissions_patcher = patch.object(config, "_apply_private_permissions")
            permissions_patcher.start()
            self.addCleanup(permissions_patcher.stop)
        self.config = config
        self.first_run = first_run
        self.web_secret = web_secret

    def _legacy_installation(self):
        """已有旧库缺少 secret；不能通过新安装事务覆盖旧管理员来造夹具。"""
        from app import database
        self.paths.config_dir.mkdir(parents=True)
        self.paths.env_file.write_text(
            'MEDIAFLUX_INITIALIZED=1\nENV_WEB_PASSPORT=admin\nENV_WEB_PASSWORD=correct-horse\n',
            encoding='utf-8',
        )
        self._reload_modules()
        database.init_db()

    def test_fresh_production_keeps_the_same_session_secret_across_setup_and_cold_login(self):
        import re

        from fastapi.testclient import TestClient
        os.environ['APP_ENV'] = 'production'
        self._reload_modules()
        from app.main import create_app
        from app.routes.discovery_image import _serializer, encode_poster_token
        app = create_app()
        middleware_secret = next(m.kwargs['secret_key'] for m in app.user_middleware
                                 if m.cls is SessionMiddleware)
        assert isinstance(middleware_secret, str)
        self.assertEqual(app.state.setup_secret_key, middleware_secret)
        with TestClient(app) as client:
            page = client.get('/setup')
            self.assertEqual(page.status_code, 200)
            self.assertFalse(self.paths.env_file.exists())
            self.assertFalse(self.paths.database_path.exists())
            self.assertFalse((self.paths.config_dir / '.web-secret-key').exists())
            search_result = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
            assert search_result is not None
            token = search_result.group(1)
            response = client.post('/setup', data={
                'username':'admin', 'password':'correct-horse',
                'password_confirm':'correct-horse', 'csrf_token':token,
            }, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers['location'], '/login')
            self.assertEqual(self.config._read_env_file()['WEB_SECRET_KEY'], middleware_secret)
            page = client.get('/login')
            search_result_2 = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
            assert search_result_2 is not None
            token = search_result_2.group(1)
            response = client.post('/login', data={'username':'admin', 'password':'correct-horse',
                                                  'csrf_token':token}, follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(client.get('/').status_code, 200)
            cookies = dict(client.cookies)
        self.assertFalse((self.paths.config_dir / '.web-secret-key').exists())
        self._reload_modules()
        self.assertEqual(self.web_secret.get_web_secret(), middleware_secret)
        self.assertEqual(_serializer().secret_key, middleware_secret.encode())
        assert isinstance(encode_poster_token('tmdb', 'poster.jpg'), str)
        with TestClient(create_app()) as client:
            client.cookies.update(cookies)
            self.assertEqual(client.get('/', follow_redirects=False).status_code, 200)

    def test_fallback_secret_survives_module_reload_and_is_private(self):
        first = self.web_secret.get_web_secret()
        fallback = self.paths.config_dir / ".web-secret-key"
        self.assertTrue(fallback.is_file())
        if os.name == "posix":
            self.assertEqual(fallback.stat().st_mode & 0o777, 0o600)

        importlib.reload(self.web_secret)
        self.assertEqual(self.web_secret.get_web_secret(), first)

    def test_fallback_secret_uses_noreplace_when_hardlinks_are_unsupported(self):
        with patch("os.link", side_effect=OSError(errno.EPERM, "not supported")):
            secret = self.web_secret.get_web_secret()
        fallback = self.paths.config_dir / ".web-secret-key"
        self.assertEqual(self.web_secret._read_fallback_secret(fallback), secret)
        if os.name == "posix":
            self.assertEqual(fallback.stat().st_mode & 0o777, 0o600)

    def test_existing_development_install_without_secret_persists_generated_secret(self):
        self._legacy_installation()
        from app.main import create_app
        create_app()
        self.assertTrue(self.config._read_env_file()['WEB_SECRET_KEY'])

    def test_existing_production_install_without_secret_generates_and_persists_one(self):
        os.environ['APP_ENV'] = 'production'
        self._legacy_installation()
        from app.main import create_app
        create_app()
        values = self.config._read_env_file()
        self.assertGreaterEqual(len(values['WEB_SECRET_KEY']), 32)

    def test_external_secret_is_used_for_session_and_discovery_signing(self):
        os.environ["WEB_SECRET_KEY"] = "external-secret"
        self._reload_modules()
        from tests.test_setup_bootstrap_support import initialize_paths
        initialize_paths(self.paths, {'ENV_WEB_PASSPORT': 'admin', 'ENV_WEB_PASSWORD': 'correct-horse', 'WEB_SECRET_KEY': 'external-secret'})
        self._reload_modules()
        from app.main import create_app
        from app.routes.discovery_image import _serializer

        app = create_app()
        middleware_secret = next(
            middleware.kwargs["secret_key"]
            for middleware in app.user_middleware
            if middleware.cls is SessionMiddleware
        )
        self.assertEqual(middleware_secret, "external-secret")
        self.assertEqual(_serializer().secret_key, b"external-secret")


if __name__ == "__main__":
    unittest.main()
