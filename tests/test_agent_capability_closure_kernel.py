"""新增能力的真实 ToolPipeline→EffectPlan→确认→安全引用纵切。"""

import json
from unittest.mock import patch

from app import config
from app import database as db
from app.agent.action_history import action_history_owner_digest
from app.agent.errors import AgentToolError
from app.agent.owner_routes import web_kernel_owner
from app.repositories.media_experience import get_media_preferences
from tests.agent_kernel_test_harness import KernelDomainTestHarness
from tests.support import IsolatedDatabaseTestCase


class ClosureKernelTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_compensations")
            conn.execute("DELETE FROM agent_media_preferences")
            conn.execute("DELETE FROM media_automation_rules")
            conn.execute("DELETE FROM download_request_keys")
            conn.execute("DELETE FROM download_log")
            conn.execute("DELETE FROM download_requests")
        self.kernel = KernelDomainTestHarness()

    def test_setting_confirmation_emits_scoped_undo_reference_and_undo_is_confirmed(
        self,
    ):
        owner = "test-closure-owner"
        digest = action_history_owner_digest(owner)
        plan = self.kernel.prepare(
            "media.set_preferences", {"preferred_download_target": "qb"}, owner=owner
        )
        self.assertFalse(get_media_preferences(digest)["explicit"])
        result = self.kernel.confirm(plan["action_plan"]["plan_id"], owner=owner)
        self.assertTrue(result["result"]["ok"])
        reference = result["result"]["reference_arguments"]["undo_receipt_ref"]
        self.assertNotIn("compensation_after", json.dumps(result))
        self.assertNotIn("revision_token", json.dumps(result))
        with self.assertRaises(AgentToolError):
            self.kernel.prepare(
                "action.undo.execute",
                {"undo_receipt_ref": reference},
                owner="another-owner",
            )
        undo = self.kernel.prepare(
            "action.undo.execute", {"undo_receipt_ref": reference}, owner=owner
        )
        self.assertEqual(
            get_media_preferences(digest)["preferred_download_target"], "qb"
        )
        confirmed = self.kernel.confirm(undo["action_plan"]["plan_id"], owner=owner)
        self.assertTrue(confirmed["result"]["ok"])
        self.assertFalse(get_media_preferences(digest)["explicit"])
        with self.assertRaises(AgentToolError):
            self.kernel.confirm(undo["action_plan"]["plan_id"], owner=owner)

    def test_activity_selection_follow_and_stop_use_same_confirmation_path(self):
        owner = web_kernel_owner("follow-kernel-owner")
        request_id, _ = db.create_download_request(
            "kernel-follow", "magnet", title="下载纵切示例"
        )
        db.update_download_request(
            request_id, status="downloading", gy_status="downloading"
        )
        original_get = config.get
        with (
            patch("app.agent.feature_gate.is_agent_enabled", return_value=True),
            patch(
                "app.modules.media_automation_rules.config.get",
                side_effect=lambda key, *a, **kw: (
                    "123" if key == "TG_CHAT_ID" else original_get(key, *a, **kw)
                ),
            ),
        ):
            found = self.kernel.invoke(
                "activity.search", {"query": "下载纵切示例"}, owner=owner
            )
            reference = found["result"]["reference_arguments"]["activity_selection_ref"]
            timeline = self.kernel.invoke(
                "activity.timeline", {"activity_selection_ref": reference}, owner=owner
            )
            self.assertEqual(
                timeline["result"]["data"]["freshness"], "persisted_snapshot"
            )
            prepared = self.kernel.prepare(
                "activity.follow",
                {"activity_selection_ref": reference, "hours": 2},
                owner=owner,
            )
            with db.get_conn() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM media_automation_rules"
                    ).fetchone()[0],
                    0,
                )
            saved = self.kernel.confirm(prepared["action_plan"]["plan_id"], owner=owner)
            self.assertTrue(saved["result"]["ok"])
            rule_id = saved["result"]["data"]["rule_id"]
            stop = self.kernel.prepare(
                "activity.unfollow", {"rule_id": rule_id}, owner=owner
            )
            self.assertTrue(
                self.kernel.confirm(stop["action_plan"]["plan_id"], owner=owner)[
                    "result"
                ]["ok"]
            )
            with db.get_conn() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT enabled FROM media_automation_rules WHERE id=?",
                        (rule_id,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM download_requests WHERE id=?", (request_id,)
                    ).fetchone()[0],
                    "downloading",
                )
