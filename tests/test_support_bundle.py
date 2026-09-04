from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.modules.runtime_diagnostics import DiagnosticCheck, DiagnosticReport
from app.modules.support_bundle import create_support_bundle
from app.runtime_paths import RuntimePaths
from app.version import BuildInfo


class SupportBundleTests(unittest.TestCase):
    def _paths(self, root: Path) -> RuntimePaths:
        return RuntimePaths(
            program_dir=root / "program",
            data_dir=root / "data",
            config_dir=root / "config",
            cache_dir=root / "cache",
            log_dir=root / "logs",
            strm_dir=root / "strm-data",
            trash_dir=root / "trash",
        )

    def test_bundle_rejects_authoritative_runtime_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._paths(Path(temporary))
            paths.ensure_writable_dirs()
            paths.database_path.write_bytes(b"database")
            paths.env_file.write_text("WEB_PORT=1258\n", encoding="utf-8")
            paths.token_file.write_text('{"token":"keep"}', encoding="utf-8")

            with patch("app.modules.support_bundle.run_diagnostics") as diagnostics:
                for target in (
                    paths.database_path,
                    paths.env_file,
                    paths.token_file,
                ):
                    with self.subTest(target=target.name):
                        original = target.read_bytes()
                        with self.assertRaisesRegex(
                            Exception,
                            "支持包输出不能覆盖 MediaFlux 运行文件",
                        ):
                            create_support_bundle(paths, output=target)
                        self.assertEqual(target.read_bytes(), original)
            diagnostics.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "文件别名验证仅在 POSIX 环境执行")
    def test_bundle_rejects_runtime_file_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            paths.ensure_writable_dirs()
            paths.token_file.write_text('{"token":"keep"}', encoding="utf-8")
            symlink = root / "token-alias.zip"
            symlink.symlink_to(paths.token_file)
            hardlink = root / "token-hardlink.zip"
            os.link(paths.token_file, hardlink)

            for target in (symlink, hardlink):
                with self.subTest(target=target.name):
                    with self.assertRaisesRegex(
                        Exception,
                        "支持包输出不能覆盖 MediaFlux 运行文件",
                    ):
                        create_support_bundle(paths, output=target)
            self.assertEqual(
                paths.token_file.read_text(encoding="utf-8"),
                '{"token":"keep"}',
            )

    def test_bundle_is_redacted_and_excludes_sensitive_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            paths.config_dir.mkdir(parents=True)
            paths.log_dir.mkdir(parents=True)
            paths.data_dir.mkdir(parents=True)
            paths.env_file.write_text(
                "WEB_PASSWORD=top-secret-password\n"
                "API_KEY=top-secret-key\n"
                "DOUBAN_DBCL2=top-secret-dbcl2\n"
                "TRACKER_PASSKEY=top-secret-passkey\n"
                "JELLYFIN_SESSION_ID=top-secret-session\n"
                "AGENT_LLM_API_URL=https://alice:url-secret@example.invalid/v1"
                "?token=query-secret\n"
                "NORMAL_VALUE=visible\n",
                encoding="utf-8",
            )
            paths.token_file.write_text('{"token":"top-secret-token"}', encoding="utf-8")
            paths.database_path.parent.mkdir(parents=True, exist_ok=True)
            paths.database_path.write_bytes(b"top-secret-database")
            (paths.log_dir / "app.log").write_text(
                "Authorization: Bearer top-secret-bearer\n"
                "Authorization: Token custom-auth-secret\nnormal log line\n",
                encoding="utf-8",
            )
            output = root / "support.zip"
            report = DiagnosticReport(
                (
                    DiagnosticCheck(
                        "runtime",
                        "ok",
                        "Authorization: Bearer xyz",
                    ),
                )
            )
            build = BuildInfo("MediaFlux", "1.2.3", "abc", "now", "3.12", "test", "source")

            with (
                patch("app.modules.support_bundle.run_diagnostics", return_value=report),
                patch("app.modules.support_bundle.BuildInfo.current", return_value=build),
            ):
                destination = create_support_bundle(paths, output=output)

            self.assertEqual(destination, output.resolve())
            with zipfile.ZipFile(destination) as archive:
                names = set(archive.namelist())
                self.assertEqual(
                    names,
                    {
                        "build-info.json",
                        "runtime.json",
                        "diagnostics.json",
                        "config-redacted.json",
                        "logs.txt",
                    },
                )
                payload = b"\n".join(archive.read(name) for name in sorted(names))
                config = json.loads(archive.read("config-redacted.json"))

            for secret in (
                b"top-secret-password",
                b"top-secret-key",
                b"top-secret-token",
                b"top-secret-database",
                b"top-secret-bearer",
                b"custom-auth-secret",
                b"top-secret-dbcl2",
                b"top-secret-passkey",
                b"top-secret-session",
                b"url-secret",
                b"query-secret",
                b"Bearer xyz",
            ):
                self.assertNotIn(secret, payload)
            self.assertEqual(config["values"]["WEB_PASSWORD"], "********")
            self.assertEqual(config["values"]["API_KEY"], "********")
            self.assertEqual(config["values"]["DOUBAN_DBCL2"], "********")
            self.assertEqual(config["values"]["TRACKER_PASSKEY"], "********")
            self.assertEqual(config["values"]["JELLYFIN_SESSION_ID"], "********")
            self.assertEqual(
                config["values"]["AGENT_LLM_API_URL"],
                "https://********@example.invalid/v1?token=********",
            )
            self.assertEqual(config["values"]["NORMAL_VALUE"], "visible")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_bundle_survives_missing_config_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            output = root / "support.zip"
            report = DiagnosticReport((DiagnosticCheck("runtime", "warning", "有限诊断"),))
            with patch("app.modules.support_bundle.run_diagnostics", return_value=report):
                create_support_bundle(paths, output=output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read("logs.txt"), b"")
                self.assertEqual(json.loads(archive.read("config-redacted.json"))["values"], {})

    @unittest.skipUnless(os.name == "posix", "符号链接保护仅在 POSIX 环境验证")
    def test_bundle_refuses_to_follow_log_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            paths.log_dir.mkdir(parents=True)
            external = root / "external-secret.log"
            external.write_text("token=symlink-secret-value\n", encoding="utf-8")
            (paths.log_dir / "app.log").symlink_to(external)
            output = root / "support.zip"
            report = DiagnosticReport((DiagnosticCheck("runtime", "ok", "正常"),))

            with patch("app.modules.support_bundle.run_diagnostics", return_value=report):
                create_support_bundle(paths, output=output)

            with zipfile.ZipFile(output) as archive:
                log_payload = archive.read("logs.txt")
            self.assertNotIn(b"symlink-secret-value", log_payload)
            self.assertIn("不是普通文件", log_payload.decode("utf-8"))

    def test_bundle_bounds_oversized_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            paths.config_dir.mkdir(parents=True)
            paths.env_file.write_bytes(b"SAFE_VALUE=" + b"x" * (1024 * 1024 + 1))
            output = root / "support.zip"
            report = DiagnosticReport((DiagnosticCheck("runtime", "ok", "正常"),))

            with patch("app.modules.support_bundle.run_diagnostics", return_value=report):
                create_support_bundle(paths, output=output)

            with zipfile.ZipFile(output) as archive:
                config = json.loads(archive.read("config-redacted.json"))
            self.assertEqual(config["values"], {})
            self.assertEqual(config["read_error"], "ConfigFileTooLargeError")

    @unittest.skipUnless(os.name == "posix", "符号链接保护仅在 POSIX 环境验证")
    def test_bundle_rejects_symlinked_output_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            real_output = root / "real-output"
            real_output.mkdir()
            linked_output = root / "linked-output"
            linked_output.symlink_to(real_output, target_is_directory=True)

            with self.assertRaisesRegex(Exception, "输出目录"):
                create_support_bundle(
                    paths, output=linked_output / "new-directory" / "support.zip"
                )
            self.assertFalse((real_output / "new-directory").exists())

    @unittest.skipUnless(os.name == "posix", "目录 FD 保护仅在 POSIX 环境验证")
    def test_log_directory_replacement_cannot_escape_open_directory_fd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            paths.log_dir.mkdir(parents=True)
            (paths.log_dir / "app.log").write_text("safe log\n", encoding="utf-8")
            external = root / "external"
            external.mkdir()
            (external / "app.log").write_text("external-secret\n", encoding="utf-8")
            original_open = __import__(
                "app.modules.support_bundle", fromlist=["_open_directory_fd"]
            )._open_directory_fd

            replaced = False

            def open_then_replace(path):
                nonlocal replaced
                descriptor = original_open(path)
                if path == paths.log_dir and not replaced:
                    moved = root / "logs-original"
                    paths.log_dir.rename(moved)
                    paths.log_dir.symlink_to(external, target_is_directory=True)
                    replaced = True
                return descriptor

            output = root / "support.zip"
            report = DiagnosticReport((DiagnosticCheck("runtime", "ok", "正常"),))
            with patch(
                "app.modules.support_bundle._open_directory_fd",
                side_effect=open_then_replace,
            ), patch(
                "app.modules.support_bundle.run_diagnostics", return_value=report
            ):
                create_support_bundle(paths, output=output)

            with zipfile.ZipFile(output) as archive:
                logs = archive.read("logs.txt")
            self.assertIn(b"safe log", logs)
            self.assertNotIn(b"external-secret", logs)

    def test_bundle_bounds_oversized_log_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            paths.log_dir.mkdir(parents=True)
            oversized = "x" * (1024 * 1024 + 100) + " token=oversized-secret"
            (paths.log_dir / "app.log").write_text(oversized, encoding="utf-8")
            output = root / "support.zip"
            report = DiagnosticReport((DiagnosticCheck("runtime", "ok", "正常"),))

            with patch("app.modules.support_bundle.run_diagnostics", return_value=report):
                create_support_bundle(paths, output=output)

            with zipfile.ZipFile(output) as archive:
                log_payload = archive.read("logs.txt")
            self.assertLess(len(log_payload), 4096)
            self.assertNotIn(b"oversized-secret", log_payload)
            self.assertIn("已截取末尾内容", log_payload.decode("utf-8"))

    def test_bundle_creates_missing_explicit_output_parents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            output = root / "exports" / "nested" / "support.zip"
            report = DiagnosticReport((DiagnosticCheck("runtime", "ok", "正常"),))

            with patch(
                "app.modules.support_bundle.run_diagnostics",
                return_value=report,
            ):
                result = create_support_bundle(paths, output=output)

            self.assertEqual(result, output)
            self.assertTrue(output.is_file())



if __name__ == "__main__":
    unittest.main()
