"""运行时路径消费者必须共用同一份 RuntimePaths。"""
from __future__ import annotations

import importlib
import logging
import os
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.runtime_paths import RuntimePaths, configure_runtime_paths

_CONSUMER_MODULES = (
    "app.config",
    "app.database",
    "app.logger",
    "app.clients.guangya",
)


class _RefreshingRawClient:
    """仅模拟 GuangYa SDK 刷新响应，持久化仍走真实 GuangYaClient。"""

    def __init__(self) -> None:
        self.token = "access-before-refresh"
        self.refresh_token_value = "refresh-before-refresh"
        self.device_id = "runtime-path-test-device"
        self.token_expires_at = None

    def refresh_token(self, _refresh_token=None):
        self.token = "access-after-refresh"
        self.refresh_token_value = "refresh-after-refresh"
        self.token_expires_at = 1_900_000_000
        return {
            "access_token": self.token,
            "refresh_token": self.refresh_token_value,
            "expires_at": self.token_expires_at,
        }


@contextmanager
def isolated_runtime_paths(root: Path):
    """用彼此独立的可写目录覆盖当前进程运行路径。"""
    paths = RuntimePaths(
        program_dir=root / "program",
        data_dir=root / "data",
        config_dir=root / "config",
        cache_dir=root / "cache",
        log_dir=root / "logs",
        strm_dir=root / "strm-data",
        trash_dir=root / "trash",
    )
    configure_runtime_paths(paths)
    try:
        yield paths
    finally:
        configure_runtime_paths(None)


def reload_runtime_modules():
    """按依赖顺序重新加载读取模块级路径常量的消费者。"""
    config = importlib.import_module("app.config")
    database = importlib.import_module("app.database")
    logger = importlib.import_module("app.logger")
    guangya = importlib.import_module("app.clients.guangya")
    return (
        importlib.reload(config),
        importlib.reload(database),
        importlib.reload(logger),
        importlib.reload(guangya),
    )


class RuntimePathConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._module_snapshots = {
            name: dict(module.__dict__) if (module := sys.modules.get(name)) else None
            for name in _CONSUMER_MODULES
        }
        self._root_logger = logging.getLogger()
        self._root_handlers = self._root_logger.handlers[:]
        for handler in self._root_handlers:
            self._root_logger.removeHandler(handler)

    def tearDown(self) -> None:
        for handler in self._root_logger.handlers[:]:
            self._root_logger.removeHandler(handler)
            handler.close()
        for handler in self._root_handlers:
            self._root_logger.addHandler(handler)

        configure_runtime_paths(None)
        for name in reversed(_CONSUMER_MODULES):
            snapshot = self._module_snapshots[name]
            module = sys.modules.get(name)
            if snapshot is None:
                sys.modules.pop(name, None)
                parent_name, attribute = name.rsplit(".", 1)
                parent = sys.modules.get(parent_name)
                if parent is not None and getattr(parent, attribute, None) is module:
                    delattr(parent, attribute)
                continue
            if module is not None:
                module.__dict__.clear()
                module.__dict__.update(snapshot)

    def test_database_logger_config_and_token_share_runtime_paths(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            with isolated_runtime_paths(root_path) as paths:
                # 测试命令会启用测试模式；这里验证消费者的真实写入位置。
                with patch.dict(
                    os.environ,
                    {"MEDIAFLUX_TEST_MODE": "0", "MEDIAFLUX_DISABLE_FILE_LOGGING": "0"},
                    clear=False,
                ):
                    config, database, logger, guangya = reload_runtime_modules()

                    config.set_and_save({"RUNTIME_PATH_CONSUMER_TEST": "1"})
                    database.init_db()
                    with database.get_conn() as connection:
                        self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

                    logger.get_logger(__name__).info("runtime-path-consumer-write")
                    for handler in logging.getLogger().handlers:
                        handler.flush()

                    client = guangya.GuangYaClient()
                    client._raw = _RefreshingRawClient()
                    client.refresh_now()

                    artifacts = (
                        config.ENV_FILE,
                        database.resolve_db_path(),
                        logger.LOG_DIR / "app.log",
                        guangya.TOKEN_FILE,
                    )
                    self.assertEqual(database.resolve_db_path(), paths.database_path)
                    self.assertEqual(config.DATA_DIR, paths.data_dir)
                    self.assertEqual(config.CONFIG_DIR, paths.config_dir)
                    self.assertEqual(config.ENV_FILE, paths.env_file)
                    self.assertEqual(logger.LOG_DIR, paths.log_dir)
                    self.assertEqual(guangya.TOKEN_FILE, paths.token_file)
                    self.assertEqual(client.token_file, paths.token_file)
                    for artifact in artifacts:
                        with self.subTest(artifact=artifact):
                            self.assertTrue(artifact.is_file())
                            self.assertTrue(artifact.is_relative_to(root_path))
                            self.assertFalse(artifact.is_relative_to(paths.program_dir))
                    if os.name == "posix":
                        for private_artifact in (
                            database.resolve_db_path(),
                            logger.LOG_DIR / "app.log",
                        ):
                            with self.subTest(private_mode=private_artifact):
                                self.assertEqual(
                                    stat.S_IMODE(private_artifact.stat().st_mode),
                                    0o600,
                                )
                    self.assertFalse(paths.program_dir.exists())

                with self.subTest("test mode rejects configured production database"):
                    with patch.dict(
                        os.environ,
                        {
                            "MEDIAFLUX_TEST_MODE": "1",
                            "MEDIAFLUX_TEST_DB_PATH": str(paths.database_path),
                        },
                        clear=False,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "生产数据库"):
                            database.resolve_db_path()


if __name__ == "__main__":
    unittest.main()
