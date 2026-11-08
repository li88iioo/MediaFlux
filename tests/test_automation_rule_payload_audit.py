"""损坏的历史规则只隔离自身，不阻断查询、有效规则领取或修复。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from app import database as db
from app.modules.media_automation_rules import digests
from app.agent.models import ToolContext
from app.modules.media_automation_rules import drain_automation_rules
from app.repositories import media_automation_rules as rules
from app.repositories.agent_jobs import agent_job_owner_digest
from tests.support import IsolatedDatabaseTestCase


class AutomationRulePayloadAuditTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_automation_rules")
        self.clock = datetime(2026, 1, 2, 8, tzinfo=timezone.utc)
        self.context = ToolContext(owner="payload-audit-owner")
        self.owner = agent_job_owner_digest(self.context.owner)

    def _save(self, *, settings=None, due=None):
        row = rules.save_rule(
            self.owner,
            kind="daily_summary",
            settings={"hour": 8, "minute": 0} if settings is None else settings,
            enabled=True,
            next_run_at=(due or self.clock).isoformat(),
        )
        assert row is not None
        return row

    def _corrupt(self, row, payload):
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_automation_rules SET settings_json=? WHERE id=?",
                (payload, row["id"]),
            )

    def test_corrupt_payloads_remain_visible_without_exposing_raw_data(self):
        payloads = [
            '{"secret": "private-marker",',
            "null",
            "[]",
            '"private-marker"',
            "3",
            '{"nested":{"value":NaN}}',
            '{"nested":{"value":Infinity}}',
            '{"nested":{"value":1e10000}}',
            '{"value":"' + "界" * 6_000 + '"}',
        ]
        for payload in payloads:
            row = self._save()
            self._corrupt(row, payload)
        items = digests(owner=self.context.owner)["data"]["items"]
        self.assertEqual(len(items), len(payloads))
        for item in items:
            self.assertTrue(item.get("settings_error"))
            self.assertEqual(item["settings"]["hour"], None)
        self.assertNotIn("private-marker", json.dumps(items))
        self.assertEqual(rules.claim_due_rules(self.clock), [])

    def test_runtime_recursion_error_is_contained_without_rewriting_history(self):
        row = self._save()
        with patch(
            "app.repositories.media_automation_rules.json.loads",
            side_effect=RecursionError("runtime depth limit"),
        ):
            read = rules.get_rule(self.owner, row["id"])
            assert read is not None
            self.assertTrue(read.get("settings_error"))
            self.assertEqual(rules.claim_due_rules(self.clock), [])
        get_rule_result = rules.get_rule(self.owner, row["id"])
        assert get_rule_result is not None
        self.assertEqual(
            get_rule_result["settings"], row["settings"]
        )

    def test_invalid_rows_beyond_batch_limit_do_not_starve_valid_rule(self):
        broken_ids = []
        for _ in range(25):
            row = self._save(due=self.clock - timedelta(minutes=1))
            self._corrupt(row, "[]")
            broken_ids.append(row["id"])
        good = self._save()
        claimed = rules.claim_due_rules(self.clock, limit=1)
        self.assertEqual([row["id"] for row in claimed], [good["id"]])
        self.assertEqual(rules.claim_due_rules(self.clock, limit=1), [])
        with db.get_conn() as conn:
            invalid = conn.execute(
                "SELECT lease_token,lease_until,settings_json FROM media_automation_rules "
                "WHERE id<>?",
                (good["id"],),
            ).fetchall()
        self.assertEqual(len(invalid), len(broken_ids))
        self.assertTrue(all(tuple(row) == ("", "", "[]") for row in invalid))

    def test_normal_optional_settings_and_empty_object_keep_existing_contract(self):
        values = [{}, {"hour": 8, "minute": 0, "optional": {"value": 0.25}}]
        rows = [self._save(settings=value) for value in values]
        for row, value in zip(rows, values):
            self.assertEqual(row["settings"], value)
            self.assertNotIn("settings_error", row)
        self.assertEqual(len(rules.claim_due_rules(self.clock)), 2)

    def test_nonfinite_new_settings_are_rejected_before_persistence(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self._save(settings={"value": {"nested": value}})
        self.assertEqual(rules.list_rules(self.owner), [])

    def test_corrupt_rule_can_be_repaired_or_deleted_with_existing_cas(self):
        broken = self._save()
        self._corrupt(broken, '{"hour":')
        read = rules.get_rule(self.owner, broken["id"])
        assert read is not None
        self.assertTrue(read.get("settings_error"))
        self.assertIsNone(rules.get_rule("another-owner", broken["id"]))
        fixed = rules.save_rule(
            self.owner,
            kind="daily_summary",
            settings={"hour": 8, "minute": 0},
            enabled=True,
            next_run_at=self.clock.isoformat(),
            rule_id=broken["id"],
            expected_revision=read["revision"],
        )
        assert fixed is not None
        self.assertNotIn("settings_error", fixed)
        assert fixed is not None
        self.assertEqual(fixed["revision"], read["revision"] + 1)
        self._corrupt(fixed, "null")
        self.assertTrue(
            rules.delete_rule(
                self.owner, fixed["id"], expected_revision=fixed["revision"]
            )
        )
        self.assertEqual(rules.list_rules(self.owner), [])

    def test_good_rule_handoff_and_repeat_survive_bad_neighbor(self):
        broken = self._save(due=self.clock - timedelta(minutes=1))
        self._corrupt(broken, '{"hour":')
        good = self._save(settings={"hour": 8, "minute": 0, "send_empty": True})
        with (
            patch(
                "app.modules.media_automation_rules.is_agent_enabled", return_value=True
            ),
            patch(
                "app.modules.media_automation_rules._authorized_notification_chat",
                return_value="123",
            ),
            patch(
                "app.modules.media_automation_rules.today_content_summary",
                return_value={"downloads": {"success": 1}},
            ),
            patch(
                "app.modules.media_automation_rules.publish_notification_event",
                return_value=Mock(status="queued"),
            ) as publish,
        ):
            self.assertEqual(drain_automation_rules(now=self.clock, limit=1), 1)
            db.init_db()
            self.assertEqual(drain_automation_rules(now=self.clock, limit=1), 0)
        publish.assert_called_once()
        self.assertIn(good["id"], publish.call_args.args[0])
        self.assertFalse(publish.call_args.kwargs["deliver_now"])
