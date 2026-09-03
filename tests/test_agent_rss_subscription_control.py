"""RSS 订阅管理动作的严格解析、确认、竞态与脱敏回归。"""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

from app import database as db
from app.agent.action_history import action_history_owner_digest
from app.agent.errors import AgentToolError
from app.agent.rate_limit import agent_rate_limiter
from app.agent.rss_subscription_control_actions import (
    rss_create_subscription_arguments,
    rss_update_subscription_arguments,
)
from app.modules.rss_subscription_config import (
    RSSSubscriptionConfigError,
    normalize_rss_subscription_create,
)
from tests.agent_kernel_test_harness import (
    get_kernel_test_service as get_agent_service,
)
from tests.agent_kernel_test_harness import (
    reset_kernel_test_service as reset_agent_service_for_tests,
)
from tests.support import IsolatedDatabaseTestCase


class RSSSubscriptionControlTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM rss_entries")
            conn.execute("DELETE FROM rss_items")
            conn.execute("DELETE FROM agent_action_history")
        reset_agent_service_for_tests()
        self.sid = db.add_rss_subscription(
            "Private Feed",
            "https://secret.invalid/rss?passkey=RSS_SECRET",
            exclude_keywords="PRIVATE_FILTER",
            enabled=1,
            refresh_interval_minutes=30,
        )

    def tearDown(self) -> None:
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    def _confirm(self, tool: str, arguments: dict):
        service = get_agent_service()
        prepared = service.prepare(tool, arguments, owner="owner")
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        return (prepared, confirmed)

    def test_enable_and_interval_confirm_reload_runtime_without_private_data(self):
        scheduler = Mock()
        with patch(
            "app.modules.rss_scheduler.get_rss_scheduler", return_value=scheduler
        ):
            prepared, confirmed = self._confirm(
                "rss.update_subscription",
                {"subscription_id": self.sid, "enabled": False},
            )
            prepared2, confirmed2 = self._confirm(
                "rss.update_subscription",
                {"subscription_id": self.sid, "refresh_interval_minutes": 0},
            )
        self.assertEqual(scheduler.reload.call_count, 2)
        row = db.get_rss_subscription(self.sid)
        self.assertEqual(int(row["enabled"]), 0)
        self.assertEqual(int(row["refresh_interval_minutes"]), 0)
        self.assertTrue(confirmed["result"]["data"]["runtime_refreshed"])
        self.assertTrue(confirmed2["result"]["data"]["runtime_refreshed"])
        serialized = json.dumps(
            {
                "prepared": prepared,
                "confirmed": confirmed,
                "prepared2": prepared2,
                "confirmed2": confirmed2,
            },
            ensure_ascii=False,
        )
        for secret in (
            "Private Feed",
            "secret.invalid",
            "passkey",
            "RSS_SECRET",
            "PRIVATE_FILTER",
        ):
            self.assertNotIn(secret, serialized)

    def test_stale_subscription_update_conflicts(self):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.update_subscription",
            {"subscription_id": self.sid, "enabled": False},
            owner="owner",
        )
        db.update_rss_subscription(self.sid, {"exclude_keywords": "CHANGED_SECRET"})
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertFalse(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["status"], "conflict")
        self.assertEqual(int(db.get_rss_subscription(self.sid)["enabled"]), 1)

    def test_delete_is_cascaded_locally_and_stale_entry_count_conflicts(self):
        db.add_rss_entry(self.sid, "PRIVATE_TITLE", "secret-guid")
        other = db.add_rss_subscription("Other", "https://other.invalid/rss")
        db.add_rss_entry(other, "OTHER_TITLE", "other-guid")
        service = get_agent_service()
        prepared = service.prepare(
            "rss.delete_subscription", {"subscription_id": self.sid}, owner="owner"
        )
        db.add_rss_entry(self.sid, "LATE_PRIVATE_TITLE", "late-guid")
        conflict = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(conflict["result"]["status"], "conflict")
        self.assertIsNotNone(db.get_rss_subscription(self.sid))
        with patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=Mock()):
            _, confirmed = self._confirm(
                "rss.delete_subscription", {"subscription_id": self.sid}
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["data"]["deleted_entries"], 2)
        self.assertIsNone(db.get_rss_subscription(self.sid))
        self.assertIsNotNone(db.get_rss_subscription(other))
        with db.get_conn() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM rss_entries WHERE rss_item_id=?", (other,)
                ).fetchone()[0],
                1,
            )

    def test_reload_failure_keeps_committed_change_and_history_is_safe(self):
        with patch(
            "app.modules.rss_scheduler.get_rss_scheduler",
            side_effect=RuntimeError("secret scheduler failure"),
        ):
            _, confirmed = self._confirm(
                "rss.update_subscription",
                {"subscription_id": self.sid, "enabled": False},
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertFalse(confirmed["result"]["data"]["runtime_refreshed"])
        self.assertEqual(int(db.get_rss_subscription(self.sid)["enabled"]), 0)
        history = db.list_agent_action_history(
            owner_digest=action_history_owner_digest("owner"), limit=1
        )[0]
        details = json.loads(history["safe_details"])
        self.assertEqual(details["operation"], "update")
        self.assertEqual(details["affected"], 1)
        self.assertNotIn("subscription_id", details)
        serialized = json.dumps(confirmed, ensure_ascii=False)
        self.assertNotIn("secret scheduler failure", serialized)

    def test_create_and_update_use_shared_safe_config_chain(self):
        create_arguments = {
            "name": "Anime Feed",
            "urls": ["https://feed.example/rss"],
            "exclude_keywords": "CAM",
            "action": "download",
            "enabled": True,
            "refresh_interval_minutes": 45,
            "download_method": "qb",
            "media_tmdb_id": "12345",
            "media_default_season": 2,
            "skip_existing_episodes": True,
        }
        normalized = rss_create_subscription_arguments(create_arguments)
        self.assertEqual(normalized["urls"], "https://feed.example/rss")
        self.assertEqual(normalized["download_method"], "qb")
        with self.assertRaises(AgentToolError):
            rss_create_subscription_arguments(
                {**create_arguments, "qb_save_path": "/unsafe/path"}
            )
        service = get_agent_service()
        with patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=Mock()):
            prepared = service.prepare(
                "rss.create_subscription", create_arguments, owner="owner"
            )
            serialized = json.dumps(prepared, ensure_ascii=False)
            self.assertNotIn("feed.example", serialized)
            created = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertTrue(created["result"]["ok"])
        self.assertTrue(created["result"]["data"]["verified"])
        self.assertIn("已创建并核验", created["result"]["summary"])
        created_id = int(created["result"]["data"]["subscription_id"])
        self.assertEqual(created["result"]["data"]["subscription_number"], created_id)
        row = db.get_rss_subscription(created_id)
        self.assertEqual(row["name"], "Anime Feed")
        self.assertEqual(row["media_tmdb_id"], "12345")
        update_arguments = rss_update_subscription_arguments(
            {
                "subscription_id": created_id,
                "name": "Anime Feed Updated",
                "refresh_interval_minutes": 60,
            }
        )
        with patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=Mock()):
            prepared = service.prepare(
                "rss.update_subscription", update_arguments, owner="owner"
            )
            updated = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertTrue(updated["result"]["ok"])
        row = db.get_rss_subscription(created_id)
        self.assertEqual(row["name"], "Anime Feed Updated")
        self.assertEqual(int(row["refresh_interval_minutes"]), 60)

    def test_create_reports_outcome_unknown_when_readback_cannot_verify(self):
        service = get_agent_service()
        arguments = {
            "name": "Readback Feed",
            "urls": ["https://feed.example/readback.xml"],
            "action": "subscribe",
            "enabled": False,
            "refresh_interval_minutes": 360,
            "download_method": "qb",
        }
        prepared = service.prepare("rss.create_subscription", arguments, owner="owner")
        with patch(
            "app.agent.rss_subscription_control_actions.db.get_rss_subscription",
            return_value=None,
        ):
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertFalse(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["status"], "outcome_unknown")
        self.assertFalse(confirmed["result"]["data"]["verified"])
        self.assertIn("避免重复创建", confirmed["result"]["suggestions"][0])

    def test_update_urls_validator_is_idempotent_and_confirmation_succeeds(self):
        arguments = {
            "subscription_id": self.sid,
            "urls": ["https://updated.example/rss"],
        }
        normalized = rss_update_subscription_arguments(arguments)
        self.assertEqual(
            normalized,
            {"subscription_id": self.sid, "urls": "https://updated.example/rss"},
        )
        self.assertEqual(rss_update_subscription_arguments(normalized), normalized)
        service = get_agent_service()
        with patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=Mock()):
            prepared = service.prepare(
                "rss.update_subscription", arguments, owner="owner"
            )
            self.assertNotIn(
                "updated.example", json.dumps(prepared, ensure_ascii=False)
            )
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertEqual(
            db.get_rss_subscription(self.sid)["urls"], "https://updated.example/rss"
        )

    def test_shared_config_integer_fields_reject_lossy_values(self):
        base = {"name": "Strict Feed", "urls": "https://example.invalid/rss"}
        for field in ("refresh_interval_minutes", "media_default_season"):
            for invalid in (True, 1.0, 1.9, "1.0", "1e3", "not-an-integer"):
                with (
                    self.subTest(field=field, invalid=invalid),
                    self.assertRaises(RSSSubscriptionConfigError),
                ):
                    normalize_rss_subscription_create({**base, field: invalid})
        normalized = normalize_rss_subscription_create(
            {**base, "refresh_interval_minutes": "45", "media_default_season": "2"}
        )
        self.assertEqual(normalized["refresh_interval_minutes"], 45)
        self.assertEqual(normalized["media_default_season"], 2)

    def test_update_confirmation_uses_one_write_transaction_for_revision_and_bindings(
        self,
    ):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.update_subscription",
            {
                "subscription_id": self.sid,
                "name": "Atomic Feed",
                "media_tmdb_id": "12345",
                "media_default_season": 2,
            },
            owner="owner",
        )
        original_update = db.update_rss_subscription
        observed: list[tuple[bool, bool]] = []

        def update_in_transaction(sub_id, fields, **kwargs):
            connection = kwargs.get("connection")
            observed.append((connection is not None, bool(connection.in_transaction)))
            return original_update(sub_id, fields, **kwargs)

        with patch(
            "app.agent.rss_subscription_control_actions.db.update_rss_subscription",
            side_effect=update_in_transaction,
        ):
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertEqual(observed, [(True, True)])
        current = db.get_rss_subscription(self.sid)
        self.assertEqual(current["name"], "Atomic Feed")
        self.assertEqual(current["media_tmdb_id"], "12345")
        self.assertEqual(current["media_default_season"], 2)

    def test_update_confirmation_conflicts_after_subscription_changes(self):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.update_subscription",
            {"subscription_id": self.sid, "exclude_keywords": "SAFE"},
            owner="owner",
        )
        db.update_rss_subscription(self.sid, {"refresh_interval_minutes": 31})
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertFalse(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["status"], "conflict")
        self.assertEqual(
            db.get_rss_subscription(self.sid)["exclude_keywords"], "PRIVATE_FILTER"
        )
