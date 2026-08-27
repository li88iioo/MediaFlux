from __future__ import annotations

import errno
import importlib
import os
import re
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.runtime_paths import RuntimePaths, configure_runtime_paths


class FirstRunTests(unittest.TestCase):
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
        self.environment = {
            key: os.environ.get(key)
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
            )
        }
        for key in self.environment:
            os.environ.pop(key, None)
        os.environ["MEDIAFLUX_TEST_MODE"] = "0"
        configure_runtime_paths(self.paths)
        from app.security import clear_setup_failures
        clear_setup_failures("testclient")
        self._reload_runtime_modules()

    def tearDown(self) -> None:
        for key, value in self.environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from app.security import clear_setup_failures
        clear_setup_failures("testclient")
        configure_runtime_paths(None)
        self._reload_runtime_modules(restore_default_database=True)
        self.temp.cleanup()

    def _reload_runtime_modules(
        self,
        paths: RuntimePaths | None = None,
        *,
        restore_default_database: bool = False,
    ) -> None:
        import app.config as config
        import app.database as database
        import app.modules.first_run as first_run

        importlib.reload(config)
        importlib.reload(database)
        if not restore_default_database:
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
        self.assertIn("/static/js/lucide.min.js?v=20260816a", response.text)
        self.assertIn("/static/js/app.js?v=20260824a", response.text)
        return response

    def test_empty_data_dir_redirects_login_to_setup_after_lifespan_creates_schema(self):
        self.assertFalse(self.paths.config_dir.exists())
        self.assertTrue(self.first_run.needs_initialization())

        with self._new_client() as client:
            response = client.get("/login", follow_redirects=False)

        self.assertTrue(self.paths.database_path.exists())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/setup")

    def test_fresh_login_submit_redirects_to_setup_before_csrf_validation(self):
        with self._new_client() as client:
            response = client.post(
                "/login",
                data={"username": "admin", "password": "correct-horse"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/setup")

    def test_incomplete_installation_artifacts_enter_safe_recovery(self):
        cases = {
            "empty_user_env": lambda: (
                self.paths.config_dir.mkdir(parents=True, exist_ok=True),
                self.paths.env_file.write_text("", encoding="utf-8"),
            ),
            "ordinary_config": lambda: (
                self.paths.config_dir.mkdir(parents=True, exist_ok=True),
                self.paths.env_file.write_text(
                    "TMDB_API_KEY=kept\nWEB_HOST=0.0.0.0\n", encoding="utf-8"
                ),
            ),
            "username_only": lambda: os.environ.__setitem__("ENV_WEB_PASSPORT", "partial"),
            "password_only": lambda: os.environ.__setitem__("ENV_WEB_PASSWORD", "partial-password"),
            "initialized_flag_only": lambda: os.environ.__setitem__("MEDIAFLUX_INITIALIZED", "1"),
            "database_only": lambda: (
                self.paths.database_path.parent.mkdir(parents=True, exist_ok=True),
                self.paths.database_path.touch(),
            ),
        }
        for name, arrange in cases.items():
            with self.subTest(name=name):
                arrange()
                self._reload_runtime_modules()
                self.assertTrue(self.first_run.needs_initialization())
                from app.main import _resolve_bind_host

                self.assertEqual(_resolve_bind_host(None), "127.0.0.1")
                with self._new_client() as client:
                    login = client.get("/login", follow_redirects=False)
                self.assertEqual(login.status_code, 302)
                self.assertEqual(login.headers["location"], "/setup")
                os.environ.pop("MEDIAFLUX_INITIALIZED", None)
                os.environ.pop("ENV_WEB_PASSPORT", None)
                os.environ.pop("ENV_WEB_PASSWORD", None)
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

    def test_setup_page_avoids_username_autofocus_to_prevent_mobile_keyboard_layout_shift(self):
        with self._new_client() as client:
            response = client.get("/setup")

        self.assertNotIn('id="setup-username" type="text" name="username" class="form-input" placeholder="admin" value="" autocomplete="username" autofocus', response.text)

    def test_setup_page_does_not_request_login_wallpaper_and_login_keeps_wallpaper(self):
        with patch("app.routes.auth.get_login_wallpaper") as get_wallpaper:
            with self._new_client() as client:
                setup = client.get("/setup")
                self.assertEqual(setup.status_code, 200)
                get_wallpaper.assert_not_called()

                login = client.get("/login", follow_redirects=False)
                self.assertEqual(login.status_code, 302)

    def test_setup_post_requires_csrf_and_renders_setup_form(self):
        with self._new_client() as client:
            response = client.post(
                "/setup",
                data={"username": "admin", "password": "correct-horse", "password_confirm": "correct-horse"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertIn('class="login-form setup-form"', response.text)
        self.assertFalse(self.paths.env_file.exists())

    def test_setup_post_validates_fields_without_writing_configuration(self):
        invalid_forms = (
            {"username": "   ", "password": "correct-horse", "password_confirm": "correct-horse"},
            {"username": "admin\nname", "password": "correct-horse", "password_confirm": "correct-horse"},
            {"username": "admin", "password": "short", "password_confirm": "short"},
            {"username": "admin", "password": "correct-horse", "password_confirm": "different-horse"},
            {"username": "admin", "password": "bad\npassword", "password_confirm": "bad\npassword"},
        )
        with self._new_client() as client:
            for data in invalid_forms:
                with self.subTest(data=data):
                    page = self._setup_page(client)
                    response = client.post(
                        "/setup",
                        data={"csrf_token": self._csrf_token(page), **data},
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertIn('class="login-form setup-form"', response.text)
                    self.assertFalse(self.paths.env_file.exists())

    def test_initialize_admin_rejects_control_characters_before_username_stripping(self):
        with self.assertRaisesRegex(ValueError, "换行"):
            self.first_run.initialize_admin("admin\r\n", "correct-horse")

        self.assertFalse(self.paths.env_file.exists())

    def test_initialize_admin_rejects_all_splitline_separators_without_publishing_file(self):
        for separator in ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                os.environ.pop("ENV_WEB_PASSPORT", None)
                os.environ.pop("ENV_WEB_PASSWORD", None)
                self.paths.env_file.unlink(missing_ok=True)
                self.first_run._reset_startup_state_for_tests()
                with self.assertRaises(ValueError):
                    self.first_run.initialize_admin(
                        f"admin{separator}name",
                        f"correct{separator}horse",
                    )
                self.assertFalse(self.paths.env_file.exists())

    def test_initialize_admin_uses_snapshot_env_file_not_early_config_module_path(self):
        other_root = Path(self.temp.name) / "early-config"
        other_env = other_root / "config" / "user.env"
        with patch.object(self.config, "ENV_FILE", other_env):
            self.first_run.initialize_admin("admin", "correct-horse")

        self.assertTrue(self.paths.env_file.exists())
        self.assertFalse(other_env.exists())

    def test_failed_create_only_publish_leaves_no_final_and_setup_can_retry(self):
        with patch("app.config._publish_noreplace", side_effect=OSError("publish failed")):
            with self.assertRaises(Exception):
                self.first_run.initialize_admin("admin", "correct-horse")

        self.assertFalse(self.paths.env_file.exists())
        self.first_run.initialize_admin("admin", "correct-horse")
        self.assertTrue(self.paths.env_file.exists())

    def test_recovery_setup_preserves_unrelated_config_and_replaces_partial_credentials(self):
        self.paths.config_dir.mkdir(parents=True)
        self.paths.env_file.write_text(
            "TMDB_API_KEY=keep-me\nENV_WEB_PASSPORT=stale-only\n",
            encoding="utf-8",
        )
        self._reload_runtime_modules()

        self.first_run.initialize_admin("new-admin", "correct-horse")

        values = self.config._read_env_file(self.paths.env_file)
        self.assertEqual(values["TMDB_API_KEY"], "keep-me")
        self.assertEqual(values["ENV_WEB_PASSPORT"], "new-admin")
        self.assertEqual(values["ENV_WEB_PASSWORD"], "correct-horse")
        self.assertEqual(values["MEDIAFLUX_INITIALIZED"], "1")

    def test_recovery_setup_refuses_to_overwrite_file_changed_after_snapshot(self):
        self.paths.config_dir.mkdir(parents=True)
        self.paths.env_file.write_text("TMDB_API_KEY=original\n", encoding="utf-8")
        self._reload_runtime_modules()
        self.paths.env_file.write_text(
            "TMDB_API_KEY=competitor\nENV_WEB_PASSPORT=other\nENV_WEB_PASSWORD=other-password\n",
            encoding="utf-8",
        )

        with self.assertRaises(self.first_run.InitializationError):
            self.first_run.initialize_admin("admin", "correct-horse")

        self.assertIn("TMDB_API_KEY=competitor", self.paths.env_file.read_text(encoding="utf-8"))

    def test_create_only_competitor_with_complete_credentials_redirects_to_real_login(self):
        competitor = (
            b"ENV_WEB_PASSPORT=other\n"
            b"ENV_WEB_PASSWORD=other-password\n"
            b"MEDIAFLUX_INITIALIZED=1\n"
        )

        def competing_publish(_temporary: Path, target: Path) -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(competitor)
            raise FileExistsError(errno.EEXIST, "exists", str(target))

        with self._new_client() as client, patch(
            "app.config._publish_noreplace", side_effect=competing_publish
        ):
            page = self._setup_page(client)
            response = client.post(
                "/setup",
                data={
                    "csrf_token": self._csrf_token(page),
                    "username": "admin",
                    "password": "correct-horse",
                    "password_confirm": "correct-horse",
                },
                follow_redirects=False,
            )
            login = client.get("/login", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login")
        self.assertEqual(login.status_code, 200)

    def test_create_only_competitor_with_partial_config_stays_on_setup_with_error(self):
        competitor = b"TMDB_API_KEY=competitor\nENV_WEB_PASSPORT=partial\n"

        def competing_publish(_temporary: Path, target: Path) -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(competitor)
            raise FileExistsError(errno.EEXIST, "exists", str(target))

        with self._new_client() as client, patch(
            "app.config._publish_noreplace", side_effect=competing_publish
        ):
            page = self._setup_page(client)
            response = client.post(
                "/setup",
                data={
                    "csrf_token": self._csrf_token(page),
                    "username": "admin",
                    "password": "correct-horse",
                    "password_confirm": "correct-horse",
                },
                follow_redirects=False,
            )
            login = client.get("/login", follow_redirects=False)

        self.assertEqual(response.status_code, 409)
        self.assertIn("配置已发生变化", response.text)
        self.assertEqual(login.status_code, 302)
        self.assertEqual(login.headers["location"], "/setup")

    def test_initialize_admin_writes_private_configuration_without_network_binding(self):
        self.first_run.initialize_admin("  admin  ", "correct-horse")

        self.assertFalse(self.first_run.needs_initialization())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.paths.env_file.stat().st_mode), 0o600)
        values = self.config._read_env_file()
        self.assertEqual(
            {key: value for key, value in values.items() if key != "WEB_SECRET_KEY"},
            {
                "ENV_WEB_PASSPORT": "admin",
                "ENV_WEB_PASSWORD": "correct-horse",
                "MEDIAFLUX_INITIALIZED": "1",
            },
        )
        self.assertTrue(values["WEB_SECRET_KEY"])

    def test_initialize_admin_preserves_password_whitespace_in_user_env(self):
        password = "  correct-$horse\"  "
        self.first_run.initialize_admin("admin", password)

        self.assertEqual(self.config._read_env_file()["ENV_WEB_PASSWORD"], password)

    def test_initialize_admin_only_allows_one_concurrent_creator(self):
        barrier = threading.Barrier(3)
        errors: list[Exception] = []

        def initialize(username: str) -> None:
            barrier.wait()
            try:
                self.first_run.initialize_admin(username, "correct-horse")
            except Exception as exc:  # 仅允许一个调用者赢得独占创建。
                errors.append(exc)

        threads = [threading.Thread(target=initialize, args=(name,)) for name in ("one", "two")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(len(errors), 1)
        self.assertIn(self.config._read_env_file()["ENV_WEB_PASSPORT"], {"one", "two"})
        with self.assertRaises(RuntimeError):
            self.first_run.initialize_admin("three", "correct-horse")

    def test_successful_setup_creates_login_session_without_writing_network_binding(self):
        with self._new_client() as client:
            page = self._setup_page(client)
            self.assertNotIn('name="allow_lan"', page.text)
            response = client.post(
                "/setup",
                data={
                    "csrf_token": self._csrf_token(page),
                    "username": "admin",
                    "password": "correct-horse",
                    "password_confirm": "correct-horse",
                    "allow_lan": "1",
                },
                follow_redirects=False,
            )
            dashboard = client.get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn("WEB_HOST", self.config._read_env_file())

    def test_removed_network_applying_route_returns_not_found(self):
        with self._new_client() as client:
            response = client.get("/network-applying", follow_redirects=False)

        self.assertEqual(response.status_code, 404)

    def test_unicode_credentials_survive_setup_logout_and_login(self):
        with self._new_client() as client:
            page = self._setup_page(client)
            setup = client.post(
                "/setup",
                data={
                    "csrf_token": self._csrf_token(page),
                    "username": "管理员",
                    "password": "密码足够长度1234",
                    "password_confirm": "密码足够长度1234",
                },
                follow_redirects=False,
            )
            self.assertEqual(setup.status_code, 302)
            dashboard = client.get("/")
            logout = client.post(
                "/logout",
                data={"csrf_token": self._csrf_token(dashboard)},
                follow_redirects=False,
            )
            self.assertEqual(logout.status_code, 302)
            self.assertEqual(logout.headers["location"], "/login")
            page = client.get("/login")
            self.assertEqual(page.status_code, 200)
            login = client.post(
                "/login",
                data={
                    "csrf_token": self._csrf_token(page),
                    "username": "管理员",
                    "password": "密码足够长度1234",
                },
                follow_redirects=False,
            )

        self.assertEqual(login.status_code, 302)
        self.assertEqual(login.headers["location"], "/")

    def test_setup_rate_limit_is_independent_and_cleared_after_success(self):
        from app.security import (
            clear_setup_failures,
            login_rate_limited,
            record_setup_failure,
        )

        with self._new_client() as client:
            for _ in range(5):
                page = self._setup_page(client)
                response = client.post(
                    "/setup",
                    data={
                        "csrf_token": self._csrf_token(page),
                        "username": "admin",
                        "password": "short",
                        "password_confirm": "short",
                    },
                )
                self.assertEqual(response.status_code, 400)
            page = self._setup_page(client)
            limited = client.post(
                "/setup",
                data={
                    "csrf_token": self._csrf_token(page),
                    "username": "admin",
                    "password": "correct-horse",
                    "password_confirm": "correct-horse",
                },
            )
            self.assertEqual(limited.status_code, 429)
            self.assertFalse(login_rate_limited("testclient"))

            clear_setup_failures("testclient")
            for _ in range(4):
                record_setup_failure("testclient")
            page = self._setup_page(client)
            success = client.post(
                "/setup",
                data={
                    "csrf_token": self._csrf_token(page),
                    "username": "admin",
                    "password": "correct-horse",
                    "password_confirm": "correct-horse",
                },
                follow_redirects=False,
            )
            self.assertEqual(success.status_code, 302)

        from app.security import record_setup_failure, setup_rate_limited
        record_setup_failure("testclient")
        self.assertFalse(setup_rate_limited("testclient"))
        clear_setup_failures("testclient")


    def test_corrupt_non_utf8_config_shows_manual_recovery_and_post_returns_409(self):
        self.paths.config_dir.mkdir(parents=True)
        broken = b"\xff\xfeBROKEN"
        self.paths.env_file.write_bytes(broken)
        self._reload_runtime_modules()

        with self._new_client() as client:
            page = client.get("/setup")
            response = client.post(
                "/setup",
                data={
                    "csrf_token": self._csrf_token(page),
                    "username": "admin",
                    "password": "correct-horse",
                    "password_confirm": "correct-horse",
                },
                follow_redirects=False,
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn("user.env", page.text)
        self.assertIn("备份", page.text)
        self.assertEqual(response.status_code, 409)
        self.assertIn("移走", response.text)
        self.assertEqual(self.paths.env_file.read_bytes(), broken)

    @unittest.skipIf(os.name == "nt", "POSIX 链接安全 Web 回归")
    def test_symlink_config_setup_returns_manual_recovery_without_touching_target(self):
        self.paths.config_dir.mkdir(parents=True)
        external = Path(self.temp.name) / "outside.env"
        external.write_text("TMDB_API_KEY=outside\n", encoding="utf-8")
        self.paths.env_file.symlink_to(external)
        self._reload_runtime_modules()

        with self._new_client() as client:
            page = client.get("/setup")
            response = client.post(
                "/setup",
                data={
                    "csrf_token": self._csrf_token(page),
                    "username": "admin",
                    "password": "correct-horse",
                    "password_confirm": "correct-horse",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("user.env", response.text)
        self.assertEqual(external.read_text(encoding="utf-8"), "TMDB_API_KEY=outside\n")
        self.assertTrue(self.paths.env_file.is_symlink())

    def test_any_publish_error_refreshes_complete_competitor_state(self):
        self.paths.config_dir.mkdir(parents=True)
        self.paths.env_file.write_text(
            "TMDB_API_KEY=keep-me\nENV_WEB_PASSPORT=partial\n",
            encoding="utf-8",
        )
        self._reload_runtime_modules()
        competitor = (
            b"ENV_WEB_PASSPORT=other\n"
            b"ENV_WEB_PASSWORD=other-password\n"
            b"WEB_SECRET_KEY=competitor-secret\n"
            b"MEDIAFLUX_INITIALIZED=1\n"
        )

        real_permissions = self.config._apply_private_permissions
        calls = 0

        def fail_backup_acl(path: Path):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.paths.env_file.write_bytes(competitor)
                raise self.config.AtomicPublishError("backup acl failed")
            return real_permissions(path)

        with self._new_client() as client, patch(
            "app.config._apply_private_permissions", side_effect=fail_backup_acl
        ):
            page = self._setup_page(client)
            response = client.post(
                "/setup",
                data={
                    "csrf_token": self._csrf_token(page),
                    "username": "admin",
                    "password": "correct-horse",
                    "password_confirm": "correct-horse",
                },
                follow_redirects=False,
            )
            login = client.get("/login", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login")
        self.assertEqual(login.status_code, 200)
        self.assertFalse(self.first_run.needs_initialization())

    def test_successful_publish_does_not_read_final_file_again(self):
        with patch.object(Path, "read_bytes", side_effect=PermissionError("post-publish read blocked")):
            self.first_run.initialize_admin("admin", "correct-horse")

        self.assertFalse(self.first_run.needs_initialization())
        self.assertTrue(self.paths.env_file.exists())


if __name__ == "__main__":
    unittest.main()
