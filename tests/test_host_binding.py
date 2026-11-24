from __future__ import annotations

import importlib
import os
import sys
from contextlib import ExitStack
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.runtime_paths import RuntimePaths, configure_runtime_paths


class MainHostBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import config, database, runtime_paths
        from app.modules import first_run, web_secret

        stack = ExitStack()
        self.addCleanup(stack.close)
        self.temp = stack.enter_context(tempfile.TemporaryDirectory())
        root = Path(self.temp)
        # reload mutates module dictionaries in place. Restore the actual prior
        # bindings, not a new application against the suite's schema-only DB.
        stack.enter_context(patch.dict(os.environ))
        stack.callback(configure_runtime_paths, runtime_paths._configured_paths)
        for module in (config, database, web_secret, first_run):
            stack.enter_context(patch.dict(module.__dict__))
        previous_main = sys.modules.get("app.main")
        if previous_main is not None:
            stack.enter_context(patch.dict(previous_main.__dict__))
        else:
            def remove_new_main():
                import app
                sys.modules.pop("app.main", None)
                app.__dict__.pop("main", None)
            stack.callback(remove_new_main)
        stack.callback(self._release_fixture_app)
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

    def _release_fixture_app(self) -> None:
        main = getattr(self, "main", None)
        if main is not None:
            release = getattr(main.app.state, "release_startup_lifecycle_guard", None)
            if release is not None:
                release()

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
        import app.main as main
        self.main = importlib.reload(main)

    def test_main_run_fresh_defaults_to_loopback(self):
        with patch("app.main.uvicorn.run") as run:
            self.main.run()

        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        self.assertIsNone(run.call_args.kwargs["log_config"])
        self.assertFalse(run.call_args.kwargs["access_log"])

    def test_main_run_rejects_remote_fresh_host_and_allows_initialized_persisted_host(self):
        os.environ["WEB_HOST"] = "0.0.0.0"
        self._reload_modules()
        with patch("app.main.uvicorn.run") as run, self.assertRaises(
            self.main.first_run.UnsafeFirstRunBindingError
        ):
            self.main.run()
        run.assert_not_called()

        os.environ.pop("WEB_HOST", None)
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        self.paths.env_file.write_text(
            "WEB_HOST=0.0.0.0\nMEDIAFLUX_INITIALIZED=1\n"
            "ENV_WEB_PASSPORT=admin\nENV_WEB_PASSWORD=correct-horse\n",
            encoding="utf-8",
        )
        # 已有账号与真实 legacy DB 走正常迁移，不能套用新安装事务。
        from app import database
        from app.env_file import read_env_bytes

        database.init_db()
        database_inode = self.paths.database_path.stat().st_ino
        self._reload_modules()
        values = read_env_bytes(self.paths.env_file.read_bytes())
        self.assertEqual(values["ENV_WEB_PASSPORT"], "admin")
        self.assertEqual(values["ENV_WEB_PASSWORD"], "correct-horse")
        self.assertEqual(self.paths.database_path.stat().st_ino, database_inode)
        with patch("app.main.uvicorn.run") as run:
            self.main.run()
        self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
