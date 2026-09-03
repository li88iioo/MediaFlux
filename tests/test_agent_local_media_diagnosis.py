"""本地媒体 Agent 诊断的聚合、脱敏、路由与 API 契约。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.local_media_actions import (
    diagnose_local_media,
    local_media_diagnosis_arguments,
)
from tests.support import IsolatedDatabaseTestCase


class LocalMediaDiagnosisUnitTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_library_targets")
            conn.execute("DELETE FROM local_media_sources")

    @staticmethod
    def _source(
        *,
        owner: str = "admin",
        enabled: int = 1,
        mode: str = "move",
        suffix: str = "one",
    ) -> int:
        return db.create_local_media_source(
            name=f"SECRET_SOURCE_{suffix}",
            qb_profile="configured:qb",
            qb_path_prefix=f"/remote/SECRET_{suffix}",
            local_root=f"/private/SECRET_{suffix}",
            enabled=enabled,
            owner=owner,
            mode=mode,
        )

    @staticmethod
    def _task(
        source_id: int,
        status: str,
        *,
        trigger: str = "manual",
        owner: str = "admin",
        suffix: str = "one",
    ) -> int:
        task_id = db.create_local_media_task(
            source_id,
            f"HASH_SECRET_{suffix}",
            f"/private/TITLE_SECRET_{suffix}.mkv",
            owner=owner,
            trigger=trigger,
        )
        if status != "waiting_stable":
            db.update_local_media_task(
                task_id,
                owner=owner,
                status=status,
                title=f"TITLE_SECRET_{suffix}",
                error=f"ERROR_SECRET_{suffix} /private/path",
                warning=f"WARNING_SECRET_{suffix}",
                tmdb_id="987654",
            )
        return task_id

    def test_arguments_reject_every_extra_field(self):
        self.assertEqual(local_media_diagnosis_arguments({}), {})
        with self.assertRaisesRegex(
            AgentToolError, "^local_media\\.diagnose 不接受参数$"
        ):
            local_media_diagnosis_arguments({"path": "/private"})

    def test_database_summary_is_owner_scoped_and_classifies_states(self):
        source = self._source()
        db.upsert_local_library_target(
            source, "movie", "/library/SECRET", owner="admin"
        )
        self._task(source, "waiting_stable", trigger="qb_completed", suffix="waiting")
        self._task(source, "recognizing", trigger="scan", suffix="active")
        self._task(source, "requires_manual", trigger="manual", suffix="manual")
        self._task(source, "planned", trigger="manual", suffix="planned")
        self._task(source, "failed", trigger="manual", suffix="failed")
        self._task(source, "completed", trigger="qb_completed", suffix="completed")
        other = self._source(owner="other", suffix="other")
        self._task(other, "failed", owner="other", suffix="other")
        summary = db.get_local_media_diagnostic_summary(owner="admin")
        self.assertEqual(
            summary["sources"],
            {
                "total": 1,
                "enabled": 1,
                "disabled": 0,
                "move_mode": 1,
                "preview_only_mode": 0,
                "enabled_without_targets": 0,
            },
        )
        self.assertEqual(
            summary["tasks"],
            {
                "total": 6,
                "waiting_stable": 1,
                "active": 1,
                "requires_manual": 1,
                "planned": 1,
                "failed": 1,
                "completed": 1,
                "qb_completed": 2,
                "scan": 1,
                "manual": 3,
            },
        )

    def test_empty_and_inactive_states_are_explicit(self):
        with patch(
            "app.agent.local_media_actions.peek_local_media_scheduler_status"
        ) as scheduler:
            scheduler.return_value = {"running": False, "interval_seconds": 10}
            empty = diagnose_local_media({})
            self._source(enabled=0)
            inactive = diagnose_local_media({})
        self.assertEqual(empty.status, "not_configured")
        self.assertTrue(empty.ok)
        self.assertEqual(inactive.status, "inactive")
        self.assertEqual(inactive.data["sources"]["disabled"], 1)

    def test_attention_and_active_states_preserve_planned_semantics(self):
        source = self._source()
        db.upsert_local_library_target(
            source, "default", "/library/default", owner="admin"
        )
        self._task(source, "planned", suffix="planned")
        self._task(source, "waiting_stable", suffix="waiting")
        with patch(
            "app.agent.local_media_actions.peek_local_media_scheduler_status"
        ) as scheduler:
            scheduler.return_value = {"running": True, "interval_seconds": 5}
            active = diagnose_local_media({})
        self.assertEqual(active.status, "active")
        self.assertEqual(active.data["attention"]["total"], 0)
        self.assertNotIn("scan_enabled", active.data["sources"])
        self.assertEqual(active.data["tasks"]["planned"], 1)
        self._task(source, "requires_manual", suffix="manual")
        self._task(source, "failed", suffix="failed")
        source_without_target = self._source(suffix="unmapped")
        self.assertIsInstance(source_without_target, int)
        with patch(
            "app.agent.local_media_actions.peek_local_media_scheduler_status"
        ) as scheduler:
            scheduler.return_value = {"running": True, "interval_seconds": 5}
            attention = diagnose_local_media({})
        self.assertEqual(attention.status, "attention")
        self.assertEqual(
            attention.data["attention"],
            {
                "total": 3,
                "categories": {
                    "requires_manual": 1,
                    "failed": 1,
                    "enabled_sources_without_targets": 1,
                    "scheduler_not_running": 0,
                },
            },
        )

    def test_scheduler_stopped_and_inactive_failures_keep_attention_consistent(self):
        source = self._source()
        db.upsert_local_library_target(
            source, "default", "/library/default", owner="admin"
        )
        with patch(
            "app.agent.local_media_actions.peek_local_media_scheduler_status",
            return_value={"running": False, "interval_seconds": 0},
        ):
            stopped = diagnose_local_media({})
        self.assertEqual(stopped.status, "attention")
        self.assertEqual(
            stopped.data["attention"]["categories"]["scheduler_not_running"], 1
        )
        self.assertIn("调度器当前未运行", stopped.suggestions[-1])
        with db.get_conn() as conn:
            conn.execute("UPDATE local_media_sources SET enabled=0 WHERE owner='admin'")
        self._task(source, "failed", suffix="inactive-failed")
        with patch(
            "app.agent.local_media_actions.peek_local_media_scheduler_status",
            return_value={"running": False, "interval_seconds": 0},
        ):
            inactive_failed = diagnose_local_media({})
        self.assertEqual(inactive_failed.status, "attention")
        self.assertEqual(inactive_failed.data["attention"]["categories"]["failed"], 1)

    def test_diagnosis_does_not_initialize_scheduler_or_read_env_file(self):
        with (
            patch("app.modules.local_media_scheduler._scheduler", None),
            patch("app.config._read_env_file") as read_env,
        ):
            result = diagnose_local_media({})
            from app.modules import local_media_scheduler

            self.assertIsNone(local_media_scheduler._scheduler)
        read_env.assert_not_called()
        self.assertEqual(
            result.data["scheduler"], {"running": False, "interval_seconds": 0.0}
        )

    def test_sensitive_database_fields_never_leave_projection(self):
        source = self._source(suffix="leak")
        self._task(source, "failed", suffix="leak")
        with patch(
            "app.agent.local_media_actions.peek_local_media_scheduler_status"
        ) as scheduler:
            scheduler.return_value = {"running": False, "interval_seconds": 10}
            result = diagnose_local_media({})
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "SECRET_SOURCE_leak",
            "HASH_SECRET_leak",
            "TITLE_SECRET_leak",
            "ERROR_SECRET_leak",
            "WARNING_SECRET_leak",
            "/private",
            "987654",
        ):
            self.assertNotIn(secret, serialized)
        for forbidden_key in (
            '"source_id"',
            '"task_id"',
            '"content_path"',
            '"qb_hash"',
            '"title"',
            '"tmdb_id"',
            '"warning"',
        ):
            self.assertNotIn(forbidden_key, serialized)
        self.assertEqual(result.error, "")
        self.assertFalse(result.data["network_accessed"])

    def test_exception_returns_fixed_sanitized_error(self):
        with patch(
            "app.agent.local_media_actions.db.get_local_media_diagnostic_summary",
            side_effect=RuntimeError("LOCAL_SECRET /private/path"),
        ):
            result = diagnose_local_media({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("LOCAL_SECRET", serialized)
        self.assertNotIn("/private/path", serialized)
        self.assertFalse(result.data["network_accessed"])
