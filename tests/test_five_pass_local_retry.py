"""Web/Agent 本地整理重试共用状态机，保留中断核验与版本边界。"""
from __future__ import annotations

from typing import cast

from unittest.mock import patch

from app import database as db
from app.modules import local_media_tasks as tasks
from tests.support import IsolatedDatabaseTestCase, isolated_test_database


class UnifiedLocalRetryTests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.source_id = db.create_local_media_source(
            name=f"five-pass-{self._testMethodName}", qb_profile="", qb_path_prefix="",
            local_root="/synthetic/five-pass", owner="admin",
        )

    def _task(self, *, interrupted=False):
        task_id = db.prepare_manual_local_media_task(
            self.source_id, "/synthetic/five-pass/Show.S02E03.mkv",
            snapshot_digest="preview:fixture", owner="admin", tmdb_id="42", media_type="tv",
            season_override=2, episode_override=3,
        )
        db.add_local_media_task_item(
            task_id, "/synthetic/five-pass/Show.S02E03.mkv",
            "/synthetic/library/Show.S02E03.mkv", role="video", owner="admin",
        )
        db.update_local_media_task(
            task_id, owner="admin", status="moving" if interrupted else "failed",
            confirmation_actor="human", title="old", year="2025",
            error="old failure", warning="old warning", snapshot_digest="old-digest",
        )
        if interrupted:
            db.init_db()
        task = db.get_local_media_task(task_id, owner="admin")
        assert task is not None
        return task

    def test_cas_retry_clears_previous_confirmation_and_keeps_selection(self):
        task = self._task()
        assert task is not None
        self.assertTrue(db.reset_local_media_task_if_current(
            task.id, owner="admin", expected_version=task.version,
            expected_status=task.status,
        ))
        retried = db.get_local_media_task(task.id, owner="admin")
        assert retried is not None
        self.assertEqual(retried.confirmation_actor, "")
        self.assertEqual(retried.status, "waiting_stable")
        self.assertEqual(retried.version, task.version + 1)
        self.assertNotEqual(retried.operation_token, task.operation_token)
        self.assertEqual((retried.tmdb_id, retried.season_override, retried.episode_override), ("42", 2, 3))
        self.assertEqual(db.list_local_media_task_items(task.id, owner="admin"), [])

    def test_cas_retry_does_not_discard_interrupted_write_without_confirmation(self):
        task = self._task(interrupted=True)
        assert task is not None
        self.assertTrue(db.is_interrupted_local_media_write_error(task.error))
        self.assertFalse(db.reset_local_media_task_if_current(
            task.id, owner="admin", expected_version=task.version,
            expected_status=task.status,
        ))
        self.assertEqual(db.get_local_media_task(task.id, owner="admin"), task)
        self.assertEqual(len(db.list_local_media_task_items(task.id, owner="admin")), 1)

    def test_cas_retry_explicit_interruption_confirmation_is_replay_safe(self):
        task = self._task(interrupted=True)
        assert task is not None
        args = dict(owner="admin", expected_version=task.version,
                    expected_status=task.status, confirm_interrupted_write=True)
        self.assertTrue(db.reset_local_media_task_if_current(task.id, **args))
        first = db.get_local_media_task(task.id, owner="admin")
        self.assertFalse(db.reset_local_media_task_if_current(task.id, **args))
        self.assertEqual(db.get_local_media_task(task.id, owner="admin"), first)

    def test_cas_retry_stale_version_retains_old_items_and_owner(self):
        task = self._task()
        for owner, version in (("other", task.version), ("admin", task.version - 1)):
            with self.subTest(owner=owner, version=version):
                assert task is not None
                self.assertFalse(db.reset_local_media_task_if_current(
                    task.id, owner=owner, expected_version=version,
                    expected_status=task.status,
                ))
                self.assertEqual(db.get_local_media_task(task.id, owner="admin"), task)
                self.assertEqual(len(db.list_local_media_task_items(task.id, owner="admin")), 1)

    def test_cas_retry_cannot_silently_disable_version_or_status_guards(self):
        task = self._task()
        for version, status in ((None, task.status), (task.version, None)):
            with self.subTest(version=version, status=status):
                self.assertFalse(db.reset_local_media_task_if_current(
                    task.id, owner="admin", expected_version=cast(int, version),
                    expected_status=cast(str, status),
                ))
                self.assertEqual(db.get_local_media_task(task.id, owner="admin"), task)
                self.assertEqual(len(db.list_local_media_task_items(task.id, owner="admin")), 1)

    def test_native_preflight_requires_explicit_interruption_confirmation(self):
        task = self._task(interrupted=True)
        with self.assertRaises(tasks.LocalMediaServiceError):
            tasks.prepare_local_task_retry(task.id)
        prepared = tasks.prepare_local_task_retry(task.id, confirm_interrupted_write=True)
        self.assertTrue(prepared["fingerprint"])
        self.assertTrue(prepared["preview"]["data"]["interrupted_write"])
        self.assertIn("qB", " ".join(prepared["preview"]["data"]["effects"]))

    def test_native_confirmed_interrupted_retry_uses_common_reset(self):
        task = self._task(interrupted=True)
        prepared = tasks.prepare_local_task_retry(task.id, confirm_interrupted_write=True)
        with patch("app.modules.local_media_scheduler.get_local_media_scheduler") as scheduler:
            result = tasks.retry_local_task(task.id, expected_fingerprint=prepared["fingerprint"], confirm_interrupted_write=True)
        self.assertTrue(result["ok"])
        current = db.get_local_media_task(task.id)
        assert current is not None
        self.assertEqual(current.confirmation_actor, "")
        self.assertEqual(result["data"]["task_id"], task.id)
        scheduler.return_value.reload.assert_called_once()
