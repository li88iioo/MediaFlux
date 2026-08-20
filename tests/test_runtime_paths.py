from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.runtime_paths import (
    RuntimePaths,
    configure_runtime_paths,
    get_runtime_paths,
)


class RuntimePathsTests(unittest.TestCase):
    def tearDown(self) -> None:
        configure_runtime_paths(None)

    def test_explicit_environment_overrides_platform_defaults(self) -> None:
        paths = RuntimePaths.from_environment({
            "MEDIAFLUX_DATA_DIR": "/srv/mediaflux/data",
            "MEDIAFLUX_CONFIG_DIR": "/srv/mediaflux/config",
            "MEDIAFLUX_CACHE_DIR": "/srv/mediaflux/cache",
            "MEDIAFLUX_LOG_DIR": "/srv/mediaflux/logs",
            "MEDIAFLUX_STRM_DIR": "/srv/mediaflux/strm",
        }, platform_name="Linux", frozen=True)

        self.assertEqual(paths.database_path, Path("/srv/mediaflux/data/db/mediaflux.db"))
        self.assertEqual(paths.env_file, Path("/srv/mediaflux/config/user.env"))
        self.assertEqual(paths.log_dir, Path("/srv/mediaflux/logs"))
        self.assertEqual(paths.trash_dir, Path("/srv/mediaflux/data/trash"))
        self.assertEqual(paths.token_dir, Path("/srv/mediaflux/data"))
        self.assertEqual(
            paths.token_file,
            Path("/srv/mediaflux/data/guangya_token.json"),
        )

    def test_shared_data_and_config_directory_preserves_flat_legacy_database(self) -> None:
        paths = RuntimePaths.from_environment({
            "MEDIAFLUX_DATA_DIR": "/app/db",
            "MEDIAFLUX_CONFIG_DIR": "/app/db",
            "MEDIAFLUX_CACHE_DIR": "/app/db/cache",
            "MEDIAFLUX_LOG_DIR": "/app/db/logs",
            "MEDIAFLUX_STRM_DIR": "/data/strm",
        }, platform_name="Linux", frozen=False)

        self.assertEqual(paths.database_path, Path("/app/db/mediaflux.db"))
        self.assertEqual(paths.env_file, Path("/app/db/user.env"))

    def test_container_frozen_defaults(self) -> None:
        paths = RuntimePaths.from_environment(
            {}, platform_name="Linux", frozen=True
        )

        self.assertEqual(paths.data_dir, Path("/app/db"))
        self.assertEqual(paths.config_dir, Path("/app/db"))
        self.assertEqual(paths.cache_dir, Path("/app/db/cache"))
        self.assertEqual(paths.log_dir, Path("/app/db/logs"))
        self.assertEqual(paths.strm_dir, Path("/data/strm"))

    def test_source_mode_preserves_project_root_layout(self) -> None:
        paths = RuntimePaths.from_environment({}, platform_name="Linux", frozen=False)
        project_root = Path(__file__).resolve().parents[1]

        self.assertEqual(paths.program_dir, project_root)
        self.assertEqual(paths.data_dir, project_root)
        self.assertEqual(paths.config_dir, project_root / "db")
        self.assertEqual(paths.cache_dir, project_root / "db" / "cache")
        self.assertEqual(paths.log_dir, project_root / "db" / "logs")
        self.assertEqual(paths.strm_dir, project_root / "strm-data")
        self.assertEqual(paths.trash_dir, project_root / "db" / "trash")
        self.assertEqual(paths.database_path, project_root / "db" / "mediaflux.db")
        self.assertEqual(paths.env_file, project_root / "db" / "user.env")
        self.assertEqual(paths.token_dir, project_root / "db")
        self.assertEqual(paths.token_file, project_root / "db" / "guangya_token.json")

    def test_staged_source_runtime_keeps_token_in_config_db(self) -> None:
        source_root = Path("/srv/mediaflux-source")
        paths = RuntimePaths(
            program_dir=Path("/tmp/mediaflux-runtime"),
            data_dir=source_root,
            config_dir=source_root / "db",
            cache_dir=source_root / "db" / "cache",
            log_dir=source_root / "db" / "logs",
            strm_dir=source_root / "strm-data",
            trash_dir=source_root / "db" / "trash",
        )

        self.assertEqual(paths.token_dir, source_root / "db")
        self.assertEqual(paths.token_file, source_root / "db" / "guangya_token.json")

    def test_source_mode_data_override_moves_trash_directory(self) -> None:
        paths = RuntimePaths.from_environment(
            {"MEDIAFLUX_DATA_DIR": "/srv/mediaflux/data"},
            platform_name="Linux",
            frozen=False,
        )

        self.assertEqual(paths.data_dir, Path("/srv/mediaflux/data"))
        self.assertEqual(paths.trash_dir, Path("/srv/mediaflux/data/trash"))
        self.assertEqual(paths.token_dir, Path("/srv/mediaflux/data"))
        self.assertEqual(
            paths.token_file,
            Path("/srv/mediaflux/data/guangya_token.json"),
        )

    def test_relative_directory_overrides_are_rejected(self) -> None:
        for variable in (
            "MEDIAFLUX_DATA_DIR",
            "MEDIAFLUX_CONFIG_DIR",
            "MEDIAFLUX_CACHE_DIR",
            "MEDIAFLUX_LOG_DIR",
            "MEDIAFLUX_STRM_DIR",
        ):
            with self.subTest(variable=variable):
                with self.assertRaisesRegex(ValueError, variable):
                    RuntimePaths.from_environment(
                        {variable: "relative/path"},
                        platform_name="Linux",
                        frozen=True,
                    )

    def test_ensure_writable_dirs_never_creates_program_directory(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            program_dir = root_path / "readonly-program"
            paths = RuntimePaths(
                program_dir=program_dir,
                data_dir=root_path / "data",
                config_dir=root_path / "config",
                cache_dir=root_path / "cache",
                log_dir=root_path / "logs",
                strm_dir=root_path / "strm-data",
                trash_dir=root_path / "trash",
            )

            paths.ensure_writable_dirs()

            self.assertFalse(program_dir.exists())
            for writable_dir in (
                paths.data_dir,
                paths.database_path.parent,
                paths.config_dir,
                paths.cache_dir,
                paths.log_dir,
                paths.strm_dir,
                paths.trash_dir,
            ):
                self.assertTrue(writable_dir.is_dir())

    def test_configured_paths_override_environment_lookup(self) -> None:
        configured = RuntimePaths(
            program_dir=Path("/program"),
            data_dir=Path("/data"),
            config_dir=Path("/config"),
            cache_dir=Path("/cache"),
            log_dir=Path("/logs"),
            strm_dir=Path("/strm-data"),
            trash_dir=Path("/trash"),
        )

        configure_runtime_paths(configured)

        self.assertIs(get_runtime_paths(), configured)


if __name__ == "__main__":
    unittest.main()
