from __future__ import annotations

import errno
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import config


class ConfigAtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.env_file = Path(self.temp.name) / "config" / "user.env"
        self.environment = dict(os.environ)
        self.env_patch = patch.object(config, "ENV_FILE", self.env_file)
        self.cache_patch = patch.object(config, "_cache", None)
        self.env_patch.start()
        self.cache_patch.start()
        app_keys = {
            k for k in os.environ
            if k.startswith((
                "MEDIAFLUX_", "ENV_", "WEB_", "TMDB_", "JELLYFIN_",
                "OPENAI_", "EMBY_", "PLEX_", "SAFE_VALUE", "UNRELATED", "DISCOVERY_",
            ))
        }
        for k in app_keys:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.environment)
        self.cache_patch.stop()
        self.env_patch.stop()
        self.temp.cleanup()

    def test_web_port_defaults_to_project_default_when_unconfigured(self):
        self.assertEqual(config.flask_port(), 1258)

    def test_replace_publish_failure_keeps_final_absent_and_does_not_update_runtime_state(self):
        with patch(
            "app.config._publish_noreplace",
            side_effect=OSError("publish failed"),
        ):
            with self.assertRaises(OSError):
                config.set_and_save({"SAFE_VALUE": "new"})

        self.assertFalse(self.env_file.exists())
        self.assertNotIn("SAFE_VALUE", os.environ)
        self.assertIsNone(config._cache)
        self.assertEqual(list(self.env_file.parent.glob(".user.env.*.tmp")), [])

    def test_unrelated_save_does_not_replay_stale_file_value_over_startup_override(self):
        self.env_file.parent.mkdir(parents=True)
        config.write_env_file(
            self.env_file,
            {
                "TAVILY_API_KEY": "stale-file-secret",
                "WEB_PORT": "1258",
            },
            replace=False,
        )
        os.environ["TAVILY_API_KEY"] = "deployment-secret"

        with patch.object(
            config, "_STARTUP_ENV_OVERRIDES", frozenset({"TAVILY_API_KEY"})
        ):
            config.set_and_save({"WEB_PORT": "22366"})

        self.assertEqual(os.environ["TAVILY_API_KEY"], "deployment-secret")
        self.assertEqual(os.environ["WEB_PORT"], "22366")
        self.assertEqual(config.get("TAVILY_API_KEY"), "deployment-secret")
        self.assertEqual(
            config._read_env_file(self.env_file)["TAVILY_API_KEY"],
            "stale-file-secret",
        )

    def test_direct_save_rejects_startup_override_without_file_or_runtime_mutation(self):
        self.env_file.parent.mkdir(parents=True)
        config.write_env_file(
            self.env_file, {"WEB_PORT": "1258"}, replace=False
        )
        os.environ["WEB_PORT"] = "32366"

        with patch.object(
            config, "_STARTUP_ENV_OVERRIDES", frozenset({"WEB_PORT"})
        ):
            with self.assertRaises(config.ExternalConfigOverrideError):
                config.set_and_save({"WEB_PORT": "22366"})

        self.assertEqual(os.environ["WEB_PORT"], "32366")
        self.assertEqual(config._read_env_file(self.env_file)["WEB_PORT"], "1258")
        self.assertIsNone(config._cache)

    def test_set_and_save_rejects_competing_file_change(self):
        self.env_file.parent.mkdir(parents=True)
        config.write_env_file(
            self.env_file,
            {"WEB_PORT": "1258"},
            replace=False,
        )
        real_snapshot = config.read_env_snapshot

        def snapshot_then_compete(path, **kwargs):
            snapshot = real_snapshot(path, **kwargs)
            config.write_env_file(
                self.env_file,
                {"WEB_PORT": "32366"},
                replace=True,
            )
            return snapshot

        with patch.object(
            config,
            "read_env_snapshot",
            side_effect=snapshot_then_compete,
        ):
            with self.assertRaises(config.ConcurrentConfigUpdateError):
                config.set_and_save({"WEB_PORT": "22366"})

        self.assertEqual(config._read_env_file(self.env_file)["WEB_PORT"], "32366")
        self.assertNotIn("WEB_PORT", os.environ)
        self.assertIsNone(config._cache)

    def test_directory_fsync_unsupported_errno_is_ignored_after_publish(self):
        error = OSError(errno.EINVAL, "directory fsync unsupported")
        with patch("app.config.os.fsync", side_effect=(None, error)), patch(
            "app.config._logger.critical"
        ) as critical:
            config.write_env_file(self.env_file, {"SAFE_VALUE": "new"}, replace=False)

        critical.assert_not_called()
        self.assertEqual(config._read_env_file(self.env_file)["SAFE_VALUE"], "new")

    @unittest.skipIf(os.name == "nt", "POSIX 目录 fsync 合同")
    def test_directory_fsync_eio_logs_critical_but_keeps_published_final(self):
        error = OSError(errno.EIO, "directory fsync failed")
        with patch("app.config.os.fsync", side_effect=(None, error)), patch(
            "app.config._logger.critical"
        ) as critical:
            config.write_env_file(self.env_file, {"SAFE_VALUE": "new"}, replace=False)

        critical.assert_called_once()
        self.assertIn("fsync", critical.call_args.args[0])
        self.assertEqual(config._read_env_file(self.env_file)["SAFE_VALUE"], "new")

    def test_write_failure_cleans_only_its_temp_and_keeps_abandoned_temp_for_recovery_inspection(self):
        self.env_file.parent.mkdir(parents=True)
        abandoned = self.env_file.parent / ".user.env.previous-process.tmp"
        abandoned.write_bytes(b"partial")
        with patch("app.config.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                config.write_env_file(self.env_file, {"SAFE_VALUE": "new"}, replace=False)

        self.assertFalse(self.env_file.exists())
        self.assertEqual(abandoned.read_bytes(), b"partial")
        self.assertEqual(list(self.env_file.parent.glob(".user.env.*.tmp")), [abandoned])

    def test_create_only_publish_never_removes_competing_final_file(self):
        other_contents = b"ENV_WEB_PASSPORT=other\n"

        def competing_publish(temp: Path, final: Path) -> None:
            final.write_bytes(other_contents)
            raise FileExistsError(errno.EEXIST, "already exists", str(final))

        with patch("app.config._publish_noreplace", side_effect=competing_publish):
            with self.assertRaises(FileExistsError):
                config.write_env_file(
                    self.env_file,
                    {"ENV_WEB_PASSPORT": "admin"},
                    replace=False,
                )

        self.assertEqual(self.env_file.read_bytes(), other_contents)
        self.assertEqual(list(self.env_file.parent.glob(".user.env.*.tmp")), [])

    def test_create_only_publish_unsupported_fails_closed_without_empty_final(self):
        with patch("app.config._publish_noreplace", side_effect=config.AtomicPublishError("unsupported")):
            with self.assertRaises(config.AtomicPublishError):
                config.write_env_file(
                    self.env_file,
                    {"ENV_WEB_PASSPORT": "admin"},
                    replace=False,
                )

        self.assertFalse(self.env_file.exists())
        self.assertEqual(list(self.env_file.parent.glob(".user.env.*.tmp")), [])

    @unittest.skipIf(os.name == "nt", "Linux rename 合同")
    def test_no_replace_uses_linux_native_rename_and_never_hard_links(self):
        def publish(temp: Path, final: Path) -> None:
            os.rename(temp, final)

        with patch("app.config._is_windows", return_value=False), patch(
            "app.config._linux_rename_noreplace", side_effect=publish
        ) as native, patch("app.config.os.link") as hard_link:
            config.write_env_file(self.env_file, {"SAFE_VALUE": "value"}, replace=False)

        native.assert_called_once()
        hard_link.assert_not_called()
        self.assertEqual(config._read_env_file(self.env_file)["SAFE_VALUE"], "value")
        self.assertEqual(list(self.env_file.parent.glob(".user.env.*.tmp")), [])

    def test_successful_no_replace_does_not_depend_on_temp_unlink(self):
        with patch.object(Path, "unlink", side_effect=PermissionError("blocked")):
            config.write_env_file(self.env_file, {"SAFE_VALUE": "value"}, replace=False)

        self.assertEqual(config._read_env_file(self.env_file)["SAFE_VALUE"], "value")
        self.assertEqual(list(self.env_file.parent.glob(".user.env.*.tmp")), [])

    def test_no_replace_uses_windows_native_rename_contract(self):
        succeeded = type("Result", (), {"returncode": 0})()

        def publish(temp: Path, final: Path) -> None:
            os.rename(temp, final)

        with patch("app.config._is_windows", return_value=True), patch(
            "app.config._windows_current_user_sid", return_value="S-1-5-21-1000"
        ), patch("app.config.subprocess.run", return_value=succeeded), patch(
            "app.config._windows_rename_noreplace", side_effect=publish
        ) as native:
            config.write_env_file(self.env_file, {"SAFE_VALUE": "value"}, replace=False)

        native.assert_called_once()
        self.assertTrue(self.env_file.exists())

    def test_windows_movefileex_ctypes_contract_uses_zero_flags(self):
        move_file = Mock(return_value=1)
        kernel32 = Mock(MoveFileExW=move_file)

        with patch.object(config.os, "name", "nt"), patch(
            "app.config.ctypes.WinDLL", create=True, return_value=kernel32
        ) as win_dll:
            config._windows_rename_noreplace(Path("source.tmp"), Path("user.env"))

        win_dll.assert_called_once_with("kernel32", use_last_error=True)
        move_file.assert_called_once_with("source.tmp", "user.env", 0)

    def test_windows_movefileex_existing_target_maps_to_file_exists(self):
        move_file = Mock(return_value=0)
        kernel32 = Mock(MoveFileExW=move_file)

        with patch.object(config.os, "name", "nt"), patch(
            "app.config.ctypes.WinDLL", create=True, return_value=kernel32
        ), patch("app.config.ctypes.get_last_error", create=True, return_value=183):
            with self.assertRaises(FileExistsError):
                config._windows_rename_noreplace(Path("source.tmp"), Path("user.env"))

    def test_permission_failure_preserves_original_error_and_scrubs_temp_if_unlink_fails(self):
        original = config.AtomicPublishError("acl failed")
        real_unlink = Path.unlink

        def blocked_unlink(path: Path, *args, **kwargs):
            if path.name.startswith(".user.env."):
                raise PermissionError("blocked cleanup")
            return real_unlink(path, *args, **kwargs)

        with patch("app.config._apply_private_permissions", side_effect=[None, original]), patch.object(
            Path, "unlink", autospec=True, side_effect=blocked_unlink
        ):
            with self.assertRaisesRegex(config.AtomicPublishError, "acl failed"):
                config.write_env_file(self.env_file, {"SECRET": "top-secret"}, replace=False)

        self.assertFalse(self.env_file.exists())
        leaked = list(self.env_file.parent.glob(".user.env.*.tmp"))
        self.assertEqual(len(leaked), 1)
        self.assertEqual(leaked[0].read_bytes(), b"")
        real_unlink(leaked[0])
    def test_apply_runtime_values_serializes_with_initial_cache_load(self):
        started = threading.Event()
        release = threading.Event()

        def delayed_read(path):
            started.set()
            release.wait(timeout=2)
            return {"OLD": "stale"}

        config._cache = None
        with patch("app.config._read_env_file", side_effect=delayed_read):
            loader = threading.Thread(target=config._ensure_loaded)
            loader.start()
            self.assertTrue(started.wait(timeout=2))
            applied = threading.Thread(
                target=config._apply_runtime_values,
                args=({"NEW": "fresh"},),
                kwargs={"path": config.ENV_FILE},
            )
            applied.start()
            release.set()
            loader.join(timeout=2)
            applied.join(timeout=2)

        self.assertEqual(config.all_items(), {"NEW": "fresh"})

    def test_update_runtime_env_file_applies_only_changed_keys_after_cas_publish(self):
        config.write_env_file(
            self.env_file,
            {"DISCOVERY_ENABLED": "1", "UNRELATED": "kept"},
            replace=False,
        )
        expected = self.env_file.read_bytes()
        os.environ["UNRELATED"] = "runtime-value"
        with patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()):
            result = config.update_runtime_env_file(
                self.env_file,
                {"DISCOVERY_ENABLED": "0"},
                expected=expected,
            )

        self.assertEqual(result["DISCOVERY_ENABLED"], "0")
        self.assertEqual(config.all_items()["UNRELATED"], "kept")
        self.assertEqual(os.environ["DISCOVERY_ENABLED"], "0")
        self.assertEqual(os.environ["UNRELATED"], "runtime-value")

    def test_update_runtime_env_file_failure_keeps_runtime_state_unchanged(self):
        os.environ["DISCOVERY_ENABLED"] = "1"
        config._cache = {"DISCOVERY_ENABLED": "1", "UNRELATED": "kept"}
        original_cache = dict(config._cache)

        for error in (
            config.ConcurrentConfigUpdateError("conflict"),
            config.AtomicPublishError("publish failed"),
            OSError("io failed"),
            ValueError("invalid value"),
        ):
            with self.subTest(error=type(error).__name__), patch.object(
                config, "update_env_file", side_effect=error
            ), patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()):
                with self.assertRaises(type(error)):
                    config.update_runtime_env_file(
                        self.env_file,
                        {"DISCOVERY_ENABLED": "0"},
                        expected=b"snapshot",
                    )
                self.assertEqual(os.environ["DISCOVERY_ENABLED"], "1")
                self.assertEqual(config._cache, original_cache)

    def test_update_env_file_maps_create_only_competition_to_concurrent_update(self):
        competitor = b"DISCOVERY_ENABLED=1\n"

        def create_competitor(*args, **kwargs):
            self.env_file.parent.mkdir(parents=True, exist_ok=True)
            self.env_file.write_bytes(competitor)
            raise FileExistsError("competitor created user.env")

        with patch("app.config.write_env_file", side_effect=create_competitor):
            with self.assertRaises(config.ConcurrentConfigUpdateError):
                config.update_env_file(
                    self.env_file,
                    {"DISCOVERY_ENABLED": "0"},
                    expected=None,
                )

        self.assertEqual(self.env_file.read_bytes(), competitor)

    def test_update_runtime_env_file_rejects_startup_override_without_mutation(self):
        config.write_env_file(self.env_file, {"DISCOVERY_ENABLED": "1"}, replace=False)
        expected = self.env_file.read_bytes()
        os.environ["DISCOVERY_ENABLED"] = "1"
        with patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset({"DISCOVERY_ENABLED"})):
            with self.assertRaises(config.ExternalConfigOverrideError):
                config.update_runtime_env_file(
                    self.env_file,
                    {"DISCOVERY_ENABLED": "0"},
                    expected=expected,
                )

        self.assertEqual(self.env_file.read_bytes(), expected)
        self.assertEqual(os.environ["DISCOVERY_ENABLED"], "1")
        self.assertIsNone(config._cache)

    def test_recovery_capture_permission_failure_restores_original_without_secret_residue(self):
        self.env_file.parent.mkdir(parents=True)
        original = b"TMDB_API_KEY=original\nENV_WEB_PASSPORT=partial\n"
        self.env_file.write_bytes(original)
        failure = config.AtomicPublishError("backup acl failed")

        with patch(
            "app.config._apply_private_permissions",
            side_effect=[None, None, failure],
        ):
            with self.assertRaisesRegex(config.AtomicPublishError, "backup acl failed"):
                config.update_env_file(
                    self.env_file,
                    {"ENV_WEB_PASSWORD": "new-password"},
                    expected=original,
                )

        self.assertEqual(self.env_file.read_bytes(), original)
        self.assertEqual(list(self.env_file.parent.glob(".user.env.recovery.*.bak")), [])
        self.assertEqual(list(self.env_file.parent.glob(".user.env.*.tmp")), [])

    def test_recovery_transaction_never_overwrites_competitor_created_before_publish(self):
        self.env_file.parent.mkdir(parents=True)
        original = b"TMDB_API_KEY=original\nENV_WEB_PASSPORT=partial\n"
        competitor = b"TMDB_API_KEY=competitor\nENV_WEB_PASSPORT=other\n"
        self.env_file.write_bytes(original)
        real_publish = config._publish_noreplace
        calls = 0

        def racing_publish(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write_bytes(competitor)
            real_publish(source, target)

        with patch("app.config._publish_noreplace", side_effect=racing_publish):
            with self.assertRaises(config.ConcurrentConfigUpdateError):
                config.update_env_file(
                    self.env_file,
                    {"ENV_WEB_PASSWORD": "new-password"},
                    expected=original,
                )

        self.assertEqual(self.env_file.read_bytes(), competitor)
        backups = list(self.env_file.parent.glob(".user.env.recovery.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        backups[0].unlink()

    def test_recovery_transaction_restores_changed_snapshot_without_backup_residue(self):
        self.env_file.parent.mkdir(parents=True)
        expected = b"TMDB_API_KEY=original\n"
        competitor = b"TMDB_API_KEY=competitor\n"
        self.env_file.write_bytes(competitor)

        with self.assertRaises(config.ConcurrentConfigUpdateError):
            config.update_env_file(
                self.env_file,
                {"ENV_WEB_PASSPORT": "admin"},
                expected=expected,
            )

        self.assertEqual(self.env_file.read_bytes(), competitor)
        self.assertEqual(list(self.env_file.parent.glob(".user.env.recovery.*.bak")), [])

    def test_windows_recovery_contract_uses_native_no_replace_for_capture_and_publish(self):
        self.env_file.parent.mkdir(parents=True)
        original = b"TMDB_API_KEY=original\n"
        self.env_file.write_bytes(original)

        def rename(source: Path, target: Path) -> None:
            os.rename(source, target)

        succeeded = type("Result", (), {"returncode": 0})()
        with patch("app.config._is_windows", return_value=True), patch(
            "app.config._windows_current_user_sid", return_value="S-1-5-21-1000"
        ), patch("app.config.subprocess.run", return_value=succeeded), patch(
            "app.config._windows_rename_noreplace", side_effect=rename
        ) as native:
            values = config.update_env_file(
                self.env_file,
                {"ENV_WEB_PASSPORT": "admin"},
                expected=original,
            )

        self.assertEqual(values["ENV_WEB_PASSPORT"], "admin")
        self.assertGreaterEqual(native.call_count, 2)
        self.assertEqual(list(self.env_file.parent.glob(".user.env.recovery.*.bak")), [])

    def test_plain_values_are_literal_and_save_normalizes_file(self):
        os.environ["EXPAND_ME"] = "wrong-value"
        self.env_file.parent.mkdir(parents=True)
        self.env_file.write_text("VALUE=${EXPAND_ME}/value\n", encoding="utf-8")

        self.assertEqual(config._read_env_file(self.env_file)["VALUE"], "${EXPAND_ME}/value")
        config.set_and_save({"UNRELATED_SETTING": "ok"})

        values = config._read_env_file(self.env_file)
        self.assertEqual(values["VALUE"], "${EXPAND_ME}/value")
        self.assertEqual(values["UNRELATED_SETTING"], "ok")
        self.assertIn("# mediaflux-literal", self.env_file.read_text(encoding="utf-8"))

    def test_all_values_round_trip_without_dotenv_injection_or_special_password_corruption(self):
        os.environ["EXPAND_ME"] = "wrong-value"
        password = "  tab\t$literal${EXPAND_ME} ' \\\\ \" =  "

        config.write_env_file(
            self.env_file,
            {
                "ENV_WEB_PASSWORD": password,
                "OTHER_VALUE": "quote ' and $same",
            },
            replace=True,
        )

        self.assertEqual(config._read_env_file(self.env_file)["ENV_WEB_PASSWORD"], password)
        self.assertEqual(config._read_env_file(self.env_file)["OTHER_VALUE"], "quote ' and $same")
        self.assertNotIn("ENV_WEB_PASSWORD", os.environ)
    def test_invalid_key_or_line_separator_value_is_rejected_before_creating_final_file(self):
        for values in (
            {"INVALID-KEY": "value"},
            {"SAFE_VALUE": "before\u2028after"},
            {"SAFE_VALUE": "before\x00after"},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    config.write_env_file(self.env_file, values, replace=True)
                self.assertFalse(self.env_file.exists())

    def test_windows_acl_failure_prevents_final_publish(self):
        failed = type("Result", (), {"returncode": 1})()
        with patch("app.config._is_windows", return_value=True), patch(
            "app.config._windows_current_user_sid", return_value="S-1-5-21-1000"
        ), patch("app.config.subprocess.run", return_value=failed) as run:
            with self.assertRaises(config.AtomicPublishError):
                config.write_env_file(self.env_file, {"SAFE_VALUE": "value"}, replace=False)

        self.assertFalse(self.env_file.exists())
        self.assertEqual(list(self.env_file.parent.glob(".user.env.*.tmp")), [])
        self.assertIn("icacls", run.call_args.args[0][0])

    def test_windows_acl_grants_system_admin_and_current_service_account(self):
        succeeded = type("Result", (), {"returncode": 0})()
        with patch("app.config._is_windows", return_value=True), patch(
            "app.config._windows_current_user_sid", return_value="S-1-5-21-1000"
        ), patch("app.config.subprocess.run", return_value=succeeded) as run:
            config.write_env_file(self.env_file, {"SAFE_VALUE": "value"}, replace=False)

        command = run.call_args.args[0]
        self.assertIn("/inheritance:r", command)
        self.assertIn("*S-1-5-18:(F)", command)
        self.assertIn("*S-1-5-32-544:(F)", command)
        self.assertIn("*S-1-5-21-1000:(F)", command)
        self.assertTrue(self.env_file.exists())


    @unittest.skipIf(os.name == "nt", "POSIX 文件类型合同")
    def test_recovery_refuses_symlink_without_touching_link_target(self):
        self.env_file.parent.mkdir(parents=True)
        external = Path(self.temp.name) / "external.env"
        original = b"TMDB_API_KEY=outside\n"
        external.write_bytes(original)
        original_mode = external.stat().st_mode
        self.env_file.symlink_to(external)

        with self.assertRaisesRegex(config.UnsafeConfigFileError, "符号链接|安全"):
            config.update_env_file(
                self.env_file,
                {"ENV_WEB_PASSPORT": "admin"},
                expected=original,
            )

        self.assertTrue(self.env_file.is_symlink())
        self.assertEqual(external.read_bytes(), original)
        self.assertEqual(external.stat().st_mode, original_mode)
        self.assertEqual(list(self.env_file.parent.glob(".user.env.recovery.*.bak")), [])

    @unittest.skipIf(os.name == "nt", "POSIX 硬链接合同")
    def test_recovery_refuses_multiple_hardlinks_without_scrubbing_peer(self):
        self.env_file.parent.mkdir(parents=True)
        peer = Path(self.temp.name) / "peer.env"
        original = b"TMDB_API_KEY=outside\n"
        peer.write_bytes(original)
        os.link(peer, self.env_file)

        with self.assertRaisesRegex(config.UnsafeConfigFileError, "硬链接|安全"):
            config.update_env_file(
                self.env_file,
                {"ENV_WEB_PASSPORT": "admin"},
                expected=original,
            )

        self.assertEqual(peer.read_bytes(), original)
        self.assertEqual(self.env_file.read_bytes(), original)
        self.assertEqual(self.env_file.stat().st_nlink, 2)

    @unittest.skipIf(os.name == "nt", "POSIX FIFO 合同")
    def test_recovery_refuses_fifo_before_opening_or_mutating_it(self):
        self.env_file.parent.mkdir(parents=True)
        os.mkfifo(self.env_file, 0o600)

        with self.assertRaisesRegex(config.UnsafeConfigFileError, "普通文件|安全"):
            config.update_env_file(
                self.env_file,
                {"ENV_WEB_PASSPORT": "admin"},
                expected=b"",
            )

        self.assertTrue(stat.S_ISFIFO(os.lstat(self.env_file).st_mode))

    def test_recovery_returns_the_exact_published_snapshot(self):
        self.env_file.parent.mkdir(parents=True)
        original = b"TMDB_API_KEY=original\n"
        self.env_file.write_bytes(original)

        result = config.update_env_file(
            self.env_file,
            {"ENV_WEB_PASSPORT": "admin"},
            expected=original,
        )

        self.assertEqual(result.payload, self.env_file.read_bytes())
        self.assertEqual(result["ENV_WEB_PASSPORT"], "admin")

    def test_recovery_keeps_private_backup_when_directory_fsync_has_hard_error(self):
        self.env_file.parent.mkdir(parents=True)
        original = b"TMDB_API_KEY=original\n"
        self.env_file.write_bytes(original)

        with patch("app.config._fsync_directory", return_value="error"):
            result = config.update_env_file(
                self.env_file,
                {"ENV_WEB_PASSPORT": "admin"},
                expected=original,
            )

        self.assertEqual(result.payload, self.env_file.read_bytes())
        backups = list(self.env_file.parent.glob(".user.env.recovery.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    @unittest.skipIf(os.name == "nt", "POSIX 双重失败日志合同")
    def test_recovery_cleanup_double_failure_logs_critical_with_backup_path(self):
        self.env_file.parent.mkdir(parents=True)
        original = b"TMDB_API_KEY=original\n"
        self.env_file.write_bytes(original)

        with patch("app.config._scrub_verified_file", return_value=False), patch.object(
            Path, "unlink", side_effect=PermissionError("blocked")
        ), patch("app.config._logger.critical") as critical:
            config.update_env_file(
                self.env_file,
                {"ENV_WEB_PASSPORT": "admin"},
                expected=original,
            )

        backups = list(self.env_file.parent.glob(".user.env.recovery.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(any(str(backups[0]) in str(call) for call in critical.call_args_list))

    def test_windows_safety_contract_rejects_reparse_points_and_multiple_links(self):
        base = {
            "st_mode": stat.S_IFREG | 0o600,
            "st_dev": 1,
            "st_ino": 2,
        }
        with patch("app.config.os.name", "nt"):
            with self.assertRaisesRegex(config.UnsafeConfigFileError, "reparse"):
                config._validate_config_stat(
                    self.env_file,
                    SimpleNamespace(**base, st_nlink=1, st_file_attributes=0x400),
                )
            with self.assertRaisesRegex(config.UnsafeConfigFileError, "硬链接"):
                config._validate_config_stat(
                    self.env_file,
                    SimpleNamespace(**base, st_nlink=2, st_file_attributes=0),
                )
            with self.assertRaisesRegex(config.UnsafeConfigFileError, "可靠验证"):
                config._validate_config_stat(
                    self.env_file,
                    SimpleNamespace(**base, st_nlink=1),
                )

    def test_keyboard_interrupt_after_capture_restores_original_configuration(self):
        self.env_file.parent.mkdir(parents=True)
        original = b"TMDB_API_KEY=original\n"
        self.env_file.write_bytes(original)
        real_publish = config._publish_noreplace
        calls = 0

        def interrupt_second_publish(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("simulated process interruption")
            real_publish(source, target)

        with patch(
            "app.config._publish_noreplace",
            side_effect=interrupt_second_publish,
        ):
            with self.assertRaises(KeyboardInterrupt):
                config.update_env_file(
                    self.env_file,
                    {"ENV_WEB_PASSWORD": "new-password"},
                    expected=original,
                )

        self.assertEqual(self.env_file.read_bytes(), original)
        self.assertEqual(
            list(self.env_file.parent.glob(".user.env.recovery.*.bak")), []
        )
        self.assertEqual(list(self.env_file.parent.glob(".user.env.*.tmp")), [])

    def test_snapshot_recovers_single_backup_when_canonical_file_is_missing(self):
        self.env_file.parent.mkdir(parents=True)
        original = b"WEB_PORT=12370\n"
        backup = self.env_file.parent / ".user.env.recovery.deadbeef.bak"
        backup.write_bytes(original)

        payload, values = config.read_env_snapshot(self.env_file)

        self.assertEqual(payload, original)
        self.assertEqual(values["WEB_PORT"], "12370")
        self.assertEqual(self.env_file.read_bytes(), original)
        self.assertFalse(backup.exists())

    def test_snapshot_refuses_ambiguous_recovery_backups(self):
        self.env_file.parent.mkdir(parents=True)
        for suffix in ("one", "two"):
            (self.env_file.parent / f".user.env.recovery.{suffix}.bak").write_text(
                f"VALUE={suffix}\n", encoding="utf-8"
            )

        with self.assertRaisesRegex(config.CorruptConfigFileError, "多个"):
            config.read_env_snapshot(self.env_file)

        self.assertFalse(self.env_file.exists())



if __name__ == "__main__":
    unittest.main()
