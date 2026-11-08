from __future__ import annotations

import errno
import importlib
import os
import re
import stat
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.runtime_paths import RuntimePaths, configure_runtime_paths


class FirstRunTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import config, database, runtime_paths
        from app.modules import first_run

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ))
        stack.callback(configure_runtime_paths, runtime_paths._configured_paths)
        # reload 原位修改模块字典；连同完整环境恢复实际调用前的绑定。
        for module in (config, database, first_run):
            stack.enter_context(patch.dict(module.__dict__))
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
        for key in (
            "ENV_WEB_PASSPORT",
            "ENV_WEB_PASSWORD",
            "WEB_HOST",
            "MEDIAFLUX_INITIALIZED",
            "MEDIAFLUX_TEST_MODE",
            "MEDIAFLUX_TEST_DB_PATH",
            "WEB_SECRET_KEY",
            "APP_ENV",
            "MEDIAFLUX_CONTAINER",
            "MEDIAFLUX_ALLOW_REMOTE_SETUP",
        ):
            os.environ.pop(key, None)
        os.environ["MEDIAFLUX_TEST_MODE"] = "0"
        configure_runtime_paths(self.paths)
        from app.security import clear_setup_failures
        clear_setup_failures("testclient")
        self._reload_runtime_modules()

    def tearDown(self) -> None:
        from app.security import clear_setup_failures
        clear_setup_failures("testclient")

    def _reload_runtime_modules(
        self,
        paths: RuntimePaths | None = None,
    ) -> None:
        import app.config as config
        import app.database as database
        import app.modules.first_run as first_run

        importlib.reload(config)
        importlib.reload(database)
        database.configure_database((paths or self.paths).database_path, test_mode=False)
        importlib.reload(first_run)
        first_run._reset_startup_state_for_tests()
        self.config = config
        self.database = database
        self.first_run = first_run

    def _new_client(self) -> TestClient:
        from app.main import create_app

        return TestClient(create_app(), raise_server_exceptions=False)

    @staticmethod
    def _csrf_token(response) -> str:
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def _setup_page(self, client: TestClient):
        response = client.get("/setup")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertRegex(response.text, r"/static/js/lucide\.min\.js\?v=[0-9a-f]{16}")
        self.assertRegex(response.text, r"/static/js/app\.js\?v=[0-9a-f]{16}")
        return response


    def _initialize(self, username='admin', password='correct-horse'):
        return self.first_run.initialize_admin(username, password)

    def _web(self, username='admin', password='correct-horse', inspect=None):
        from tests.test_setup_bootstrap_support import initialize_web_paths
        result = initialize_web_paths(self.paths, username, password, inspect)
        self._assert_stable()
        return result

    @staticmethod
    def _origin():
        return {'Origin':'http://127.0.0.1:1258'}

    def _fields(self, page):
        return {'username':'admin','password':'correct-horse','password_confirm':'correct-horse',
                'csrf_token':self._csrf_token(page)}

    def _assert_unpublished(self):
        self.assertFalse(self.paths.env_file.exists())
        self.assertFalse(self.paths.database_path.exists())
        self.assertFalse((self.paths.config_dir / '.web-secret-key').exists())

    def _fence(self):
        import json
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect('file:' + str(self.paths.database_path) + '?mode=ro', uri=True)) as conn:
            self.assertEqual(Path(conn.execute('PRAGMA database_list').fetchone()[2]), self.paths.database_path)
            return json.loads(conn.execute("SELECT value FROM settings_kv WHERE key='agent.runtime_fence.v1'").fetchone()[0])

    def _assert_stable(self):
        self.assertEqual(self._fence()['phase'], 'stable')

    def _assert_failed_installation(self):
        from app.agent.feature_gate import RuntimeFenceError
        self.assertEqual(self._fence()['phase'], 'changing')
        before = self.paths.env_file.read_bytes()
        with self.assertRaises(self.first_run.InitializationError):
            self._initialize()
        self._reload_runtime_modules()
        with self.assertRaises(RuntimeFenceError):
            self._new_client()
        self.assertEqual(self.paths.env_file.read_bytes(), before)
        self.assertEqual(self._fence()['phase'], 'changing')

    def _login(self, client, username='admin', password='correct-horse'):
        page = client.get('/login')
        self.assertEqual(page.status_code, 200)
        response = client.post('/login', data={'username':username,'password':password,'csrf_token':self._csrf_token(page)}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['location'], '/')

    def test_initialize_admin_is_the_single_installation_entry_and_cannot_overwrite(self):
        self.assertIsNone(self._initialize())
        self.assertFalse(self.first_run.needs_initialization())
        self._assert_stable()
        before = self.paths.env_file.read_bytes(), self.paths.database_path.stat().st_ino
        with self.assertRaises(self.first_run.InitializationError):
            self._initialize('replacement', 'another-password')
        self.assertEqual(before, (self.paths.env_file.read_bytes(), self.paths.database_path.stat().st_ino))
        with self._new_client() as client:
            self._login(client)

    def test_empty_data_dir_serves_setup_without_creating_schema_or_configuration(self):
        self.assertFalse(self.paths.config_dir.exists())
        self.assertTrue(self.first_run.needs_initialization())
        with self._new_client() as client:
            response = client.get('/login', follow_redirects=False)
            self.assertEqual(client.get('/readyz').status_code, 503)
            self.assertEqual(client.get('/healthz').status_code, 200)
            page = self._setup_page(client)
            self.assertIn('class="login-form setup-form"', page.text)
            self.assertNotIn('name="setup_code"', page.text)
            self.assertNotIn('--confirm-new-installation', page.text)
            self._assert_unpublished()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['location'], '/setup')
        self._assert_unpublished()

    def test_fresh_login_submit_cannot_initialize_or_authenticate(self):
        with self._new_client() as client:
            response = client.post('/login', data={'username':'admin','password':'correct-horse'}, follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers['location'], '/setup')
        self._assert_unpublished()

    def test_incomplete_artifacts_never_publish_without_valid_setup(self):
        cases = {'empty_user_env':'', 'ordinary_config':'TMDB_API_KEY=kept\nWEB_HOST=0.0.0.0\n', 'username_only':None, 'password_only':None, 'initialized_flag_only':None, 'database_only':None}
        for name, content in cases.items():
            with self.subTest(name=name):
                self.paths.config_dir.mkdir(parents=True, exist_ok=True)
                if content is not None:
                    self.paths.env_file.write_text(content)
                key = {'username_only':'ENV_WEB_PASSPORT','password_only':'ENV_WEB_PASSWORD','initialized_flag_only':'MEDIAFLUX_INITIALIZED'}.get(name)
                if key:
                    os.environ[key] = '1' if key == 'MEDIAFLUX_INITIALIZED' else 'partial-value'
                if name == 'database_only':
                    self.paths.database_path.parent.mkdir(parents=True, exist_ok=True)
                    self.paths.database_path.touch()
                before = self.paths.env_file.read_bytes() if self.paths.env_file.exists() else None
                self._reload_runtime_modules()
                self.assertTrue(self.first_run.needs_initialization())
                from app.main import _resolve_bind_host
                self.assertEqual(_resolve_bind_host(None), '127.0.0.1')
                if name == 'database_only':
                    from app.agent.feature_gate import RuntimeFenceError
                    with self.assertRaises(RuntimeFenceError):
                        self._new_client()
                    self.assertEqual(self.paths.database_path.read_bytes(), b'')
                else:
                    with self._new_client() as client:
                        self.assertEqual(client.get('/login', follow_redirects=False).headers['location'], '/setup')
                        self.assertEqual(client.post('/setup', data={}).status_code, 403)
                        self.assertEqual(client.get('/readyz').status_code, 503)
                    self.assertFalse(self.paths.database_path.exists())
                self.assertEqual(self.paths.env_file.read_bytes() if self.paths.env_file.exists() else None, before)
                if key:
                    os.environ.pop(key)
                self.paths.env_file.unlink(missing_ok=True)
                self.paths.database_path.unlink(missing_ok=True)

    def test_remote_first_run_binding_requires_explicit_opt_in(self):
        self._reload_runtime_modules()

        with self.assertRaises(self.first_run.UnsafeFirstRunBindingError):
            self.first_run.resolve_bind_host("0.0.0.0")

        os.environ["MEDIAFLUX_ALLOW_REMOTE_SETUP"] = "1"
        self.assertEqual(self.first_run.resolve_bind_host("0.0.0.0"), "0.0.0.0")

    def test_official_container_defaults_fresh_setup_to_all_interfaces(self):
        os.environ["MEDIAFLUX_CONTAINER"] = "1"
        self._reload_runtime_modules()

        self.assertEqual(self.first_run.resolve_bind_host(None), "0.0.0.0")
        self.assertEqual(self.first_run.resolve_bind_host("0.0.0.0"), "0.0.0.0")

    def test_only_explicitly_initialized_credentials_skip_setup(self):
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        self.paths.env_file.write_text(
            "MEDIAFLUX_INITIALIZED=1\n"
            "ENV_WEB_PASSPORT=admin\n"
            "ENV_WEB_PASSWORD=strong-password\n",
            encoding="utf-8",
        )
        self._reload_runtime_modules()
        self.assertFalse(self.first_run.needs_initialization())
        self.paths.env_file.unlink()

        os.environ["MEDIAFLUX_INITIALIZED"] = "1"
        os.environ["ENV_WEB_PASSPORT"] = "deployed-admin"
        os.environ["ENV_WEB_PASSWORD"] = "deployed-password"
        self._reload_runtime_modules()
        self.assertFalse(self.first_run.needs_initialization())

    def test_credentials_without_initialization_marker_enter_setup(self):
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        self.paths.env_file.write_text(
            "ENV_WEB_PASSPORT=admin\nENV_WEB_PASSWORD=strong-password\n",
            encoding="utf-8",
        )
        self._reload_runtime_modules()
        self.assertTrue(self.first_run.needs_initialization())
        before = self.paths.env_file.read_bytes()
        with self.assertRaises(self.first_run.InitializationError):
            self._initialize('replacement', 'replacement-password')
        self.assertEqual(self.paths.env_file.read_bytes(), before)
        self.assertFalse(self.paths.database_path.exists())

    def test_runtime_path_reset_uses_a_new_startup_snapshot(self):
        self.paths.config_dir.mkdir(parents=True)
        self.paths.env_file.write_text(
            "MEDIAFLUX_INITIALIZED=1\n"
            "ENV_WEB_PASSPORT=admin\nENV_WEB_PASSWORD=strong-password\n",
            encoding="utf-8",
        )
        self._reload_runtime_modules()
        self.assertFalse(self.first_run.needs_initialization())

        root = Path(self.temp.name) / "second"
        second_paths = RuntimePaths(
            program_dir=root / "program",
            data_dir=root / "data",
            config_dir=root / "config",
            cache_dir=root / "cache",
            log_dir=root / "logs",
            strm_dir=root / "strm-data",
            trash_dir=root / "trash",
        )
        configure_runtime_paths(second_paths)
        os.environ.pop("ENV_WEB_PASSPORT", None)
        os.environ.pop("ENV_WEB_PASSWORD", None)
        self._reload_runtime_modules(second_paths)

        self.assertTrue(self.first_run.needs_initialization())

    def test_live_setup_page_avoids_username_autofocus_to_prevent_mobile_keyboard_layout_shift(self):
        def inspect(page, client):
            self._setup_page(client)
            self.assertNotIn('id="setup-username" type="text" name="username" class="form-input" placeholder="admin" value="" autocomplete="username" autofocus', page.text)
        self._web(inspect=inspect)

    def test_setup_avoids_wallpaper_and_same_process_login_keeps_wallpaper(self):
        with patch('app.routes.auth.get_login_wallpaper', return_value=None) as wallpaper:
            with self._new_client() as client:
                page = self._setup_page(client)
                wallpaper.assert_not_called()
                response = client.post('/setup', data=self._fields(page), follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                wallpaper.assert_not_called()
                self.assertEqual(client.get('/login').status_code, 200)
                wallpaper.assert_called_once()

    def test_setup_post_requires_csrf_and_keeps_the_form_available(self):
        def inspect(page, client):
            fields = self._fields(page)
            fields.pop('csrf_token')
            response = client.post('/setup', data=fields, headers=self._origin())
            self.assertEqual(response.status_code, 403)
            self._assert_unpublished()
            page = self._setup_page(client)
            self.assertIn('class="login-form setup-form"', page.text)
            fields = self._fields(page)
            client.cookies.clear()
            self.assertEqual(client.post('/setup', data=fields).status_code, 403)
            self._assert_unpublished()
        self._web(inspect=inspect)

    def test_live_setup_post_validates_fields_without_writing_configuration(self):
        invalid_forms = (
            {'username':'   '}, {'username':'admin\nname'},
            {'password':'short','password_confirm':'short'},
            {'password_confirm':'different-horse'},
            {'password':'bad\npassword','password_confirm':'bad\npassword'},
        )
        def inspect(page, client):
            for invalid in invalid_forms:
                with self.subTest(fields=tuple(invalid)):
                    response = client.post('/setup', data={**self._fields(page), **invalid}, headers=self._origin())
                    self.assertEqual(response.status_code, 400)
                    self._assert_unpublished()
                    self.assertIn('class="login-form setup-form"', self._setup_page(client).text)
                    from app.security import clear_setup_failures
                    clear_setup_failures('testclient')
        self._web(inspect=inspect)

    def test_initialize_admin_rejects_control_characters_before_username_stripping(self):
        with self.assertRaises(ValueError):
            self._initialize('admin\r\n')
        self._assert_unpublished()

    def test_initialize_admin_rejects_all_splitline_separators_without_publishing_file(self):
        for separator in ('\v','\f','\x1c','\x1d','\x1e','\x85','\u2028','\u2029'):
            with self.subTest(separator=repr(separator)):
                with self.assertRaises(ValueError):
                    self._initialize(f'admin{separator}name', f'correct{separator}horse')
                self._assert_unpublished()

    def test_initialize_admin_uses_owned_paths_not_early_config_module_path(self):
        other_env = Path(self.temp.name) / 'early-config/config/user.env'
        with patch.object(self.config, 'ENV_FILE', other_env):
            self._initialize()
        self.assertTrue(self.paths.env_file.exists())
        self.assertFalse(other_env.exists())
        self._assert_stable()

    def test_failed_canonical_publish_does_not_mark_initialized_and_can_retry(self):
        with patch('app.config._publish_noreplace', side_effect=OSError('publish failed')):
            with self.assertRaises(self.first_run.InitializationError):
                self._initialize()
        self._assert_unpublished()
        self._initialize()
        self.assertTrue(self.paths.env_file.exists())
        self._assert_stable()

    def test_initialize_admin_preserves_unrelated_config_and_replaces_partial_credentials(self):
        self.paths.config_dir.mkdir(parents=True)
        self.paths.env_file.write_text('TMDB_API_KEY=keep-me\nENV_WEB_PASSPORT=stale-only\n', encoding='utf-8')
        self._reload_runtime_modules()
        self._initialize('new-admin')
        values = self.config._read_env_file(self.paths.env_file)
        self.assertEqual(values['TMDB_API_KEY'], 'keep-me')
        self.assertEqual(values['ENV_WEB_PASSPORT'], 'new-admin')
        self.assertEqual(values['ENV_WEB_PASSWORD'], 'correct-horse')
        self.assertEqual(values['MEDIAFLUX_INITIALIZED'], '1')
        self._assert_stable()

    def test_initialize_admin_uses_frozen_env_cas_not_the_latest_file(self):
        self.paths.config_dir.mkdir(parents=True)
        self.paths.env_file.write_bytes(b'TMDB_API_KEY=original\n')
        self._reload_runtime_modules()
        self.assertTrue(self.first_run.needs_initialization())
        competitor = b'TMDB_API_KEY=competitor\nENV_WEB_PASSPORT=other\nENV_WEB_PASSWORD=other-password\n'
        self.paths.env_file.write_bytes(competitor)
        with self.assertRaises(self.first_run.InitializationError):
            self._initialize()
        self.assertTrue(self.first_run.needs_initialization())
        self.assertEqual(self.paths.env_file.read_bytes(), competitor)
        self.assertFalse(self.paths.database_path.exists())

    def test_complete_competitor_is_preserved_but_never_auto_admitted(self):
        competitor = b'ENV_WEB_PASSPORT=other\nENV_WEB_PASSWORD=other-password\nMEDIAFLUX_INITIALIZED=1\n'
        real_publish = self.config._publish_noreplace
        def competing_publish(temporary, target):
            if target == self.paths.env_file:
                target.write_bytes(competitor)
                raise FileExistsError(errno.EEXIST, 'exists', str(target))
            return real_publish(temporary, target)
        with patch('app.config._publish_noreplace', side_effect=competing_publish):
            with self.assertRaises(self.first_run.InitializationError):
                self._initialize()
        self.assertEqual(self.paths.env_file.read_bytes(), competitor)
        self._assert_failed_installation()

    def test_partial_competitor_is_preserved_and_failed_installation_is_not_admitted(self):
        competitor = b'TMDB_API_KEY=competitor\nENV_WEB_PASSPORT=partial\n'
        real_publish = self.config._publish_noreplace
        def competing_publish(temporary, target):
            if target == self.paths.env_file:
                target.write_bytes(competitor)
                raise FileExistsError(errno.EEXIST, 'exists', str(target))
            return real_publish(temporary, target)
        with patch('app.config._publish_noreplace', side_effect=competing_publish):
            with self.assertRaises(self.first_run.InitializationError):
                self._initialize()
        self.assertEqual(self.paths.env_file.read_bytes(), competitor)
        self._assert_failed_installation()

    def test_initialize_admin_writes_private_configuration_without_network_binding(self):
        self._initialize('  admin  ')
        self._reload_runtime_modules()
        self.assertFalse(self.first_run.needs_initialization())
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.paths.env_file.stat().st_mode), 0o600)
        values = self.config._read_env_file()
        self.assertEqual({key:value for key,value in values.items() if key != 'WEB_SECRET_KEY'}, {'ENV_WEB_PASSPORT':'admin','ENV_WEB_PASSWORD':'correct-horse','MEDIAFLUX_INITIALIZED':'1'})
        self.assertTrue(values['WEB_SECRET_KEY'])
        self._assert_stable()

    def test_initialize_admin_preserves_password_whitespace_in_user_env(self):
        password = '  correct-$horse"  '
        self._initialize('admin', password)
        self.assertEqual(self.config._read_env_file()['ENV_WEB_PASSWORD'], password)
        self._assert_stable()
        self._reload_runtime_modules()
        with self._new_client() as client:
            self._login(client, password=password)

    def test_initialize_admin_only_allows_one_concurrent_creator(self):
        self.paths.ensure_writable_dirs()
        barrier = threading.Barrier(3)
        errors = []
        successes = []
        def initialize(username):
            barrier.wait(timeout=5)
            try:
                self._initialize(username)
                successes.append(username)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=initialize, args=(name,)) for name in ('one','two')]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(successes), 1)
        self.assertEqual(self.config._read_env_file()['ENV_WEB_PASSPORT'], successes[0])
        self._assert_stable()
        before = self.paths.env_file.read_bytes()
        with self.assertRaises(self.first_run.InitializationError):
            self._initialize('three')
        self.assertEqual(self.paths.env_file.read_bytes(), before)

    def test_live_setup_immediately_activates_and_logs_in_without_network_configuration(self):
        with self._new_client() as client:
            page = self._setup_page(client)
            self.assertNotIn('name="allow_lan"', page.text)
            self.assertEqual(client.get('/readyz').status_code, 503)
            self._assert_unpublished()
            response = client.post('/setup', data={**self._fields(page), 'allow_lan':'1'},
                                   follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers['location'], '/login')
            self.assertEqual(client.get('/readyz').status_code, 200)
            self.assertFalse(self.first_run.needs_initialization())
            self._login(client)
            self.assertEqual(client.get('/').status_code, 200)
            before = self.paths.env_file.read_bytes()
            response = client.post('/setup', data={**self._fields(client.get('/')), 'username':'attacker'},
                                   follow_redirects=False)
            self.assertIn(response.status_code, (302, 303))
            self.assertEqual(self.paths.env_file.read_bytes(), before)
        self.assertNotIn('WEB_HOST', self.config._read_env_file())
        self._assert_stable()

    def test_removed_network_applying_route_returns_not_found(self):
        with self._new_client() as client:
            response = client.get("/network-applying", follow_redirects=False)

        self.assertEqual(response.status_code, 404)

    def test_unicode_credentials_survive_setup_cold_login_logout_and_login(self):
        username, password = '管理员', '密码足够长度1234'
        self._web(username, password)
        self._reload_runtime_modules()
        with self._new_client() as client:
            self._login(client, username, password)
            dashboard = client.get('/')
            logout = client.post('/logout', data={'csrf_token':self._csrf_token(dashboard)}, follow_redirects=False)
            self.assertEqual(logout.status_code, 302)
            self.assertEqual(logout.headers['location'], '/login')
            self._login(client, username, password)

    def test_setup_rate_limit_does_not_block_login_and_preserves_unpublished_state(self):
        from app.security import _SETUP_MAX_FAILURES, clear_setup_failures, login_rate_limited
        with self._new_client() as client:
            page = self._setup_page(client)
            invalid = {**self._fields(page), 'password_confirm':'mismatch'}
            for _ in range(_SETUP_MAX_FAILURES):
                self.assertEqual(client.post('/setup', data=invalid).status_code, 400)
            response = client.post('/setup', data=self._fields(page))
            self.assertEqual(response.status_code, 429)
            self.assertFalse(login_rate_limited('testclient'))
            self._assert_unpublished()
            clear_setup_failures('testclient')
            response = client.post('/setup', data=self._fields(self._setup_page(client)), follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self._login(client)
            self.assertEqual(client.get('/readyz').status_code, 200)


    def test_corrupt_non_utf8_config_is_preserved_and_all_initialization_refused(self):
        self.paths.config_dir.mkdir(parents=True)
        broken = b'\xff\xfeBROKEN'
        self.paths.env_file.write_bytes(broken)
        self._reload_runtime_modules()
        with self._new_client() as client:
            page = client.get('/setup')
            self.assertEqual(page.status_code, 200)
            self.assertIn('user.env', page.text)
            self.assertIn('恢复', page.text)
            self.assertEqual(client.post('/setup', data=self._fields(page)).status_code, 409)
        with self.assertRaises(self.first_run.InitializationError):
            self._initialize()
        self.assertEqual(self.paths.env_file.read_bytes(), broken)
        self.assertFalse(self.paths.database_path.exists())

    @unittest.skipIf(os.name == "nt", "POSIX 链接安全 Web 回归")
    def test_symlink_config_never_initializes_or_touches_target(self):
        self.paths.config_dir.mkdir(parents=True)
        external = Path(self.temp.name) / 'outside.env'
        external.write_text('TMDB_API_KEY=outside\n', encoding='utf-8')
        self.paths.env_file.symlink_to(external)
        self._reload_runtime_modules()
        with self._new_client() as client:
            page = client.get('/setup')
            self.assertIn('user.env', page.text)
            self.assertEqual(client.post('/setup', data=self._fields(page)).status_code, 409)
        with self.assertRaises(self.first_run.InitializationError):
            self._initialize()
        self.assertEqual(external.read_text(encoding='utf-8'), 'TMDB_API_KEY=outside\n')
        self.assertTrue(self.paths.env_file.is_symlink())
        self.assertFalse(self.paths.database_path.exists())

    def test_permission_failure_never_promotes_competitor_credentials(self):
        self.paths.config_dir.mkdir(parents=True)
        self.paths.env_file.write_text('TMDB_API_KEY=keep-me\nENV_WEB_PASSPORT=partial\n')
        self._reload_runtime_modules()
        competitor = b'ENV_WEB_PASSPORT=other\nENV_WEB_PASSWORD=other-password\nWEB_SECRET_KEY=competitor-secret\nMEDIAFLUX_INITIALIZED=1\n'
        calls = []
        def fail_permissions(path):
            calls.append(path)
            self.paths.env_file.write_bytes(competitor)
            raise self.config.AtomicPublishError('private permissions failed')
        with (
            patch('app.config._apply_private_permissions', side_effect=fail_permissions),
            self.assertRaises(self.first_run.InitializationError),
        ):
            self._initialize()
        self.assertTrue(calls)
        self.assertEqual(self.paths.env_file.read_bytes(), competitor)
        self._assert_failed_installation()

    def test_successful_publish_verifies_and_syncs_physical_env(self):
        from app.modules import backup
        events: list[tuple] = []
        real_fsync = os.fsync
        real_read = self.config._read_verified_file_bytes
        real_sync = backup._fsync_directory
        def observe_read(path, **kwargs):
            payload = real_read(path, **kwargs)
            if path == self.paths.env_file:
                events.append(('read', payload))
            return payload
        def observe_sync(fd):
            result = real_fsync(fd)
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode):
                events.append(('file_sync', info.st_dev, info.st_ino))
            return result
        def observe_directory(path):
            result = real_sync(path)
            if path == self.paths.config_dir and self.paths.env_file.exists():
                events.append(('directory_sync', None))
            return result
        with patch.object(self.config, '_read_verified_file_bytes', side_effect=observe_read), \
             patch('os.fsync', side_effect=observe_sync), \
             patch.object(backup, '_fsync_directory', side_effect=observe_directory):
            self._initialize()
        payload, info = self.paths.env_file.read_bytes(), self.paths.env_file.stat()
        file_sync = ('file_sync', info.st_dev, info.st_ino)
        self.assertIn(file_sync, events)
        self.assertIn(('read', payload), events)
        read_index = events.index(('read', payload))
        self.assertLess(events.index(file_sync), read_index)
        self.assertIn(('directory_sync', None), events[read_index + 1:])
        self._assert_stable()
        self.assertFalse(self.first_run.needs_initialization())


if __name__ == "__main__":
    unittest.main()


def test_first_run_fixture_restores_published_environment_and_runtime_state():
    from app import config, database, runtime_paths

    environment = dict(os.environ)
    database_scope = (database.DB_PATH, database._configured_test_mode)
    configured_paths = runtime_paths._configured_paths
    externally_managed = config.has_external_override("TMDB_API_KEY")
    result = unittest.TestResult()
    FirstRunTests(
        "test_initialize_admin_preserves_unrelated_config_and_replaces_partial_credentials"
    ).run(result)
    assert result.wasSuccessful(), (result.errors, result.failures)
    assert dict(os.environ) == environment
    assert config.has_external_override("TMDB_API_KEY") == externally_managed
    assert (database.DB_PATH, database._configured_test_mode) == database_scope
    assert runtime_paths._configured_paths is configured_paths
