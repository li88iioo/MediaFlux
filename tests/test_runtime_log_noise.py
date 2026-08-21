"""运行期日志噪声与误报的回归测试。

覆盖三个真实观察到的问题：
- SQLite sidecar 在 lstat 与 open 之间消失时误报权限收紧失败
- 短命光鸭客户端反复以 INFO 记录同一 token
- 重复启动实例时提示语错误引用了「恢复备份」场景
"""
from __future__ import annotations

import errno
import logging
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import private_files
from app.private_files import protect_private_file, protect_sqlite_files


class PrivateFileRaceTests(unittest.TestCase):
    """文件在检查与打开之间消失不算失败。"""

    def test_missing_file_is_a_no_op(self):
        with TemporaryDirectory() as root:
            self.assertTrue(protect_private_file(Path(root) / "absent.db"))

    def test_file_vanishing_before_open_is_not_a_failure(self):
        with TemporaryDirectory() as root:
            target = Path(root) / "mediaflux.db-wal"
            target.write_text("wal", encoding="utf-8")

            real_open = os.open

            def vanishing_open(path, *args, **kwargs):
                if str(path) == str(target):
                    raise FileNotFoundError(2, "No such file or directory")
                return real_open(path, *args, **kwargs)

            with patch.object(private_files.os, "open", side_effect=vanishing_open):
                self.assertTrue(protect_private_file(target))

    def test_other_open_errors_still_report_failure(self):
        with TemporaryDirectory() as root:
            target = Path(root) / "mediaflux.db"
            target.write_text("db", encoding="utf-8")

            with patch.object(
                private_files.os, "open", side_effect=PermissionError(13, "denied")
            ):
                self.assertFalse(protect_private_file(target))

    def test_sqlite_sidecars_missing_do_not_fail_the_batch(self):
        with TemporaryDirectory() as root:
            database = Path(root) / "mediaflux.db"
            database.write_text("db", encoding="utf-8")

            # 只有主库存在，-wal/-shm 缺席属于正常状态。
            self.assertTrue(protect_sqlite_files(database))

    @unittest.skipIf(os.name == "nt", "POSIX fchmod 挂载兼容合同")
    def test_fchmod_eperm_accepts_a_file_that_is_already_private(self):
        with TemporaryDirectory() as root:
            target = Path(root) / "private.db"
            target.write_text("db", encoding="utf-8")
            target.chmod(0o600)

            with patch.object(
                private_files.os,
                "fchmod",
                side_effect=PermissionError(errno.EPERM, "operation not permitted"),
            ):
                self.assertTrue(protect_private_file(target))

    @unittest.skipIf(os.name == "nt", "POSIX fchmod 挂载兼容合同")
    def test_fchmod_eperm_rejects_a_file_visible_to_other_users(self):
        with TemporaryDirectory() as root:
            target = Path(root) / "public.db"
            target.write_text("db", encoding="utf-8")
            target.chmod(0o644)

            with patch.object(
                private_files.os,
                "fchmod",
                side_effect=PermissionError(errno.EPERM, "operation not permitted"),
            ):
                self.assertFalse(protect_private_file(target))


class GuangYaTokenLogNoiseTests(unittest.TestCase):
    """同一 token 反复加载不得刷屏。"""

    def setUp(self) -> None:
        from app.clients import guangya

        self.guangya = guangya
        self._previous = guangya._LAST_LOGGED_TOKEN_FINGERPRINT
        guangya._LAST_LOGGED_TOKEN_FINGERPRINT = ""

    def tearDown(self) -> None:
        self.guangya._LAST_LOGGED_TOKEN_FINGERPRINT = self._previous

    def _load(self, fingerprint: str) -> list[logging.LogRecord]:
        with self.assertLogs("app.clients.guangya", level="DEBUG") as captured:
            self.guangya._log_token_loaded(fingerprint)
        return captured.records

    def test_first_load_is_reported_at_info(self):
        records = self._load("fp-a")

        self.assertEqual([record.levelname for record in records], ["INFO"])

    def test_repeated_identical_token_drops_to_debug(self):
        self._load("fp-a")

        records = self._load("fp-a")

        self.assertEqual([record.levelname for record in records], ["DEBUG"])

    def test_changed_token_is_reported_again(self):
        self._load("fp-a")

        records = self._load("fp-b")

        self.assertEqual([record.levelname for record in records], ["INFO"])

    def test_empty_fingerprint_always_reports(self):
        self._load("")

        records = self._load("")

        self.assertEqual([record.levelname for record in records], ["INFO"])


class RuntimeLifecycleMessageTests(unittest.TestCase):
    """重复启动的提示语必须描述启动冲突，而不是恢复备份。"""

    def test_second_instance_reports_a_start_conflict(self):
        from app.modules.backup import BackupError, runtime_lifecycle_guard
        from app.runtime_paths import get_runtime_paths

        with TemporaryDirectory() as root:
            base = get_runtime_paths()
            paths = type(base)(**{
                **{
                    field: getattr(base, field)
                    for field in base.__dataclass_fields__
                },
                "data_dir": Path(root),
            })
            # 守卫在同一进程内可重入，这里模拟另一个进程已持有租约。
            with patch(
                "app.modules.process_lock.CrossProcessLock.acquire",
                return_value=False,
            ):
                with self.assertRaises(BackupError) as raised:
                    with runtime_lifecycle_guard(paths):
                        pass

        message = str(raised.exception)
        self.assertIn("已拒绝重复启动", message)
        self.assertNotIn("恢复备份", message)

    def test_restore_guard_keeps_its_backup_wording(self):
        from app.modules.backup import BackupError, _exclusive_runtime_lifecycle_guard
        from app.runtime_paths import get_runtime_paths

        with TemporaryDirectory() as root:
            base = get_runtime_paths()
            paths = type(base)(**{
                **{
                    field: getattr(base, field)
                    for field in base.__dataclass_fields__
                },
                "data_dir": Path(root),
            })
            with patch(
                "app.modules.process_lock.CrossProcessLock.acquire",
                return_value=False,
            ):
                with self.assertRaises(BackupError) as raised:
                    with _exclusive_runtime_lifecycle_guard(paths):
                        pass

        self.assertIn("恢复备份", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
