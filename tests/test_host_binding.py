from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.runtime_paths import RuntimePaths, configure_runtime_paths


class MainHostBindingTests(unittest.TestCase):
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
        # 本测试会 reload 持有运行路径常量的模块；恢复默认路径后也必须再次
        # reload，避免后续测试继续引用已经清理的临时目录。
        self._reload_modules()
        self.temp.cleanup()

    def _reload_modules(self) -> None:
        import app.config as config
        import app.database as database
        import app.modules.first_run as first_run
        import app.modules.web_secret as web_secret
        import app.main as main

        importlib.reload(config)
        importlib.reload(database)
        importlib.reload(web_secret)
        importlib.reload(first_run)
        first_run._reset_startup_state_for_tests()
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
        self._reload_modules()
        with patch("app.main.uvicorn.run") as run:
            self.main.run()
        self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
