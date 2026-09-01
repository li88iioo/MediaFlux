"""生产 CLI 的运行时行为。"""
from __future__ import annotations

import io
import json
import os
import platform
import runpy
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.cli import main
from app.runtime_paths import RuntimePaths
from app.version import BuildInfo


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class CliRuntimeTests(unittest.TestCase):
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    RESULT_PREFIX = "TASK3_RESULT="

    def _run_probe(
        self,
        script: str,
        *,
        env: dict[str, str],
        runtime_root: Path,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_root = Path(temporary_directory) / "source-copy"
            shutil.copytree(self.PROJECT_ROOT / "app", source_root / "app")
            shutil.copy2(self.PROJECT_ROOT / "mediaflux.py", source_root / "mediaflux.py")

            environment = os.environ.copy()
            for name in (
                "MEDIAFLUX_DATA_DIR",
                "MEDIAFLUX_CONFIG_DIR",
                "MEDIAFLUX_CACHE_DIR",
                "MEDIAFLUX_LOG_DIR",
                "MEDIAFLUX_STRM_DIR",
                "MEDIAFLUX_TEST_MODE",
                "MEDIAFLUX_TEST_DB_PATH",
                "MEDIAFLUX_INITIALIZED",
                "ENV_WEB_PASSPORT",
                "ENV_WEB_PASSWORD",
                "WEB_HOST",
            ):
                environment.pop(name, None)
            self.assertTrue(runtime_root.is_absolute())
            environment.update(env)
            environment.update(
                {
                    "MEDIAFLUX_DATA_DIR": str(runtime_root),
                    "MEDIAFLUX_CONFIG_DIR": str(runtime_root / "config"),
                    "MEDIAFLUX_CACHE_DIR": str(runtime_root / "cache"),
                    "MEDIAFLUX_LOG_DIR": str(runtime_root / "logs"),
                    "MEDIAFLUX_STRM_DIR": str(runtime_root / "strm-data"),
                    "PYTHONPATH": os.pathsep.join(
                        (str(source_root), environment.get("PYTHONPATH", ""))
                    ),
                }
            )
            result = subprocess.run(
                [sys.executable, "-c", textwrap.dedent(script)],
                cwd=source_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertFalse(
                (source_root / "db").exists(),
                "CLI subprocess created a default source-checkout db directory",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            line = next(
                (line for line in result.stdout.splitlines() if line.startswith(self.RESULT_PREFIX)),
                None,
            )
            self.assertIsNotNone(line, result.stdout + result.stderr)
            return json.loads(line.removeprefix(self.RESULT_PREFIX))


    def _probe_start(
        self,
        args: list[str],
        *,
        runtime_root: Path,
        web_host: str | None,
        web_port: int,
        user_env: str | None = None,
    ) -> dict[str, object]:
        return self._run_probe(
            """
            import json
            import os
            import sys
            from unittest.mock import patch

            import app.cli as cli

            user_env = os.environ.get("TASK3_USER_ENV", "")
            if user_env:
                from pathlib import Path
                config_dir = Path(os.environ["MEDIAFLUX_CONFIG_DIR"])
                config_dir.mkdir(parents=True, exist_ok=True)
                (config_dir / "user.env").write_text(user_env, encoding="utf-8")

            app_main_before_start = "app.main" in sys.modules
            with patch("app.cli.uvicorn.run") as uvicorn_run:
                exit_code = cli.main(json.loads(os.environ["TASK3_ARGS"]))

            from app import config, database, logger

            call = uvicorn_run.call_args.kwargs
            result = {
                "exit_code": exit_code,
                "app_main_before_start": app_main_before_start,
                "app_main_after_start": "app.main" in sys.modules,
                "host": call["host"],
                "port": call["port"],
                "workers": call["workers"],
                "log_config_is_none": call["log_config"] is None,
                "access_log": call["access_log"],
                "data_dir": str(config.PATHS.data_dir),
                "config_dir": str(config.PATHS.config_dir),
                "cache_dir": str(config.PATHS.cache_dir),
                "database_path": str(database.resolve_db_path()),
                "log_dir": str(logger.LOG_DIR),
                "strm_dir": str(config.PATHS.strm_dir),
            }
            print("TASK3_RESULT=" + json.dumps(result))
            """,
            env={
                **({"WEB_HOST": web_host} if web_host is not None else {}),
                "TASK3_ARGS": json.dumps(args),
                **({"TASK3_USER_ENV": user_env} if user_env is not None else {}),
                "WEB_PORT": str(web_port),
                "WEB_SECRET_KEY": "task3-cli-runtime-secret",
            },
            runtime_root=runtime_root,
        )

    def test_version_command_is_machine_readable(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["version", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            set(payload),
            {"name", "version", "commit", "build_time", "python", "platform", "package", "arch"},
        )

    def test_start_uses_delayed_config_and_binds_real_path_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root) / "runtime-data"
            result = self._probe_start(
                ["start", "--data-dir", str(data_dir)],
                runtime_root=data_dir,
                web_host="127.0.0.9",
                web_port=23456,
            )

            self.assertFalse(result["app_main_before_start"])
            self.assertTrue(result["app_main_after_start"])
            self.assertEqual(result["host"], "127.0.0.9")
            self.assertEqual(result["port"], 23456)
            self.assertEqual(result["workers"], 1)
            self.assertEqual(result["data_dir"], str(data_dir))
            self.assertEqual(result["config_dir"], str(data_dir / "config"))
            self.assertEqual(result["cache_dir"], str(data_dir / "cache"))
            self.assertEqual(result["database_path"], str(data_dir / "db" / "mediaflux.db"))
            self.assertEqual(result["log_dir"], str(data_dir / "logs"))
            self.assertEqual(result["strm_dir"], str(data_dir / "strm-data"))

    def test_fresh_start_defaults_to_loopback_without_external_host(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root) / "runtime-data"
            result = self._probe_start(
                ["start", "--data-dir", str(data_dir)],
                runtime_root=data_dir,
                web_host=None,
                web_port=23456,
            )

        self.assertEqual(result["host"], "127.0.0.1")

    def test_fresh_start_rejects_non_loopback_environment_host(self) -> None:
        from app import cli

        with patch.dict(os.environ, {"WEB_HOST": "0.0.0.0"}, clear=False), patch(
            "app.modules.first_run.needs_initialization", return_value=True
        ), patch("app.cli.uvicorn.run") as run:
            exit_code = cli._start(None, 1258, None)

        self.assertEqual(exit_code, 2)
        run.assert_not_called()

    def test_start_recovers_before_config_reads_and_holds_lifecycle_through_uvicorn(self) -> None:
        import app.main
        from app import cli, config
        from app.modules import first_run

        events: list[str] = []

        @contextmanager
        def lifecycle_guard(_paths):
            events.append("lock-enter")
            try:
                yield
            finally:
                events.append("lock-exit")

        web_app = SimpleNamespace(
            state=SimpleNamespace(
                release_startup_lifecycle_guard=lambda: events.append("app-release")
            )
        )
        with patch(
            "app.modules.backup.runtime_lifecycle_guard",
            side_effect=lifecycle_guard,
        ), patch(
            "app.modules.backup.recover_pending_restore",
            side_effect=lambda *_args, **_kwargs: events.append("recover") or True,
        ), patch.object(
            config,
            "reload_after_restore",
            side_effect=lambda: events.append("config-reload"),
        ), patch.object(
            first_run,
            "refresh_startup_state_after_restore",
            side_effect=lambda: events.append("first-run-refresh"),
        ), patch.object(
            first_run,
            "resolve_bind_host",
            side_effect=lambda _host: events.append("host") or "127.0.0.1",
        ), patch.object(
            config,
            "flask_port",
            side_effect=lambda: events.append("port") or 1258,
        ), patch.object(
            app.main, "app", web_app
        ), patch.object(
            cli.uvicorn,
            "run",
            side_effect=lambda *_args, **_kwargs: events.append("uvicorn"),
        ):
            self.assertEqual(cli._start(None, None, None), 0)

        self.assertEqual(
            events,
            [
                "lock-enter",
                "recover",
                "config-reload",
                "first-run-refresh",
                "host",
                "port",
                "uvicorn",
                "app-release",
                "lock-exit",
            ],
        )

    def test_initialized_start_uses_persisted_host_after_delayed_import(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root) / "runtime-data"
            result = self._probe_start(
                ["start", "--data-dir", str(data_dir)],
                runtime_root=data_dir,
                web_host=None,
                web_port=23456,
                user_env=(
                    "WEB_HOST=0.0.0.0\nMEDIAFLUX_INITIALIZED=1\n"
                    "ENV_WEB_PASSPORT=admin\nENV_WEB_PASSWORD=correct-horse\n"
                ),
            )

        self.assertEqual(result["host"], "0.0.0.0")

    def test_explicit_start_host_and_port_override_config(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root) / "runtime-data"
            result = self._probe_start(
                [
                    "start",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--data-dir",
                    str(data_dir),
                ],
                runtime_root=data_dir,
                web_host="127.0.0.9",
                web_port=23456,
            )

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["host"], "127.0.0.1")
        self.assertEqual(result["port"], 0)
        self.assertEqual(result["workers"], 1)
        self.assertTrue(result["log_config_is_none"])
        self.assertFalse(result["access_log"])
    def test_status_uses_project_default_port_when_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            output = io.StringIO()
            with (
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.cli.urlopen", return_value=_Response(b'{"status":"ok"}')) as request,
                redirect_stdout(output),
            ):
                exit_code = main(["status"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(request.call_args.args[0], "http://127.0.0.1:1258/healthz")
        self.assertEqual(output.getvalue().strip(), "MediaFlux is healthy")

    def test_open_uses_project_default_port_when_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            output = io.StringIO()
            with (
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.cli.webbrowser.open", return_value=True) as open_browser,
                redirect_stdout(output),
            ):
                exit_code = main(["open"])

        self.assertEqual(exit_code, 0)
        open_browser.assert_called_once_with("http://127.0.0.1:1258/", new=2)
        self.assertEqual(output.getvalue().strip(), "http://127.0.0.1:1258/")

    def test_status_uses_configured_runtime_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            paths.config_dir.mkdir(parents=True)
            paths.env_file.write_text("WEB_PORT=24567\n", encoding="utf-8")
            output = io.StringIO()
            with (
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.cli.urlopen", return_value=_Response(b'{"status":"ok"}')) as request,
                redirect_stdout(output),
            ):
                exit_code = main(["status"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(request.call_args.args[0], "http://127.0.0.1:24567/healthz")
        self.assertEqual(output.getvalue().strip(), "MediaFlux is healthy")

    def test_status_rejects_non_object_and_malformed_responses_uniformly(self) -> None:
        for body in (b"[]", b"not-json"):
            with self.subTest(body=body):
                output = io.StringIO()
                with patch("app.cli.urlopen", return_value=_Response(body)), redirect_stdout(output):
                    exit_code = main(["status"])

                self.assertEqual(exit_code, 1)
                self.assertEqual(output.getvalue().strip(), "MediaFlux health check failed")

    def test_doctor_warning_only_result_uses_temporary_runtime_paths(self) -> None:
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            report = DiagnosticReport((DiagnosticCheck("ffprobe", "warning", "未找到 ffprobe"),))
            output = io.StringIO()
            with (
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.modules.runtime_diagnostics.run_diagnostics", return_value=report) as diagnostics,
                redirect_stdout(output),
            ):
                exit_code = main(["doctor", "--host", "127.0.0.8", "--port", "24567"])

        self.assertEqual(exit_code, 0)
        diagnostics.assert_called_once_with(
            paths, source_paths=(), target_paths=(), host="127.0.0.8", port=24567
        )
        self.assertIn("doctor", output.getvalue().lower())

    def test_doctor_uses_configured_host_and_port_after_runtime_paths(self) -> None:
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            report = DiagnosticReport((DiagnosticCheck("ffprobe", "warning", "未找到 ffprobe"),))
            paths.config_dir.mkdir()
            paths.env_file.write_text("WEB_HOST=127.0.0.9\nWEB_PORT=24678\n")
            output = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.modules.runtime_diagnostics.run_diagnostics", return_value=report) as diagnostics,
                redirect_stdout(output),
            ):
                exit_code = main(["doctor"])

        self.assertEqual(exit_code, 0)
        diagnostics.assert_called_once_with(
            paths, source_paths=(), target_paths=(), host="127.0.0.9", port=24678
        )

    def test_doctor_default_endpoint_reads_existing_env_file_without_importing_app_config(self) -> None:
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            paths.config_dir.mkdir()
            paths.env_file.write_text("WEB_HOST=127.0.0.9\nWEB_PORT=24678\n")
            report = DiagnosticReport((DiagnosticCheck("ffprobe", "warning", "未找到 ffprobe"),))
            output = io.StringIO()
            with (
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.modules.runtime_diagnostics.run_diagnostics", return_value=report) as diagnostics,
                redirect_stdout(output),
            ):
                exit_code = main(["doctor", "--json"])

        self.assertEqual(exit_code, 0)
        diagnostics.assert_called_once_with(
            paths, source_paths=(), target_paths=(), host="127.0.0.9", port=24678
        )
        self.assertNotIn("runtime_config", {check["key"] for check in json.loads(output.getvalue())})

    def test_doctor_environment_endpoint_overrides_existing_env_file(self) -> None:
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            paths.config_dir.mkdir()
            paths.env_file.write_text("WEB_HOST=127.0.0.9\nWEB_PORT=24678\n")
            report = DiagnosticReport((DiagnosticCheck("ffprobe", "warning", "未找到 ffprobe"),))
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"WEB_HOST": "127.0.0.7", "WEB_PORT": "24567"}, clear=True),
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.modules.runtime_diagnostics.run_diagnostics", return_value=report) as diagnostics,
                redirect_stdout(output),
            ):
                exit_code = main(["doctor"])

        self.assertEqual(exit_code, 0)
        diagnostics.assert_called_once_with(
            paths, source_paths=(), target_paths=(), host="127.0.0.7", port=24567
        )

    def test_doctor_invalid_env_file_returns_runtime_config_error_as_json(self) -> None:
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            paths.config_dir.mkdir()
            paths.env_file.write_text("WEB_PORT=not-a-port\n")
            report = DiagnosticReport((DiagnosticCheck("ffprobe", "warning", "未找到 ffprobe"),))
            output = io.StringIO()
            with (
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.modules.runtime_diagnostics.run_diagnostics", return_value=report),
                redirect_stdout(output),
            ):
                exit_code = main(["doctor", "--json"])

        checks = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(next(check for check in checks if check["key"] == "runtime_config")["status"], "error")

    def test_doctor_unreadable_config_path_returns_runtime_config_error_as_json(self) -> None:
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            paths.config_dir.mkdir()
            paths.env_file.mkdir()
            report = DiagnosticReport((DiagnosticCheck("ffprobe", "warning", "未找到 ffprobe"),))
            output = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.modules.runtime_diagnostics.run_diagnostics", return_value=report),
                redirect_stdout(output),
            ):
                exit_code = main(["doctor", "--json"])

        checks = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(next(check for check in checks if check["key"] == "runtime_config")["status"], "error")

    def test_doctor_absent_config_dir_uses_defaults_without_creating_it(self) -> None:
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "missing-config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            report = DiagnosticReport((DiagnosticCheck("ffprobe", "warning", "未找到 ffprobe"),))
            output = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.modules.runtime_diagnostics.run_diagnostics", return_value=report) as diagnostics,
                redirect_stdout(output),
            ):
                exit_code = main(["doctor"])

        self.assertEqual(exit_code, 0)
        self.assertFalse(paths.config_dir.exists())
        diagnostics.assert_called_once_with(
            paths, source_paths=(), target_paths=(), host="0.0.0.0", port=1258
        )

    def test_doctor_explicit_empty_host_overrides_environment_host(self) -> None:
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            report = DiagnosticReport((DiagnosticCheck("ffprobe", "warning", "未找到 ffprobe"),))
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"WEB_HOST": "127.0.0.7"}, clear=True),
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.modules.runtime_diagnostics.run_diagnostics", return_value=report) as diagnostics,
                redirect_stdout(output),
            ):
                exit_code = main(["doctor", "--host", "", "--port", "24567"])

        self.assertEqual(exit_code, 0)
        diagnostics.assert_called_once_with(
            paths, source_paths=(), target_paths=(), host="", port=24567
        )

    def test_doctor_shared_parser_honors_literal_and_last_assignment(self) -> None:
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            paths.config_dir.mkdir()
            paths.env_file.write_text(
                "WEB_HOST='127.0.0.8' # mediaflux-literal\n"
                "WEB_HOST=127.0.0.9\n"
                "WEB_PORT='24678' # mediaflux-literal\n",
                encoding="utf-8",
            )
            report = DiagnosticReport((DiagnosticCheck("ffprobe", "warning", "未找到 ffprobe"),))
            output = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.modules.runtime_diagnostics.run_diagnostics", return_value=report) as diagnostics,
                redirect_stdout(output),
            ):
                exit_code = main(["doctor"])

        self.assertEqual(exit_code, 0)
        diagnostics.assert_called_once_with(
            paths, source_paths=(), target_paths=(), host="127.0.0.9", port=24678
        )

    def test_doctor_strict_startup_promotes_only_default_port_warning(self) -> None:
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            report = DiagnosticReport((
                DiagnosticCheck("default_service_port", "warning", "端口已被占用", "停止占用进程"),
                DiagnosticCheck("ffprobe", "warning", "未找到 ffprobe"),
            ))
            output = io.StringIO()
            with (
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.modules.runtime_diagnostics.run_diagnostics", return_value=report),
                redirect_stdout(output),
            ):
                exit_code = main([
                    "doctor", "--json", "--strict-startup",
                    "--host", "0.0.0.0", "--port", "1258",
                ])

        checks = {item["key"]: item for item in json.loads(output.getvalue())}
        self.assertEqual(exit_code, 1)
        self.assertEqual(checks["default_service_port"]["status"], "error")
        self.assertEqual(
            checks["default_service_port"]["message"],
            "启动前检查失败：端口已被占用",
        )
        self.assertEqual(checks["ffprobe"]["status"], "warning")

    def test_doctor_runtime_paths_value_error_is_json_without_traceback(self) -> None:
        output = io.StringIO()
        with (
            patch("app.runtime_paths.get_runtime_paths", side_effect=ValueError("relative config path")),
            redirect_stdout(output),
        ):
            exit_code = main(["doctor", "--json"])

        checks = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(checks, [
            {
                "key": "runtime_config",
                "status": "error",
                "message": "当前执行身份无法解析运行路径：relative config path。",
                "suggestion": "检查运行目录相关环境变量是否为有效绝对路径。",
            }
        ])

    def test_doctor_error_result_returns_nonzero_without_host_dependencies(self) -> None:
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            report = DiagnosticReport((DiagnosticCheck("database_dir", "error", "数据库目录不可写"),))
            output = io.StringIO()
            with (
                patch("app.runtime_paths.get_runtime_paths", return_value=paths),
                patch("app.modules.runtime_diagnostics.run_diagnostics", return_value=report),
                redirect_stdout(output),
            ):
                exit_code = main(["doctor", "--json", "--host", "127.0.0.8", "--port", "24567"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())[0]["status"], "error")

    def test_build_info_uses_embedded_package_type_not_environment_spoofing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MEDIAFLUX_BUILD_COMMIT": "abc123",
                "MEDIAFLUX_BUILD_TIME": "2026-07-28T00:00:00Z",
                "MEDIAFLUX_PACKAGE": "appimage",
            },
            clear=False,
        ), patch("app.version.EMBEDDED_PACKAGE_TYPE", "system"):
            info = BuildInfo.current()
        self.assertEqual(info.commit, "abc123")
        self.assertEqual(info.build_time, "2026-07-28T00:00:00Z")
        self.assertEqual(info.package, "system")
        self.assertEqual(info.arch, platform.machine())

    def test_entrypoints_propagate_cli_exit_codes(self) -> None:
        cases = ((self.PROJECT_ROOT / "mediaflux.py", None),)
        for path, expected_args in cases:
            with self.subTest(path=path.name), patch("app.cli.main", return_value=17) as cli_main:
                with self.assertRaises(SystemExit) as exited:
                    runpy.run_path(str(path), run_name="__main__")

                self.assertEqual(exited.exception.code, 17)
                if expected_args is None:
                    cli_main.assert_called_once_with()
                else:
                    cli_main.assert_called_once_with(expected_args)


    def test_container_fresh_start_opens_web_setup_without_credentials(self) -> None:
        import app.main
        from app import cli

        web_app = SimpleNamespace(state=SimpleNamespace())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            with patch.dict(
                os.environ,
                {"MEDIAFLUX_CONTAINER": "1"},
                clear=True,
            ), patch("app.cli.get_runtime_paths", return_value=paths), patch(
                "app.modules.first_run.needs_initialization", return_value=True
            ), patch("app.main.app", web_app), patch("app.cli.uvicorn.run") as run:
                exit_code = cli._start(None, None, None)

        self.assertEqual(exit_code, 0)
        self.assertIs(run.call_args.args[0], web_app)
        self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")
        self.assertEqual(run.call_args.kwargs["workers"], 1)

    def test_container_preseeded_credentials_can_start_without_manual_secret(self) -> None:
        import app.main
        from app import cli

        web_app = SimpleNamespace(state=SimpleNamespace())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "config",
                cache_dir=root / "cache",
                log_dir=root / "logs",
                strm_dir=root / "strm-data",
                trash_dir=root / "trash",
            )
            with patch.dict(
                os.environ,
                {
                    "MEDIAFLUX_CONTAINER": "1",
                    "ENV_WEB_PASSPORT": "admin",
                    "ENV_WEB_PASSWORD": "correct-horse",
                },
                clear=True,
            ), patch("app.cli.get_runtime_paths", return_value=paths), patch(
                "app.modules.first_run.needs_initialization", return_value=False
            ), patch("app.main.app", web_app), patch("app.cli.uvicorn.run") as run:
                exit_code = cli._start(None, None, None)

        self.assertEqual(exit_code, 0)
        self.assertIs(run.call_args.args[0], web_app)
        run.assert_called_once()

    def test_dockerfile_marks_container_and_uses_fixed_internal_healthcheck(self) -> None:
        dockerfile = (self.PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("MEDIAFLUX_CONTAINER=1", dockerfile)
        self.assertIn("http://127.0.0.1:1258/readyz", dockerfile)
        self.assertNotRegex(dockerfile, r"os\.environ.*WEB_PORT")
        self.assertNotIn("127.0.0.1:1258/healthz", dockerfile)


if __name__ == "__main__":
    unittest.main()
