"""运行时安装、权限与依赖诊断。"""
from __future__ import annotations

import errno
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.runtime_paths import RuntimePaths


class RuntimeDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.program_dir = self.root / "program"
        self.data_dir = self.root / "data"
        self.database_dir = self.data_dir / "db"
        self.database_path = self.database_dir / "mediaflux.db"
        self.source = self.root / "source"
        self.library = self.root / "library"
        for directory in (
            self.program_dir,
            self.database_dir,
            self.source,
            self.library,
        ):
            directory.mkdir(parents=True)
        self.paths = RuntimePaths(
            program_dir=self.program_dir,
            data_dir=self.data_dir,
            config_dir=self.data_dir / "config",
            cache_dir=self.data_dir / "cache",
            log_dir=self.data_dir / "logs",
            strm_dir=self.data_dir / "strm-data",
            trash_dir=self.data_dir / "trash",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _run_with_external_checks_ok(self, **kwargs):
        from app.modules.runtime_diagnostics import run_diagnostics

        with (
            patch("app.modules.runtime_diagnostics._is_port_available", return_value=True),
            patch("app.modules.runtime_diagnostics.shutil.which", return_value="/usr/bin/ffprobe"),
        ):
            return run_diagnostics(self.paths, **kwargs)

    def test_ffprobe_diagnostic_honors_explicit_packaged_path(self) -> None:
        from app.modules.runtime_diagnostics import _ffprobe_check

        executable = self.root / "tools" / "ffprobe"
        executable.parent.mkdir()
        executable.write_bytes(b"binary")
        with patch.dict(os.environ, {"MEDIAFLUX_FFPROBE": str(executable)}, clear=False):
            check = _ffprobe_check()
        self.assertEqual(check.status, "ok")
        self.assertIn(str(executable), check.message)

    def test_report_is_immutable_rejects_duplicate_or_unknown_keys_and_returns_copies(self) -> None:
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        first = DiagnosticCheck("database_dir", "ok", "数据库目录可写")
        second = DiagnosticCheck("ffprobe", "warning", "未找到 ffprobe", "安装 FFmpeg")
        input_checks = [first, second]
        report = DiagnosticReport(input_checks)
        input_checks.clear()

        self.assertIs(report.check("database_dir"), first)
        self.assertEqual(report.checks, (first, second))
        serialized = report.as_dict()
        serialized["checks"][0]["status"] = "error"
        self.assertEqual(report.check("database_dir").status, "ok")
        with self.assertRaises(KeyError):
            report.check("missing")
        with self.assertRaises(ValueError):
            DiagnosticReport((first, first))
        with self.assertRaises(FrozenInstanceError):
            first.status = "error"  # type: ignore[misc]

    def test_doctor_reports_writable_data_and_media_permissions_without_leftover_probes(self) -> None:
        report = self._run_with_external_checks_ok(
            source_paths=[self.source],
            target_paths=[self.library],
        )

        self.assertEqual(report.check("database_dir").status, "ok")
        self.assertEqual(report.check("source_readable").status, "ok")
        self.assertEqual(report.check("library_writable").status, "ok")
        self.assertEqual(list(self.database_dir.glob(".mediaflux-diagnostic-*")), [])
        self.assertEqual(list(self.library.glob(".mediaflux-diagnostic-*")), [])

    def test_doctor_rejects_symlink_source_target_and_database_file(self) -> None:
        source_link = self.root / "source-link"
        target_link = self.root / "target-link"
        database_target = self.root / "outside.db"
        database_target.touch()
        try:
            source_link.symlink_to(self.source, target_is_directory=True)
            target_link.symlink_to(self.library, target_is_directory=True)
            self.database_path.symlink_to(database_target)
        except OSError as exc:
            self.skipTest(f"symlink is unavailable: {exc}")

        report = self._run_with_external_checks_ok(
            source_paths=[source_link],
            target_paths=[target_link],
        )

        self.assertEqual(report.check("source_readable").status, "error")
        self.assertEqual(report.check("library_writable").status, "error")
        self.assertEqual(report.check("database_dir").status, "error")
        self.assertIn("符号链接", report.check("source_readable").message)

    def test_special_files_are_reported_without_blocking(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO is unavailable on this platform")
        fifo = self.root / "media.fifo"
        os.mkfifo(fifo)

        started = time.monotonic()
        report = self._run_with_external_checks_ok(source_paths=[fifo], target_paths=[fifo])
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1)
        self.assertEqual(report.check("source_readable").status, "error")
        self.assertEqual(report.check("library_writable").status, "error")
        self.assertIn("特殊文件", report.check("source_readable").message)

    def test_source_directory_requires_search_or_traverse_permission(self) -> None:
        if os.name != "posix":
            self.skipTest("effective identity traversal checks are POSIX-specific")
        with patch("app.modules.runtime_diagnostics.os.access", return_value=False):
            report = self._run_with_external_checks_ok(source_paths=[self.source])

        self.assertEqual(report.check("source_readable").status, "error")
        self.assertIn("搜索", report.check("source_readable").message)

    def test_source_directory_must_enumerate_not_only_open_scandir(self) -> None:
        class Entries:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def __iter__(self):
                return self

            def __next__(self):
                raise PermissionError("cannot enumerate directory")

        with (
            patch("app.modules.runtime_diagnostics._directory_access", return_value=True),
            patch("app.modules.runtime_diagnostics.os.scandir", return_value=Entries()),
        ):
            report = self._run_with_external_checks_ok(source_paths=[self.source])

        self.assertEqual(report.check("source_readable").status, "error")
        self.assertIn("cannot enumerate", report.check("source_readable").message)

    def test_existing_database_file_requires_safe_write_open(self) -> None:
        self.database_path.write_bytes(b"SQLite format 3\x00")
        real_open = os.open

        def deny_database_open(path, flags, *args, **kwargs):
            if Path(path) == self.database_path or (
                path == self.database_path.name and kwargs.get("dir_fd") is not None
            ):
                raise PermissionError("read-only database")
            return real_open(path, flags, *args, **kwargs)

        with patch("app.modules.runtime_diagnostics.os.open", side_effect=deny_database_open):
            report = self._run_with_external_checks_ok()

        self.assertEqual(report.check("database_dir").status, "error")
        self.assertIn("数据库文件", report.check("database_dir").message)

    def test_missing_database_directory_reports_creatable_first_run(self) -> None:
        missing_data_dir = self.root / "new-data"
        paths = RuntimePaths(
            program_dir=self.program_dir,
            data_dir=missing_data_dir,
            config_dir=missing_data_dir / "config",
            cache_dir=missing_data_dir / "cache",
            log_dir=missing_data_dir / "logs",
            strm_dir=missing_data_dir / "strm-data",
            trash_dir=missing_data_dir / "trash",
        )
        from app.modules.runtime_diagnostics import run_diagnostics

        with (
            patch("app.modules.runtime_diagnostics._is_port_available", return_value=True),
            patch("app.modules.runtime_diagnostics.shutil.which", return_value="/usr/bin/ffprobe"),
        ):
            report = run_diagnostics(paths)

        self.assertEqual(report.check("database_dir").status, "ok")
        self.assertIn("尚未创建但可创建", report.check("database_dir").message)
        self.assertFalse(missing_data_dir.exists())

    def test_no_media_paths_are_warnings_that_explicitly_state_not_verified(self) -> None:
        report = self._run_with_external_checks_ok()

        self.assertEqual(report.check("source_readable").status, "warning")
        self.assertEqual(report.check("library_writable").status, "warning")
        self.assertIn("未验证", report.check("source_readable").message)
        self.assertIn("未验证", report.check("library_writable").message)

    def test_root_execution_identity_is_a_warning(self) -> None:
        with (
            patch("app.modules.runtime_diagnostics.os.geteuid", return_value=0, create=True),
            patch("app.modules.runtime_diagnostics.getpass.getuser", return_value="root"),
        ):
            report = self._run_with_external_checks_ok()

        identity = report.check("execution_identity")
        self.assertEqual(identity.status, "warning")
        self.assertIn("当前执行身份", identity.message)
        self.assertIn("root", identity.message)

    def test_port_parameters_are_forwarded_without_host_assumptions(self) -> None:
        from app.modules.runtime_diagnostics import run_diagnostics

        for host in ("0.0.0.0", "127.0.0.8", "::1"):
            with self.subTest(host=host):
                with (
                    patch("app.modules.runtime_diagnostics._is_port_available", return_value=True) as available,
                    patch("app.modules.runtime_diagnostics.shutil.which", return_value="/usr/bin/ffprobe"),
                ):
                    report = run_diagnostics(self.paths, host=host, port=24567)

                self.assertEqual(report.check("default_service_port").status, "ok")
                available.assert_called_once_with(host, 24567)

    def test_port_probe_uses_ipv6_address_family_and_context_cleanup(self) -> None:
        from app.modules.runtime_diagnostics import _is_port_available

        listener = MagicMock()
        listener.__enter__.return_value = listener
        with (
            patch(
                "app.modules.runtime_diagnostics.socket.getaddrinfo",
                return_value=[(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 24567, 0, 0))],
            ),
            patch("app.modules.runtime_diagnostics.socket.socket", return_value=listener) as socket_factory,
        ):
            self.assertTrue(_is_port_available("::1", 24567))

        socket_factory.assert_called_once_with(socket.AF_INET6, socket.SOCK_STREAM, 0)
        listener.bind.assert_called_once_with(("::1", 24567, 0, 0))
        listener.close.assert_called_once()

    def test_cli_doctor_json_uses_runtime_paths_and_returns_error_exit_code(self) -> None:
        from app.cli import main
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        report = DiagnosticReport(
            (
                DiagnosticCheck("database_dir", "error", "数据库目录不可写", "检查目录权限"),
            )
        )
        output = io.StringIO()
        with (
            patch("app.runtime_paths.get_runtime_paths", return_value=self.paths),
            patch("app.modules.runtime_diagnostics.run_diagnostics", return_value=report) as diagnostics,
            redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "doctor",
                    "--json",
                    "--source",
                    str(self.source),
                    "--target",
                    str(self.library),
                    "--host",
                    "127.0.0.8",
                    "--port",
                    "24567",
                ]
            )

        self.assertEqual(exit_code, 1)
        diagnostics.assert_called_once_with(
            self.paths,
            source_paths=(self.source,),
            target_paths=(self.library,),
            host="127.0.0.8",
            port=24567,
        )
        self.assertEqual(json.loads(output.getvalue())[0]["status"], "error")

    def test_mediaflux_doctor_json_subprocess_uses_temporary_runtime_paths(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            for directory in (
                runtime_root / "program",
                runtime_root / "data" / "db",
                runtime_root / "cache",
                runtime_root / "logs",
                runtime_root / "strm-data",
                runtime_root / "trash",
            ):
                directory.mkdir(parents=True, exist_ok=True)
            bootstrap = runtime_root / "bootstrap"
            bootstrap.mkdir()
            (bootstrap / "sitecustomize.py").write_text(
                "import os\n"
                "from pathlib import Path\n"
                "from app.runtime_paths import RuntimePaths, configure_runtime_paths\n"
                "root = Path(os.environ['TASK4_RUNTIME_ROOT'])\n"
                "configure_runtime_paths(RuntimePaths(\n"
                "    program_dir=root / 'program',\n"
                "    data_dir=root / 'data',\n"
                "    config_dir=root / 'config',\n"
                "    cache_dir=root / 'cache',\n"
                "    log_dir=root / 'logs',\n"
                "    strm_dir=root / 'strm-data',\n"
                "    trash_dir=root / 'trash',\n"
                "))\n"
            )
            environment = os.environ.copy()
            environment["TASK4_RUNTIME_ROOT"] = str(runtime_root)
            environment.pop("WEB_HOST", None)
            environment.pop("WEB_PORT", None)
            environment["PYTHONPATH"] = os.pathsep.join(
                (str(bootstrap), str(project_root), environment.get("PYTHONPATH", ""))
            )
            completed = subprocess.run(
                [sys.executable, "mediaflux.py", "doctor", "--json"],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            checks = json.loads(completed.stdout)
            self.assertIsInstance(checks, list)
            self.assertTrue(checks)
            self.assertFalse((runtime_root / "config").exists())
            self.assertEqual(list(runtime_root.rglob(".mediaflux-diagnostic-*")), [])


    def test_ancestor_symlinks_are_rejected_without_outside_probes(self) -> None:
        outside = self.root / "outside"
        outside_source = outside / "source"
        outside_library = outside / "library"
        outside_database_dir = outside / "data" / "db"
        for directory in (outside_source, outside_library, outside_database_dir):
            directory.mkdir(parents=True, exist_ok=True)
        source_alias = self.root / "source-alias"
        library_alias = self.root / "library-alias"
        data_alias = self.root / "data-alias"
        try:
            source_alias.symlink_to(outside, target_is_directory=True)
            library_alias.symlink_to(outside, target_is_directory=True)
            data_alias.symlink_to(outside / "data", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink is unavailable: {exc}")
        paths = RuntimePaths(
            program_dir=self.program_dir,
            data_dir=data_alias,
            config_dir=self.data_dir / "config",
            cache_dir=self.data_dir / "cache",
            log_dir=self.data_dir / "logs",
            strm_dir=self.data_dir / "strm-data",
            trash_dir=self.data_dir / "trash",
        )

        from app.modules.runtime_diagnostics import run_diagnostics

        with (
            patch("app.modules.runtime_diagnostics._is_port_available", return_value=True),
            patch("app.modules.runtime_diagnostics.shutil.which", return_value="/usr/bin/ffprobe"),
        ):
            report = run_diagnostics(
                paths,
                source_paths=[source_alias / "source"],
                target_paths=[library_alias / "library"],
            )

        self.assertEqual(report.check("database_dir").status, "error")
        self.assertEqual(report.check("source_readable").status, "error")
        self.assertEqual(report.check("library_writable").status, "error")
        self.assertEqual(list(outside.rglob(".mediaflux-diagnostic-*")), [])

    def test_string_backend_rejects_ancestor_symlink_contract(self) -> None:
        outside = self.root / "outside-string-backend"
        outside.mkdir()
        alias = self.root / "alias-string-backend"
        try:
            alias.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink is unavailable: {exc}")

        with patch("app.modules.runtime_diagnostics._uses_dir_fd", return_value=False):
            report = self._run_with_external_checks_ok(target_paths=[alias / "library"])

        self.assertEqual(report.check("library_writable").status, "error")
        self.assertEqual(list(outside.rglob(".mediaflux-diagnostic-*")), [])

    def test_paths_with_parent_components_are_rejected(self) -> None:
        report = self._run_with_external_checks_ok(
            source_paths=[self.source / ".." / "source"],
            target_paths=[self.library / ".." / "library"],
        )

        self.assertEqual(report.check("source_readable").status, "error")
        self.assertEqual(report.check("library_writable").status, "error")
        self.assertIn("..", report.check("source_readable").message)

    def test_posix_probe_uses_dir_fd_and_closes_opened_descriptors(self) -> None:
        if os.name != "posix":
            self.skipTest("descriptor-relative probes are POSIX-specific")
        from app.modules.runtime_diagnostics import _probe_writable

        nested = self.root / "nested" / "parent" / "library"
        nested.mkdir(parents=True)
        real_open = os.open
        real_close = os.close
        opened: list[int] = []
        closed: list[int] = []

        def capture_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        def capture_close(descriptor: int):
            closed.append(descriptor)
            return real_close(descriptor)

        with (
            patch("app.modules.runtime_diagnostics.os.open", side_effect=capture_open) as open_mock,
            patch("app.modules.runtime_diagnostics.os.close", side_effect=capture_close),
        ):
            writable, detail = _probe_writable(nested)

        self.assertTrue(writable, detail)
        self.assertTrue(any("dir_fd" in call.kwargs for call in open_mock.call_args_list))
        self.assertTrue(set(opened).issubset(set(closed)))

    def test_existing_database_requires_read_write_open(self) -> None:
        self.database_path.write_bytes(b"SQLite format 3\x00")
        real_open = os.open
        database_flags: list[int] = []

        def capture_database_open(path, flags, *args, **kwargs):
            if Path(path) == self.database_path or (
                path == self.database_path.name and kwargs.get("dir_fd") is not None
            ):
                database_flags.append(flags)
            return real_open(path, flags, *args, **kwargs)

        with patch("app.modules.runtime_diagnostics.os.open", side_effect=capture_database_open):
            report = self._run_with_external_checks_ok()

        self.assertEqual(report.check("database_dir").status, "ok")
        self.assertTrue(database_flags)
        self.assertTrue(database_flags[-1] & os.O_RDWR)

    def test_windows_execution_identity_reports_elevation_and_unknown_states(self) -> None:
        from app.modules.runtime_diagnostics import _execution_identity_check

        cases = ((True, "warning"), (False, "ok"), (None, "warning"))
        for elevated, expected_status in cases:
            with self.subTest(elevated=elevated):
                with (
                    patch("app.modules.runtime_diagnostics._is_windows", return_value=True),
                    patch("app.modules.runtime_diagnostics._windows_elevation_state", return_value=elevated),
                    patch("app.modules.runtime_diagnostics.getpass.getuser", return_value="WindowsUser"),
                ):
                    check = _execution_identity_check()

                self.assertEqual(check.status, expected_status)
                self.assertIn("当前执行身份", check.message)

    def test_port_probe_rejects_conflict_after_another_address_binds_and_closes_all(self) -> None:
        from app.modules.runtime_diagnostics import _is_port_available

        first = MagicMock()
        second = MagicMock()
        first.__enter__.return_value = first
        second.__enter__.return_value = second
        second.bind.side_effect = OSError(errno.EADDRINUSE, "in use")
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("0.0.0.0", 24567)),
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::", 24567, 0, 0)),
        ]
        with (
            patch("app.modules.runtime_diagnostics.socket.getaddrinfo", return_value=addresses) as getaddrinfo,
            patch("app.modules.runtime_diagnostics.socket.socket", side_effect=[first, second]),
        ):
            self.assertFalse(_is_port_available("0.0.0.0", 24567))

        self.assertEqual(getaddrinfo.call_args.kwargs["flags"], socket.AI_PASSIVE)
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_port_probe_skips_unavailable_address_sets_ipv6_only_and_closes_all(self) -> None:
        from app.modules.runtime_diagnostics import _is_port_available

        unavailable = MagicMock()
        available = MagicMock()
        unavailable.__enter__.return_value = unavailable
        available.__enter__.return_value = available
        unavailable.bind.side_effect = OSError(errno.EADDRNOTAVAIL, "not available")
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.2", 24567)),
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 24567, 0, 0)),
        ]
        with (
            patch("app.modules.runtime_diagnostics.socket.getaddrinfo", return_value=addresses),
            patch("app.modules.runtime_diagnostics.socket.socket", side_effect=[unavailable, available]),
        ):
            self.assertTrue(_is_port_available("::1", 24567))

        available.setsockopt.assert_called_with(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        unavailable.close.assert_called_once()
        available.close.assert_called_once()

    def test_diagnostic_models_validate_runtime_fields_and_report_elements(self) -> None:
        from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport

        for arguments in (
            ("", "ok", "message"),
            ("key", "unknown", "message"),
            ("key", "ok", 17),
            (17, "ok", "message"),
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises((TypeError, ValueError)):
                    DiagnosticCheck(*arguments)
        with self.assertRaises(TypeError):
            DiagnosticReport((object(),))



    def test_open_directory_at_closes_descriptor_when_fstat_fails(self) -> None:
        from app.modules.runtime_diagnostics import _open_directory_at

        descriptor = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        real_close = os.close
        try:
            with (
                patch("app.modules.runtime_diagnostics.os.open", return_value=descriptor),
                patch("app.modules.runtime_diagnostics.os.fstat", side_effect=OSError("fstat failed")),
                patch("app.modules.runtime_diagnostics.os.close", wraps=real_close) as close,
            ):
                self.assertIsNone(_open_directory_at(123, "directory"))

            close.assert_called_once_with(descriptor)
        finally:
            try:
                os.fstat(descriptor)
            except OSError:
                pass
            else:
                os.close(descriptor)

    def test_port_probe_skips_socket_creation_failure_and_binds_later_address(self) -> None:
        from app.modules.runtime_diagnostics import _is_port_available

        listener = MagicMock()
        addresses = [
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 24567, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 24567)),
        ]
        with (
            patch("app.modules.runtime_diagnostics.socket.getaddrinfo", return_value=addresses),
            patch(
                "app.modules.runtime_diagnostics.socket.socket",
                side_effect=[OSError(errno.EAFNOSUPPORT, "unsupported family"), listener],
            ),
        ):
            self.assertTrue(_is_port_available("localhost", 24567))

        listener.bind.assert_called_once_with(("127.0.0.1", 24567))
        listener.close.assert_called_once()

    def test_mediaflux_doctor_configuration_failures_are_stable_json_subprocesses(self) -> None:
        project_root = Path(__file__).resolve().parents[1]

        def assert_runtime_config_error(completed: subprocess.CompletedProcess[str]) -> None:
            self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
            checks = json.loads(completed.stdout)
            self.assertTrue(any(check["key"] == "runtime_config" and check["status"] == "error" for check in checks))
            self.assertNotIn("Traceback", completed.stderr)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            working_directory = root / "relative-config-cwd"
            working_directory.mkdir()
            environment = os.environ.copy()
            environment.update(
                {
                    "MEDIAFLUX_DATA_DIR": str(root / "data"),
                    "MEDIAFLUX_CONFIG_DIR": "relative-config",
                }
            )
            completed = subprocess.run(
                [sys.executable, str(project_root / "mediaflux.py"), "doctor", "--json"],
                cwd=working_directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            assert_runtime_config_error(completed)
            self.assertFalse((working_directory / "relative-config").exists())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for directory in (
                root / "program",
                root / "data" / "db",
                root / "config",
                root / "cache",
                root / "logs",
                root / "strm-data",
                root / "trash",
            ):
                directory.mkdir(parents=True, exist_ok=True)
            (root / "config" / "user.env").write_bytes(b"WEB_HOST=\xff\n")
            bootstrap = root / "bootstrap"
            bootstrap.mkdir()
            (bootstrap / "sitecustomize.py").write_text(
                "import os\n"
                "from pathlib import Path\n"
                "from app.runtime_paths import RuntimePaths, configure_runtime_paths\n"
                "root = Path(os.environ['TASK4_RUNTIME_ROOT'])\n"
                "configure_runtime_paths(RuntimePaths(\n"
                "    program_dir=root / 'program',\n"
                "    data_dir=root / 'data',\n"
                "    config_dir=root / 'config',\n"
                "    cache_dir=root / 'cache',\n"
                "    log_dir=root / 'logs',\n"
                "    strm_dir=root / 'strm-data',\n"
                "    trash_dir=root / 'trash',\n"
                "))\n"
            )
            environment = os.environ.copy()
            environment["TASK4_RUNTIME_ROOT"] = str(root)
            environment["PYTHONPATH"] = os.pathsep.join(
                (str(bootstrap), str(project_root), environment.get("PYTHONPATH", ""))
            )
            completed = subprocess.run(
                [sys.executable, "mediaflux.py", "doctor", "--json"],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            assert_runtime_config_error(completed)
            self.assertEqual(list(root.rglob(".mediaflux-diagnostic-*")), [])



if __name__ == "__main__":
    unittest.main()
