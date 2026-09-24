"""Web/Agent 本地整理重试共用状态机，保留中断核验与版本边界。"""
from __future__ import annotations

from unittest.mock import patch

from app import database as db
from app.agent import local_media_task_actions as actions
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
            owner="admin", tmdb_id="42", media_type="tv",
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
        return db.get_local_media_task(task_id, owner="admin")

    def test_cas_retry_clears_previous_confirmation_and_keeps_selection(self):
        task = self._task()
        self.assertTrue(db.reset_local_media_task_if_current(
            task.id, owner="admin", expected_version=task.version,
            expected_status=task.status,
        ))
        retried = db.get_local_media_task(task.id, owner="admin")
        self.assertEqual(retried.confirmation_actor, "")
        self.assertEqual(retried.status, "waiting_stable")
        self.assertEqual(retried.version, task.version + 1)
        self.assertNotEqual(retried.operation_token, task.operation_token)
        self.assertEqual((retried.tmdb_id, retried.season_override, retried.episode_override), ("42", 2, 3))
        self.assertEqual(db.list_local_media_task_items(task.id, owner="admin"), [])

    def test_cas_retry_does_not_discard_interrupted_write_without_confirmation(self):
        task = self._task(interrupted=True)
        self.assertTrue(db.is_interrupted_local_media_write_error(task.error))
        self.assertFalse(db.reset_local_media_task_if_current(
            task.id, owner="admin", expected_version=task.version,
            expected_status=task.status,
        ))
        self.assertEqual(db.get_local_media_task(task.id, owner="admin"), task)
        self.assertEqual(len(db.list_local_media_task_items(task.id, owner="admin")), 1)

    def test_cas_retry_explicit_interruption_confirmation_is_replay_safe(self):
        task = self._task(interrupted=True)
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
                    task.id, owner="admin", expected_version=version,
                    expected_status=status,
                ))
                self.assertEqual(db.get_local_media_task(task.id, owner="admin"), task)
                self.assertEqual(len(db.list_local_media_task_items(task.id, owner="admin")), 1)

    def test_agent_preflight_explains_interrupted_write_before_confirming(self):
        task = self._task(interrupted=True)
        with patch.object(actions, "_require_owner", return_value="owner-a"), patch.object(
            actions, "_current_task", return_value=(None, task)
        ):
            result, fingerprint = actions.prepare_retry_local_media_task({"task_number": 1}, None)
        self.assertTrue(fingerprint)
        self.assertTrue(result.data["interrupted_write"])
        self.assertIn("核验", " ".join(result.data["effects"]))
        self.assertIn("qB", " ".join(result.data["effects"]))

    def test_agent_confirmed_interrupted_retry_uses_common_reset(self):
        task = self._task(interrupted=True)
        with patch.object(actions, "_require_owner", return_value="owner-a"), patch.object(
            actions, "_current_task", return_value=(None, task)
        ), patch.object(actions, "get_local_media_scheduler") as scheduler:
            _, fingerprint = actions.prepare_retry_local_media_task({"task_number": 1}, None)
            result = actions.retry_local_media_task_confirmed({"task_number": 1}, fingerprint, None)
        self.assertTrue(result.ok)
        self.assertEqual(
            result.effect_metadata["completion"],
            {
                "kind": "local_media_task",
                "task_id": task.id,
                "task_number": 1,
                "operation": "retry",
            },
        )
        self.assertEqual(db.get_local_media_task(task.id, owner="admin").confirmation_actor, "")
        scheduler.return_value.reload.assert_called_once()
