"""RSS 订阅管理动作的严格解析、确认、竞态与脱敏回归。"""
from __future__ import annotations

import json
from unittest.mock import Mock, patch

from app import database as db
from app.agent.action_history import action_history_owner_digest
from app.agent.orchestrator import (
    is_rss_subscription_control_write_message,
    rss_subscription_control_name_request,
    rss_subscription_control_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.rss_subscription_control_actions import (
    rss_delete_subscription_arguments,
    rss_refresh_interval_arguments,
    rss_subscription_enabled_arguments,
)
from app.agent.service import get_agent_service, reset_agent_service_for_tests
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
        confirmed = service.confirm(prepared["confirmation"]["confirmation_id"], owner="owner")
        return prepared, confirmed

    def test_validators_registry_and_natural_language_are_strict(self):
        self.assertEqual(
            rss_subscription_enabled_arguments({"subscription_id": self.sid, "enabled": False}),
            {"subscription_id": self.sid, "enabled": False},
        )
        self.assertEqual(
            rss_refresh_interval_arguments({
                "subscription_id": self.sid, "refresh_interval_minutes": 0,
            })["refresh_interval_minutes"],
            0,
        )
        self.assertEqual(
            rss_delete_subscription_arguments({"subscription_id": self.sid}),
            {"subscription_id": self.sid},
        )
        invalid_enabled = ({}, {"subscription_id": self.sid, "enabled": 1},
                           {"subscription_id": True, "enabled": False},
                           {"subscription_id": self.sid, "enabled": False, "all": True})
        for invalid in invalid_enabled:
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                rss_subscription_enabled_arguments(invalid)
        for interval in (-1, 10081, True, "30"):
            with self.subTest(interval=interval), self.assertRaises(AgentToolError):
                rss_refresh_interval_arguments({
                    "subscription_id": self.sid,
                    "refresh_interval_minutes": interval,
                })

        tools = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        self.assertEqual(tools["rss.set_subscription_enabled"]["risk"], "low_write")
        self.assertEqual(tools["rss.set_refresh_interval"]["risk"], "low_write")
        self.assertEqual(tools["rss.delete_subscription"]["risk"], "danger")
        self.assertTrue(tools["rss.delete_subscription"]["requires_confirmation"])

        self.assertEqual(
            rss_subscription_control_request(f"停用 RSS 订阅 {self.sid}"),
            ("rss.set_subscription_enabled", {"subscription_id": self.sid, "enabled": False}),
        )
        self.assertEqual(
            rss_subscription_control_request(f"将 RSS 订阅 {self.sid} 刷新周期设为 2 小时"),
            ("rss.set_refresh_interval", {
                "subscription_id": self.sid, "refresh_interval_minutes": 120,
            }),
        )
        self.assertEqual(
            rss_subscription_control_request(f"删除 RSS 订阅 {self.sid}"),
            ("rss.delete_subscription", {"subscription_id": self.sid}),
        )
        for message in ("停用全部 RSS", "删除 RSS 订阅", "RSS 订阅刷新间隔怎么样"):
            self.assertIsNone(rss_subscription_control_request(message))
        self.assertEqual(
            rss_subscription_control_name_request("停用 Mikan RSS 订阅"),
            ("rss.set_subscription_enabled", "Mikan", {"enabled": False}),
        )
        self.assertEqual(
            rss_subscription_control_name_request("将 RSS 订阅 Mikan 刷新周期设为 2 小时"),
            ("rss.set_refresh_interval", "Mikan", {"refresh_interval_minutes": 120}),
        )
        self.assertEqual(
            rss_subscription_control_name_request("删除 Mikan RSS 订阅"),
            ("rss.delete_subscription", "Mikan", {}),
        )
        for message in ("停用全部 RSS 订阅", "删除所有 RSS 订阅", "将全部 RSS 订阅刷新周期设为 1 小时"):
            self.assertIsNone(rss_subscription_control_name_request(message))
        self.assertTrue(is_rss_subscription_control_write_message("停用全部 RSS 订阅"))

    def test_enable_and_interval_confirm_reload_runtime_without_private_data(self):
        scheduler = Mock()
        with patch("app.modules.rss_scheduler.get_rss_scheduler", return_value=scheduler):
            prepared, confirmed = self._confirm(
                "rss.set_subscription_enabled",
                {"subscription_id": self.sid, "enabled": False},
            )
            prepared2, confirmed2 = self._confirm(
                "rss.set_refresh_interval",
                {"subscription_id": self.sid, "refresh_interval_minutes": 0},
            )
        self.assertEqual(scheduler.reload.call_count, 2)
        row = db.get_rss_subscription(self.sid)
        self.assertEqual(int(row["enabled"]), 0)
        self.assertEqual(int(row["refresh_interval_minutes"]), 0)
        self.assertTrue(confirmed["result"]["data"]["runtime_refreshed"])
        self.assertTrue(confirmed2["result"]["data"]["runtime_refreshed"])
        serialized = json.dumps(
            {"prepared": prepared, "confirmed": confirmed, "prepared2": prepared2,
             "confirmed2": confirmed2}, ensure_ascii=False,
        )
        for secret in ("Private Feed", "secret.invalid", "passkey", "RSS_SECRET", "PRIVATE_FILTER"):
            self.assertNotIn(secret, serialized)

    def test_stale_subscription_update_conflicts(self):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.set_subscription_enabled",
            {"subscription_id": self.sid, "enabled": False},
            owner="owner",
        )
        db.update_rss_subscription(self.sid, {"exclude_keywords": "CHANGED_SECRET"})
        confirmed = service.confirm(prepared["confirmation"]["confirmation_id"], owner="owner")
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
        conflict = service.confirm(prepared["confirmation"]["confirmation_id"], owner="owner")
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
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM rss_entries WHERE rss_item_id=?", (other,)
            ).fetchone()[0], 1)

    def test_reload_failure_keeps_committed_change_and_history_is_safe(self):
        with patch(
            "app.modules.rss_scheduler.get_rss_scheduler",
            side_effect=RuntimeError("secret scheduler failure"),
        ):
            _, confirmed = self._confirm(
                "rss.set_subscription_enabled",
                {"subscription_id": self.sid, "enabled": False},
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertFalse(confirmed["result"]["data"]["runtime_refreshed"])
        self.assertEqual(int(db.get_rss_subscription(self.sid)["enabled"]), 0)
        history = db.list_agent_action_history(
            owner_digest=action_history_owner_digest("owner"), limit=1
        )[0]
        details = json.loads(history["safe_details"])
        self.assertEqual(details["operation"], "disable")
        self.assertEqual(details["affected"], 1)
        self.assertNotIn("subscription_id", details)
        serialized = json.dumps(confirmed, ensure_ascii=False)
        self.assertNotIn("secret scheduler failure", serialized)

    def test_orchestrator_prepares_exact_subscription_control(self):
        service = get_agent_service()
        result = service.query(f"停用 RSS 订阅 {self.sid}", owner="owner")
        self.assertEqual(result["mode"], "confirmation_required")
        self.assertEqual(result["confirmation"]["tool"], "rss.set_subscription_enabled")

    def test_orchestrator_resolves_unique_subscription_name_for_controls(self):
        service = get_agent_service()
        for message, tool in (
            ("停用 Private Feed RSS 订阅", "rss.set_subscription_enabled"),
            ("将 Private Feed RSS 订阅刷新周期设为 45 分钟", "rss.set_refresh_interval"),
            ("删除 Private Feed RSS 订阅", "rss.delete_subscription"),
        ):
            with self.subTest(message=message):
                result = service.query(message, owner="owner")
                self.assertEqual(result["mode"], "confirmation_required")
                self.assertEqual(result["confirmation"]["tool"], tool)
                serialized = json.dumps(result, ensure_ascii=False)
                self.assertNotIn("secret.invalid", serialized)
                self.assertNotIn("RSS_SECRET", serialized)

    def test_orchestrator_does_not_guess_duplicate_or_unknown_subscription_names(self):
        db.add_rss_subscription("Ｐｒｉｖａｔｅ　Ｆｅｅｄ", "https://duplicate.invalid/rss")
        duplicate = get_agent_service().query("停用 Private Feed RSS 订阅", owner="owner")
        self.assertEqual(duplicate["result"]["status"], "unsupported")
        self.assertIn("多个名为", duplicate["result"]["summary"])
        self.assertNotIn("duplicate.invalid", json.dumps(duplicate, ensure_ascii=False))

        unknown = get_agent_service().query("停用 Missing RSS 订阅", owner="owner")
        self.assertEqual(unknown["result"]["status"], "unsupported")
        self.assertIn("没有找到名为《Missing》", unknown["result"]["summary"])


if __name__ == "__main__":
    import unittest
    unittest.main()
