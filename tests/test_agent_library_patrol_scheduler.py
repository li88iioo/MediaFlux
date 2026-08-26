"""Agent 持久化全库缺集巡检的状态机与安全投影测试。"""
from __future__ import annotations

from datetime import datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import database as db
from app.agent.library_patrol_status import get_library_patrol_status
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator, is_library_patrol_status_message
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.tools import build_tool_registry
from app.routes.api import save_config
from app.modules.agent_library_patrol_scheduler import AgentLibraryPatrolScheduler
from app.notifier import TelegramSendResult
from tests.support import IsolatedDatabaseTestCase


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _patrol_result(*, status: str = "updates_available", ok: bool = True) -> ToolResult:
    return ToolResult(ok, status, "巡检完成", data={
        "as_of": "2026-08-03",
        "checked_series_count": 8,
        "updates_available_count": 1,
        "missing_episode_count": 2,
        "inconclusive_count": 0,
        "unmapped_series_count": 0,
        "findings_truncated": False,
        "findings": [{
            "title": "The Show",
            "tmdb_id": "12345",
            "status": "updates_available",
            "missing_count": 2,
            "missing_sample": [
                {"season": 2, "episode": 3},
                {"season": 2, "episode": 4},
            ],
            "missing_sample_truncated": False,
            "server_url": "https://private.invalid?token=SECRET",
            "path": "/volume/private",
        }],
        "sources": [{"url": "https://private.invalid", "token": "SECRET"}],
    })


class AgentLibraryPatrolContractTests(unittest.TestCase):
    def test_status_tool_is_read_only_and_rejects_arguments(self):
        registry = build_tool_registry()
        capability = {item["name"]: item for item in registry.capabilities()}[
            "library.patrol_status"
        ]
        self.assertEqual(capability["risk"], "read")
        self.assertFalse(capability["requires_confirmation"])
        self.assertEqual(capability["parameters"], {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        })
        with self.assertRaises(AgentToolError):
            registry.execute("library.patrol_status", {"token": "secret"})

    def test_status_intent_does_not_shadow_live_patrol(self):
        registry = ToolRegistry()
        for name in ("library.patrol_status", "library.audit_library_episodes"):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: ToolResult(
                    True, "not_run", tool, data=arguments
                ),
                validator=lambda arguments: dict(arguments),
            ))
        agent = AgentOrchestrator(registry)

        status = agent.query("上次自动缺集巡检结果")
        live = agent.query("巡检整个媒体库有没有缺集")

        self.assertEqual(status["tool_call"]["name"], "library.patrol_status")
        self.assertEqual(live["tool_call"]["name"], "library.audit_library_episodes")
        self.assertTrue(is_library_patrol_status_message("后台全库巡检状态怎么样"))
        self.assertFalse(is_library_patrol_status_message("巡检整个媒体库有没有缺集"))
        for message in (
            "开启自动缺集巡检",
            "自动缺集巡检怎么配置",
            "关闭定时巡检",
            "设置全库巡检间隔",
        ):
            with self.subTest(message=message):
                self.assertFalse(is_library_patrol_status_message(message))


class AgentLibraryPatrolConfigTests(unittest.TestCase):
    @staticmethod
    def _request():
        return SimpleNamespace(
            session={"logged_in": True},
            app=SimpleNamespace(
                state=SimpleNamespace(
                    background_services_enabled=False,
                    media_proxy_manager=None,
                )
            ),
        )

    def test_config_accepts_bounded_values_and_reloads_scheduler(self):
        scheduler = Mock()
        payload = {
            "AGENT_LIBRARY_PATROL_ENABLED": "1",
            "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED": "1",
            "AGENT_LIBRARY_PATROL_INTERVAL_HOURS": "12",
            "AGENT_LIBRARY_PATROL_MAX_SERIES": "80",
        }
        with patch("app.routes.api.config.get", return_value=""), patch(
            "app.routes.api.config.set_and_save"
        ) as persist, patch("app.services.clear_dashboard_cache"), patch(
            "app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler",
            return_value=scheduler,
        ):
            response = save_config(self._request(), payload)

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with(payload)
        scheduler.reload.assert_called_once_with(immediate=False)

    def test_config_rejects_invalid_boolean_without_persisting(self):
        for key in (
            "AGENT_LIBRARY_PATROL_ENABLED",
            "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED",
        ):
            with self.subTest(key=key), patch(
                "app.routes.api.config.get", return_value=""
            ), patch("app.routes.api.config.set_and_save") as persist:
                response = save_config(self._request(), {key: "enabled"})
            self.assertEqual(response.status_code, 400)
            persist.assert_not_called()

    def test_config_rejects_out_of_range_values_without_persisting(self):
        for payload in (
            {"AGENT_LIBRARY_PATROL_INTERVAL_HOURS": "0"},
            {"AGENT_LIBRARY_PATROL_INTERVAL_HOURS": "169"},
            {"AGENT_LIBRARY_PATROL_MAX_SERIES": "0"},
            {"AGENT_LIBRARY_PATROL_MAX_SERIES": "101"},
        ):
            with self.subTest(payload=payload), patch(
                "app.routes.api.config.get", return_value=""
            ), patch("app.routes.api.config.set_and_save") as persist:
                response = save_config(self._request(), payload)
            self.assertEqual(response.status_code, 400)
            persist.assert_not_called()


class AgentLibraryPatrolDatabaseTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_maintenance")
            conn.execute("DELETE FROM agent_library_patrol_notification_outbox")
            conn.execute("DELETE FROM agent_library_patrol")

    def test_singleton_claim_and_generation_cas(self):
        first = db.ensure_agent_library_patrol(next_run_at="2026-08-03 12:00:00")
        second = db.ensure_agent_library_patrol(next_run_at="2026-08-04 12:00:00")
        self.assertEqual(first["patrol_key"], "default")
        self.assertEqual(second["next_run_at"], "2026-08-03 12:00:00")

        claimed = db.claim_due_agent_library_patrol(current_time="2026-08-03 12:00:00")
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["lease_generation"], 1)
        self.assertIsNone(
            db.claim_due_agent_library_patrol(current_time="2026-08-03 12:00:00")
        )
        self.assertTrue(db.update_agent_library_patrol(
            status="pending",
            outcome="up_to_date",
            attempts=0,
            next_run_at="2026-08-04 12:00:00",
            expected_lease_generation=1,
            last_finished_at="2026-08-03 12:00:01",
        ))
        self.assertFalse(db.update_agent_library_patrol(
            status="pending",
            outcome="up_to_date",
            attempts=0,
            next_run_at="2026-08-04 12:00:00",
            expected_lease_generation=1,
            last_finished_at="2026-08-03 12:00:01",
        ))

    def test_stale_lease_is_reclaimed_and_old_worker_cannot_finish(self):
        db.ensure_agent_library_patrol(next_run_at="2026-08-03 12:00:00")
        old_job = db.claim_due_agent_library_patrol(current_time="2026-08-03 12:00:00")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_library_patrol SET updated_at=? WHERE patrol_key='default'",
                ("2026-08-03 11:00:00",),
            )
        new_job = db.claim_due_agent_library_patrol(
            current_time="2026-08-03 12:31:00",
            stale_before="2026-08-03 12:01:00",
        )
        self.assertEqual(old_job["lease_generation"], 1)
        self.assertEqual(new_job["lease_generation"], 2)
        self.assertFalse(db.update_agent_library_patrol(
            status="pending",
            outcome="up_to_date",
            attempts=0,
            next_run_at="2026-08-04 12:31:00",
            expected_lease_generation=old_job["lease_generation"],
        ))
        self.assertTrue(db.update_agent_library_patrol(
            status="pending",
            outcome="up_to_date",
            attempts=0,
            next_run_at="2026-08-04 12:31:00",
            expected_lease_generation=new_job["lease_generation"],
        ))

    def test_reschedule_moves_non_running_task_but_not_active_lease(self):
        db.ensure_agent_library_patrol(next_run_at="2026-08-04 12:00:00")
        self.assertTrue(db.reschedule_agent_library_patrol(
            next_run_at="2026-08-03 12:00:00"
        ))
        self.assertEqual(
            db.get_agent_library_patrol()["next_run_at"],
            "2026-08-03 12:00:00",
        )
        running = db.claim_due_agent_library_patrol(
            current_time="2026-08-03 12:00:00"
        )
        self.assertIsNotNone(running)
        self.assertFalse(db.reschedule_agent_library_patrol(
            next_run_at="2026-08-03 11:00:00"
        ))
        self.assertEqual(db.get_agent_library_patrol()["status"], "running")

    def test_cancel_running_lease_invalidates_old_worker(self):
        db.ensure_agent_library_patrol(next_run_at="2026-08-03 12:00:00")
        job = db.claim_due_agent_library_patrol(current_time="2026-08-03 12:00:00")

        self.assertTrue(db.cancel_agent_library_patrol_lease(
            next_run_at="2026-08-03 12:00:01",
            expected_lease_generation=job["lease_generation"],
        ))
        row = db.get_agent_library_patrol()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["lease_generation"], job["lease_generation"] + 1)
        self.assertFalse(db.update_agent_library_patrol(
            status="pending",
            outcome="up_to_date",
            attempts=0,
            next_run_at="2026-08-04 12:00:00",
            expected_lease_generation=job["lease_generation"],
        ))

    def test_notification_outbox_claim_retry_and_sent_use_generation_cas(self):
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO agent_library_patrol_notification_outbox("
                "result_revision,fingerprint,outcome,payload_json,next_attempt_at,"
                "created_at,updated_at) VALUES(1,?,'updates_available','{}',?,?,?)",
                ("a" * 64, "2026-08-03 12:00:00",
                 "2026-08-03 12:00:00", "2026-08-03 12:00:00"),
            )
        first = db.claim_due_agent_library_patrol_notification(
            current_time="2026-08-03 12:00:00"
        )
        self.assertEqual(first["status"], "sending")
        self.assertEqual(first["lease_generation"], 1)
        self.assertIsNone(db.claim_due_agent_library_patrol_notification(
            current_time="2026-08-03 12:00:00"
        ))
        self.assertTrue(db.retry_agent_library_patrol_notification(
            first["id"],
            expected_lease_generation=first["lease_generation"],
            next_attempt_at="2026-08-03 12:01:00",
            error_type="RuntimeError: token=SECRET",
        ))
        second = db.claim_due_agent_library_patrol_notification(
            current_time="2026-08-03 12:01:00"
        )
        self.assertEqual(second["lease_generation"], 2)
        self.assertFalse(db.complete_agent_library_patrol_notification(
            first["id"], expected_lease_generation=first["lease_generation"]
        ))
        self.assertTrue(db.complete_agent_library_patrol_notification(
            second["id"], expected_lease_generation=second["lease_generation"],
            sent_at="2026-08-03 12:01:01",
        ))
        row = db.list_agent_library_patrol_notifications()[0]
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["attempts"], 1)
        self.assertNotIn("SECRET", row["last_error_type"] or "")

    def test_retention_purges_only_expired_terminal_notifications(self):
        db.ensure_agent_library_patrol(next_run_at="2026-08-04 12:00:00")
        rows = (
            (1, "sent", "2026-07-20 12:00:00", "2026-07-20 12:00:00"),
            (2, "sent", None, "2026-07-20 12:00:00"),
            (3, "discarded", None, "2026-07-20 12:00:00"),
            (4, "sent", "2026-07-27 12:00:00", "2026-07-27 12:00:00"),
            (5, "discarded", None, "2026-08-02 12:00:00"),
            (9, "discarded", None, "2026-07-27 12:00:00"),
            (6, "pending", None, "2026-07-01 12:00:00"),
            (7, "sending", None, "2026-07-01 12:00:00"),
            (8, "retry_wait", None, "2026-07-01 12:00:00"),
        )
        with db.get_conn() as conn:
            for revision, status, sent_at, updated_at in rows:
                conn.execute(
                    "INSERT INTO agent_library_patrol_notification_outbox("
                    "result_revision,fingerprint,outcome,payload_json,status,"
                    "next_attempt_at,sent_at,created_at,updated_at) "
                    "VALUES(?,?,?,'{}',?,?,?,?,?)",
                    (
                        revision,
                        f"{revision:064d}",
                        "updates_available",
                        status,
                        "2026-09-01 12:00:00" if status != "sending"
                        else "2026-07-01 12:00:00",
                        sent_at,
                        updated_at,
                        updated_at,
                    ),
                )

        deleted = db.purge_expired_agent_task_history(
            current_time="2026-08-03 12:00:00",
            next_cleanup_at="2026-08-04 12:00:00",
            terminal_before="2026-07-27 12:00:00",
            limit_per_table=50,
        )

        self.assertTrue(deleted["performed"])
        self.assertEqual(deleted["download_verifications"], 0)
        self.assertEqual(deleted["patrol_notification_outbox"], 3)
        remaining = {
            int(row["result_revision"]): str(row["status"])
            for row in db.list_agent_library_patrol_notifications()
        }
        self.assertEqual(remaining, {
            4: "sent",
            5: "discarded",
            9: "discarded",
            6: "pending",
            7: "sending",
            8: "retry_wait",
        })
        self.assertIsNotNone(db.get_agent_library_patrol())

        recovered = db.claim_due_agent_library_patrol_notification(
            current_time="2026-08-03 12:00:00",
            stale_before="2026-08-03 11:58:00",
        )
        self.assertIsNone(recovered)
        rows_by_revision = {
            int(row["result_revision"]): row
            for row in db.list_agent_library_patrol_notifications()
        }
        self.assertEqual(rows_by_revision[7]["status"], "discarded")
        self.assertEqual(
            rows_by_revision[7]["last_error_type"], "DeliveryOutcomeUnknown"
        )
        self.assertEqual(rows_by_revision[7]["payload_json"], "")

    def test_init_recovers_interrupted_notification_sender(self):
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO agent_library_patrol_notification_outbox("
                "result_revision,fingerprint,outcome,payload_json,status,"
                "lease_generation,next_attempt_at,created_at,updated_at) "
                "VALUES(1,?,'updates_available','{}','sending',1,?,?,?)",
                ("b" * 64, "2026-08-03 12:00:00",
                 "2026-08-03 12:00:00", "2026-08-03 12:00:00"),
            )

        db.init_db()

        row = db.list_agent_library_patrol_notifications()[0]
        self.assertEqual(row["status"], "discarded")
        self.assertEqual(row["lease_generation"], 2)
        self.assertEqual(row["last_error_type"], "DeliveryOutcomeUnknown")

    def test_init_recovers_interrupted_running_patrol(self):
        db.ensure_agent_library_patrol(next_run_at="2026-08-03 12:00:00")
        claimed = db.claim_due_agent_library_patrol(current_time="2026-08-03 12:00:00")
        self.assertEqual(claimed["lease_generation"], 1)

        db.init_db()

        recovered = db.get_agent_library_patrol()
        self.assertEqual(recovered["status"], "retry_wait")
        self.assertEqual(recovered["lease_generation"], 2)


    def test_continuation_progress_survives_database_recovery(self):
        db.ensure_agent_library_patrol(next_run_at="2026-08-03 12:00:00")
        claimed = db.claim_due_agent_library_patrol(
            current_time="2026-08-03 12:00:00"
        )
        accumulator = {
            "as_of": "2026-08-03",
            "patrol_status": "inconclusive",
            "findings_truncated": False,
            "checked_series_count": 2,
            "updates_available_count": 0,
            "missing_episode_count": 0,
            "inconclusive_count": 0,
            "unmapped_series_count": 0,
            "options": [],
        }
        self.assertTrue(db.continue_agent_library_patrol(
            expected_lease_generation=claimed["lease_generation"],
            next_run_at="2026-08-03 12:00:01",
            cycle_as_of="2026-08-03",
            cycle_cursor_tmdb_id="12345",
            cycle_accumulator_json=json.dumps(accumulator),
            cycle_started_at="2026-08-03 12:00:00",
        ))

        db.init_db()

        row = db.get_agent_library_patrol()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["cycle_cursor_tmdb_id"], "12345")
        self.assertEqual(
            json.loads(row["cycle_accumulator_json"])["checked_series_count"], 2
        )

    def test_continuation_rejects_rollback_or_unmarked_stall(self):
        db.ensure_agent_library_patrol(next_run_at="2026-08-03 12:00:00")
        claimed = db.claim_due_agent_library_patrol(
            current_time="2026-08-03 12:00:00"
        )
        accumulator = {
            "as_of": "2026-08-03",
            "patrol_status": "inconclusive",
            "findings_truncated": False,
            "checked_series_count": 1,
            "updates_available_count": 0,
            "missing_episode_count": 0,
            "inconclusive_count": 0,
            "unmapped_series_count": 0,
            "options": [],
        }
        self.assertTrue(db.continue_agent_library_patrol(
            expected_lease_generation=claimed["lease_generation"],
            next_run_at="2026-08-03 12:00:01",
            cycle_as_of="2026-08-03",
            cycle_cursor_tmdb_id="12345",
            cycle_accumulator_json=json.dumps(accumulator),
        ))
        claimed = db.claim_due_agent_library_patrol(
            current_time="2026-08-03 12:00:01"
        )
        self.assertFalse(db.continue_agent_library_patrol(
            expected_lease_generation=claimed["lease_generation"],
            next_run_at="2026-08-03 12:00:02",
            cycle_as_of="2026-08-03",
            cycle_cursor_tmdb_id="12345",
            cycle_accumulator_json=json.dumps(accumulator),
        ))


class AgentLibraryPatrolSchedulerTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_library_patrol_notification_outbox")
            conn.execute("DELETE FROM agent_library_patrol")
        self.clock = MutableClock(datetime(2026, 8, 3, 12, 0, 0))

    def _config(
        self, *, enabled: bool = True, notify: bool = False,
        hours: int = 24, max_series: int = 50,
    ):
        def get_bool(key: str, default: bool = False) -> bool:
            if key == "AGENT_LIBRARY_PATROL_ENABLED":
                return enabled
            if key == "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED":
                return notify
            return default

        def get_int(key: str, default: int = 0) -> int:
            values = {
                "AGENT_LIBRARY_PATROL_INTERVAL_HOURS": hours,
                "AGENT_LIBRARY_PATROL_MAX_SERIES": max_series,
            }
            return values.get(key, default)

        return patch.multiple(
            "app.modules.agent_library_patrol_scheduler.config",
            get_bool=get_bool,
            get_int=get_int,
        )

    def test_disabled_scheduler_does_not_create_or_execute_patrol(self):
        executor = Mock()
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=executor,
            clock=self.clock,
        )
        with self._config(enabled=False):
            self.assertEqual(scheduler.run_once(), 0)
        executor.assert_not_called()
        self.assertIsNone(db.get_agent_library_patrol())

    def test_reload_reschedules_future_task_immediately_when_enabled(self):
        executor = Mock(return_value=(_patrol_result(status="up_to_date"), 10))
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=executor,
            clock=self.clock,
        )
        db.ensure_agent_library_patrol(next_run_at="2026-08-10 12:00:00")

        with self._config(enabled=True):
            scheduler.reload()
            self.assertEqual(
                db.get_agent_library_patrol()["next_run_at"],
                "2026-08-03 12:00:00",
            )
            self.assertEqual(scheduler.run_once(), 1)

        executor.assert_called_once()

    def test_success_persists_only_safe_projection_and_schedules_next_cycle(self):
        executor = Mock(return_value=(_patrol_result(), 10))
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=executor,
            clock=self.clock,
        )
        with self._config():
            self.assertEqual(scheduler.run_once(), 1)

        executor.assert_called_once_with({"as_of": "2026-08-03", "max_series": 50})
        row = db.get_agent_library_patrol()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["outcome"], "updates_available")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["next_run_at"], "2026-08-04 12:00:00")
        self.assertEqual(row["missing_episode_count"], 2)
        projection = json.loads(row["projection_json"])
        self.assertEqual(projection["options"][0]["episode_sample"], [3, 4])
        serialized = repr(dict(row))
        for secret in ("private.invalid", "token", "/volume/", "server_url", "sources"):
            self.assertNotIn(secret, serialized)

    def test_continuation_persists_cursor_and_accumulates_final_projection(self):
        first = _patrol_result(status="inconclusive", ok=True)
        first.data.update({
            "checked_series_count": 2,
            "updates_available_count": 1,
            "missing_episode_count": 2,
            "continuation_pending": True,
            "last_processed_tmdb_id": "12345",
        })
        second = _patrol_result(status="up_to_date", ok=True)
        second.data.update({
            "checked_series_count": 3,
            "updates_available_count": 0,
            "missing_episode_count": 0,
            "findings": [],
            "continuation_pending": False,
            "last_processed_tmdb_id": "67890",
        })
        executor = Mock(side_effect=[(first, 30_000), (second, 10_000)])
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=executor,
            clock=self.clock,
        )

        with self._config():
            self.assertEqual(scheduler.run_once(), 1)
            interim = db.get_agent_library_patrol()
            self.assertEqual(interim["status"], "pending")
            self.assertEqual(interim["cycle_cursor_tmdb_id"], "12345")
            self.assertEqual(interim["last_finished_at"], None)
            status = get_library_patrol_status({})
            self.assertTrue(status.data["continuation_pending"])
            self.assertEqual(status.data["cycle_checked_series_count"], 2)
            self.assertEqual(scheduler.run_once(), 1)

        executor.assert_has_calls([
            unittest.mock.call({"as_of": "2026-08-03", "max_series": 50}),
            unittest.mock.call({
                "as_of": "2026-08-03",
                "max_series": 50,
                "after_tmdb_id": "12345",
            }),
        ])
        row = db.get_agent_library_patrol()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["outcome"], "updates_available")
        self.assertEqual(row["checked_series_count"], 5)
        self.assertEqual(row["updates_available_count"], 1)
        self.assertEqual(row["missing_episode_count"], 2)
        self.assertEqual(row["cycle_cursor_tmdb_id"], "")
        self.assertEqual(row["cycle_accumulator_json"], "{}")

    def test_stalled_first_item_retries_then_skips_as_inconclusive(self):
        stalled_batches = []
        for _ in range(3):
            result = _patrol_result(status="inconclusive", ok=True)
            result.data.update({
                "checked_series_count": 0,
                "updates_available_count": 0,
                "missing_episode_count": 0,
                "inconclusive_count": 0,
                "findings": [],
                "continuation_pending": True,
                "last_processed_tmdb_id": "",
                "stalled_tmdb_id": "12345",
            })
            stalled_batches.append((result, 30_000))
        terminal = _patrol_result(status="up_to_date", ok=True)
        terminal.data.update({
            "checked_series_count": 1,
            "updates_available_count": 0,
            "missing_episode_count": 0,
            "inconclusive_count": 0,
            "findings": [],
            "continuation_pending": False,
            "last_processed_tmdb_id": "67890",
            "stalled_tmdb_id": "",
        })
        executor = Mock(side_effect=[*stalled_batches, (terminal, 10_000)])
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=executor,
            clock=self.clock,
        )

        with self._config():
            self.assertEqual(scheduler.run_once(), 1)
            first = db.get_agent_library_patrol()
            self.assertEqual(first["cycle_cursor_tmdb_id"], "")
            self.assertEqual(first["cycle_stall_attempts"], 1)
            status = get_library_patrol_status({})
            self.assertTrue(status.data["continuation_pending"])
            self.assertEqual(status.data["cycle_checked_series_count"], 0)

            self.assertEqual(scheduler.run_once(), 1)
            self.assertEqual(db.get_agent_library_patrol()["cycle_stall_attempts"], 2)

            self.assertEqual(scheduler.run_once(), 1)
            skipped = db.get_agent_library_patrol()
            self.assertEqual(skipped["cycle_cursor_tmdb_id"], "12345")
            self.assertEqual(skipped["cycle_stall_attempts"], 0)
            skipped_projection = json.loads(skipped["cycle_accumulator_json"])
            self.assertEqual(skipped_projection["checked_series_count"], 1)
            self.assertEqual(skipped_projection["inconclusive_count"], 1)

            self.assertEqual(scheduler.run_once(), 1)

        executor.assert_has_calls([
            unittest.mock.call({"as_of": "2026-08-03", "max_series": 50}),
            unittest.mock.call({"as_of": "2026-08-03", "max_series": 50}),
            unittest.mock.call({"as_of": "2026-08-03", "max_series": 50}),
            unittest.mock.call({
                "as_of": "2026-08-03",
                "max_series": 50,
                "after_tmdb_id": "12345",
            }),
        ])
        row = db.get_agent_library_patrol()
        self.assertEqual(row["status"], "retry_wait")
        self.assertEqual(row["outcome"], "inconclusive")
        self.assertEqual(row["checked_series_count"], 2)
        self.assertEqual(row["inconclusive_count"], 1)

    def test_reload_keeps_continuation_progress(self):
        first = _patrol_result(status="inconclusive", ok=True)
        first.data.update({
            "checked_series_count": 2,
            "continuation_pending": True,
            "last_processed_tmdb_id": "12345",
        })
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=Mock(return_value=(first, 30_000)),
            clock=self.clock,
        )

        with self._config():
            self.assertEqual(scheduler.run_once(), 1)
            scheduler.reload()

        row = db.get_agent_library_patrol()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["cycle_cursor_tmdb_id"], "12345")
        self.assertEqual(
            json.loads(row["cycle_accumulator_json"])["checked_series_count"], 2
        )

    def test_changed_success_enqueues_once_and_unchanged_result_is_deduplicated(self):
        executor = Mock(return_value=(_patrol_result(), 10))
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=executor,
            clock=self.clock,
        )
        with self._config(notify=True):
            self.assertEqual(scheduler.run_once(), 1)
            self.clock.value = datetime(2026, 8, 4, 12, 0, 0)
            self.assertEqual(scheduler.run_once(), 1)

        row = db.get_agent_library_patrol()
        notifications = db.list_agent_library_patrol_notifications()
        self.assertEqual(row["result_revision"], 1)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["status"], "pending")
        serialized = repr(dict(notifications[0]))
        for secret in ("private.invalid", "SECRET", "/volume/", "server_url", "sources"):
            self.assertNotIn(secret, serialized)

    def test_initial_up_to_date_establishes_baseline_without_notification(self):
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=Mock(return_value=(_patrol_result(status="up_to_date"), 10)),
            clock=self.clock,
        )
        with self._config(notify=True):
            self.assertEqual(scheduler.run_once(), 1)

        self.assertEqual(db.get_agent_library_patrol()["result_revision"], 1)
        self.assertEqual(db.list_agent_library_patrol_notifications(), [])

    def test_a_b_a_transitions_each_enqueue_a_new_revision(self):
        results = [
            _patrol_result(),
            _patrol_result(status="up_to_date"),
            _patrol_result(),
        ]
        results[1].data.update({
            "updates_available_count": 0,
            "missing_episode_count": 0,
            "findings": [],
        })
        executor = Mock(side_effect=[(item, 10) for item in results])
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=executor,
            clock=self.clock,
        )
        with self._config(notify=True):
            for day in (3, 4, 5):
                self.clock.value = datetime(2026, 8, day, 12, 0, 0)
                self.assertEqual(scheduler.run_once(), 1)

        notifications = db.list_agent_library_patrol_notifications()
        self.assertEqual([row["result_revision"] for row in notifications], [1, 2, 3])
        self.assertEqual(db.get_agent_library_patrol()["result_revision"], 3)

    def test_delivery_success_marks_outbox_sent(self):
        sender = Mock(return_value=True)
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=Mock(return_value=(_patrol_result(), 10)),
            notification_sender=sender,
            clock=self.clock,
        )
        with self._config(notify=True):
            scheduler.run_once()
            self.assertEqual(scheduler.dispatch_notification_once(), 1)

        sender.assert_called_once()
        notification = db.list_agent_library_patrol_notifications()[0]
        self.assertEqual(notification["status"], "sent")
        self.assertEqual(notification["sent_at"], "2026-08-03 12:00:00")
        self.assertEqual(notification["payload_json"], "")

    def test_delivery_failure_retries_without_rolling_back_patrol_result(self):
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=Mock(return_value=(_patrol_result(), 10)),
            notification_sender=Mock(return_value=False),
            clock=self.clock,
        )
        with self._config(notify=True):
            scheduler.run_once()
            self.assertEqual(scheduler.dispatch_notification_once(), 1)

        patrol = db.get_agent_library_patrol()
        notification = db.list_agent_library_patrol_notifications()[0]
        self.assertEqual(patrol["outcome"], "updates_available")
        self.assertEqual(notification["status"], "retry_wait")
        self.assertEqual(notification["attempts"], 1)
        self.assertEqual(notification["next_attempt_at"], "2026-08-03 12:01:00")
        self.assertEqual(notification["last_error_type"], "DeliveryFailed")

    def test_delivery_outcome_unknown_is_discarded_without_duplicate_retry(self):
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=Mock(return_value=(_patrol_result(), 10)),
            notification_sender=Mock(return_value=TelegramSendResult(
                ok=False, error="timeout", status_code=0,
            )),
            clock=self.clock,
        )
        with self._config(notify=True):
            scheduler.run_once()
            self.assertEqual(scheduler.dispatch_notification_once(), 1)

        notification = db.list_agent_library_patrol_notifications()[0]
        self.assertEqual(notification["status"], "discarded")
        self.assertEqual(notification["attempts"], 0)
        self.assertEqual(notification["payload_json"], "")
        self.assertEqual(notification["last_error_type"], "DeliveryOutcomeUnknown")

    def test_notification_disable_discards_backlog(self):
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=Mock(return_value=(_patrol_result(), 10)),
            clock=self.clock,
        )
        with self._config(notify=True):
            scheduler.run_once()
        with self._config(notify=False):
            scheduler.reload()

        notification = db.list_agent_library_patrol_notifications()[0]
        self.assertEqual(notification["status"], "discarded")
        self.assertEqual(notification["payload_json"], "")

    def test_notification_disabled_after_claim_does_not_start_sender(self):
        sender = Mock(return_value=True)
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=Mock(return_value=(_patrol_result(), 10)),
            notification_sender=sender,
            clock=self.clock,
        )
        with self._config(notify=True):
            scheduler.run_once()

        notification_enabled = True
        original_claim = db.claim_due_agent_library_patrol_notification

        def get_bool(key: str, default: bool = False) -> bool:
            if key == "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED":
                return notification_enabled
            if key == "AGENT_LIBRARY_PATROL_ENABLED":
                return True
            return default

        def claim_then_disable(**kwargs):
            nonlocal notification_enabled
            item = original_claim(**kwargs)
            notification_enabled = False
            return item

        with patch.multiple(
            "app.modules.agent_library_patrol_scheduler.config",
            get_bool=get_bool,
        ), patch(
            "app.modules.agent_library_patrol_scheduler.db."
            "claim_due_agent_library_patrol_notification",
            side_effect=claim_then_disable,
        ):
            self.assertEqual(scheduler.dispatch_notification_once(), 0)

        sender.assert_not_called()
        notification = db.list_agent_library_patrol_notifications()[0]
        self.assertEqual(notification["status"], "discarded")
        self.assertEqual(notification["payload_json"], "")

    def test_inconclusive_result_retries_without_claiming_complete(self):
        result = _patrol_result(status="inconclusive", ok=False)
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=Mock(return_value=(result, 20)),
            clock=self.clock,
        )
        with self._config():
            self.assertEqual(scheduler.run_once(), 1)

        row = db.get_agent_library_patrol()
        self.assertEqual(row["status"], "retry_wait")
        self.assertEqual(row["outcome"], "inconclusive")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["next_run_at"], "2026-08-03 12:15:00")


    def test_retry_budget_returns_to_normal_interval(self):
        result = _patrol_result(status="inconclusive", ok=False)
        executor = Mock(return_value=(result, 20))
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=executor,
            clock=self.clock,
        )
        with self._config(hours=24):
            self.assertEqual(scheduler.run_once(), 1)
            self.clock.value = datetime(2026, 8, 3, 12, 15, 0)
            self.assertEqual(scheduler.run_once(), 1)
            self.clock.value = datetime(2026, 8, 3, 13, 15, 0)
            self.assertEqual(scheduler.run_once(), 1)

        row = db.get_agent_library_patrol()
        self.assertEqual(executor.call_count, 3)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["next_run_at"], "2026-08-04 13:15:00")

    def test_disable_after_claim_releases_lease_without_executing(self):
        executor = Mock(return_value=(_patrol_result(), 10))
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=executor,
            clock=self.clock,
        )
        with patch.object(scheduler, "_enabled", side_effect=(True, False)):
            self.assertEqual(scheduler.run_once(), 0)

        executor.assert_not_called()
        row = db.get_agent_library_patrol()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["lease_generation"], 2)
        self.assertEqual(row["outcome"], "")

    def test_reload_while_disabled_invalidates_active_lease(self):
        db.ensure_agent_library_patrol(next_run_at="2026-08-03 12:00:00")
        job = db.claim_due_agent_library_patrol(current_time="2026-08-03 12:00:00")
        scheduler = AgentLibraryPatrolScheduler(clock=self.clock)

        with patch.object(scheduler, "_enabled", return_value=False):
            scheduler.reload()

        row = db.get_agent_library_patrol()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["lease_generation"], job["lease_generation"] + 1)
        self.assertEqual(row["next_run_at"], "2026-08-03 12:00:00")

    def test_exception_after_continuation_preserves_cursor_for_retry(self):
        first = _patrol_result(status="inconclusive", ok=True)
        first.data.update({
            "checked_series_count": 2,
            "continuation_pending": True,
            "last_processed_tmdb_id": "12345",
        })
        executor = Mock(side_effect=[(first, 30_000), RuntimeError("temporary")])
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=executor,
            clock=self.clock,
        )
        with self._config():
            self.assertEqual(scheduler.run_once(), 1)
            self.assertEqual(scheduler.run_once(), 1)

        row = db.get_agent_library_patrol()
        self.assertEqual(row["status"], "retry_wait")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["cycle_cursor_tmdb_id"], "12345")
        self.assertEqual(
            json.loads(row["cycle_accumulator_json"])["checked_series_count"], 2
        )

    def test_exception_stores_only_exception_type(self):
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=Mock(side_effect=RuntimeError("token=SECRET /volume/private")),
            clock=self.clock,
        )
        with self._config():
            self.assertEqual(scheduler.run_once(), 1)

        row = db.get_agent_library_patrol()
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(row["error_type"], "RuntimeError")
        serialized = repr(dict(row))
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("/volume", serialized)

    def test_status_tool_returns_queryable_snapshot_without_triggering_download(self):
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=Mock(return_value=(_patrol_result(), 10)),
            clock=self.clock,
        )
        with self._config():
            scheduler.run_once()
        with patch(
            "app.agent.library_patrol_status.config.get_bool",
            return_value=True,
        ):
            result = get_library_patrol_status({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "updates_available")
        self.assertEqual(result.data["missing_episode_count"], 2)
        self.assertIn("最近一次自动缺集巡检", result.summary)
        self.assertEqual(result.data["findings"][0]["missing_sample"][0], {
            "season": 2,
            "episode": 3,
        })
        self.assertIn("不会自动下载", " ".join(result.suggestions))

    def test_projection_rejects_uncontrolled_status_and_sensitive_title(self):
        result = _patrol_result(status="token=SECRET", ok=False)
        result.data["findings"][0]["title"] = "/mnt/private/api_key=SECRET"
        scheduler = AgentLibraryPatrolScheduler(
            audit_executor=Mock(return_value=(result, 10)),
            clock=self.clock,
        )
        with self._config():
            scheduler.run_once()

        row = db.get_agent_library_patrol()
        projection = json.loads(row["projection_json"])
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(projection["patrol_status"], "inconclusive")
        self.assertEqual(projection["options"], [])
        self.assertNotIn("SECRET", repr(dict(row)))
        self.assertNotIn("/mnt/", repr(dict(row)))

    def test_tampered_projection_fails_closed(self):
        db.ensure_agent_library_patrol(next_run_at="2026-08-04 12:00:00")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_library_patrol SET outcome='updates_available',"
                "projection_json=?,last_finished_at=? WHERE patrol_key='default'",
                (
                    json.dumps({"title": "https://private.invalid?token=SECRET"}),
                    "2026-08-03 12:00:00",
                ),
            )
        with patch(
            "app.agent.library_patrol_status.config.get_bool",
            return_value=True,
        ):
            result = get_library_patrol_status({})

        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.data["outcome"], "inconclusive")
        self.assertEqual(result.data["findings"], [])
        self.assertEqual(result.data["checked_series_count"], 0)
        self.assertIn("覆盖不完整", result.summary)
        self.assertNotIn("SECRET", repr(result.to_dict()))


if __name__ == "__main__":
    unittest.main()
