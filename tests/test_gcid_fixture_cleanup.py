"""GCID fixture ownership survives failed unittest setup and foreign patches."""
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import database as db
from app.modules.backup import runtime_lifecycle_guard
from app.modules import gcid_import
from app.modules import telegram_notification_center
from tests import test_gcid_import_api as api
from tests import test_gcid_retry_durability_audit as durability


def test_failed_api_setup_restores_owned_resources():
    class FailingSetup(api.GCIDImportAPITests):
        def runTest(self):
            raise AssertionError("failed setup must not reach the test body")

    case = FailingSetup()
    previous_db = db.DB_PATH
    previous_env = dict(os.environ)
    previous_notify = telegram_notification_center.send_event_result
    previous_cloud_client = gcid_import.GuangYaClient
    previous_importer = gcid_import.GuangyaGCIDImporter
    observed = []

    def fail_startup(*args, **kwargs):
        # 只在客户端已归属清理栈后的 lifespan 注入故障，不依赖安装/工厂的加锁次数。
        if not hasattr(case, "client_context"):
            return runtime_lifecycle_guard(*args, **kwargs)
        expected = Path(case.temp.name) / "api.db"
        assert db.resolve_db_path() == expected.resolve()
        with db.get_conn() as conn:
            actual = conn.execute("PRAGMA database_list").fetchone()[2]
        assert Path(actual).resolve() == expected.resolve()
        observed.append(expected)
        raise RuntimeError("forced GCID startup boundary failure")

    try:
        with patch("app.modules.backup.runtime_lifecycle_guard", side_effect=fail_startup), patch.object(
            gcid_import, "reset_runtime_state", wraps=gcid_import.reset_runtime_state
        ) as reset:
            result = unittest.TestResult()
            unittest.TestSuite([case]).run(result)
            assert result.testsRun == 1
            assert len(result.errors) == 1
            assert "forced GCID startup boundary failure" in result.errors[0][1]
            assert not result.failures
            assert len(observed) == 1
            assert db.DB_PATH == previous_db
            assert dict(os.environ) == previous_env
            assert not observed[0].parent.exists()
            assert telegram_notification_center.send_event_result is previous_notify
            assert gcid_import.GuangYaClient is previous_cloud_client
            assert gcid_import.GuangyaGCIDImporter is previous_importer
            assert case.client_context.is_closed
            assert case.client_context.portal is None
            assert reset.call_count == 2
    finally:
        # Keep the intentionally failing old-fixture RED isolated as well.
        case.doCleanups()
        for name in ("env_patch", "db_patch"):
            owner = getattr(case, name, None)
            if owner is not None:
                owner.stop()
        if hasattr(case, "temp"):
            case.temp.cleanup()
        gcid_import.reset_runtime_state()


def test_durability_cleanup_preserves_foreign_patch():
    class OwnedSetup(durability.GCIDRetryDurabilityAuditTests):
        def runTest(self):
            assert db.resolve_db_path() == self.test_db_path.resolve()
            assert db.get_gcid_import_task(self._task()) is not None

    holder = SimpleNamespace(value="original")
    foreign = patch.object(holder, "value", "foreign-owned")
    open_importer = gcid_import._open_importer
    started = gcid_import.notifier.notify_gcid_import_started
    finished = gcid_import.notifier.notify_gcid_import_finished
    foreign.start()
    try:
        result = unittest.TestResult()
        unittest.TestSuite([OwnedSetup()]).run(result)
        assert result.wasSuccessful(), result.errors + result.failures
        assert result.testsRun == 1
        assert holder.value == "foreign-owned"
        assert gcid_import._open_importer is open_importer
        assert gcid_import.notifier.notify_gcid_import_started is started
        assert gcid_import.notifier.notify_gcid_import_finished is finished
    finally:
        foreign.stop()
    assert holder.value == "original"
