from __future__ import annotations

import json

from app import database as db
from app.agent.activity_actions import (
    attach_activity_reference,
    get_activity_timeline,
    search_activities,
    search_arguments,
    selection_arguments,
)
from app.agent.models import ToolContext, ToolResult
from tests.support import IsolatedDatabaseTestCase


class ActivityClosureTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM download_request_keys")
            conn.execute("DELETE FROM download_log")
            conn.execute("DELETE FROM download_requests")
            conn.execute("DELETE FROM task_runs")

    def make_request(self, key="a", title="示例剧"):
        identifier, _ = db.create_download_request(
            key, "magnet", title=title, origin="agent"
        )
        db.update_download_request(
            identifier, targets="guangya", status="downloading", gy_status="downloading"
        )
        return identifier

    def resolved(self, identifier):
        return {
            "activity_selection": {"items": [{"kind": "download", "id": identifier}]},
            "position": 1,
        }

    def test_stages_use_persisted_request_links_not_equal_titles(self):
        identifier = self.make_request()
        unrelated = self.make_request("b")
        run_id = db.add_task_run("strm", "organize")
        db.finish_task_run(run_id, "failed", error="索引站点响应超时")
        db.update_download_request(
            identifier,
            strm_run_id=run_id,
            strm_status="failed",
            strm_error="索引站点响应超时",
        )
        db.update_download_request(
            unrelated, organize_status="failed", organize_error="不属于当前请求"
        )
        value = get_activity_timeline(self.resolved(identifier), ToolContext(owner="a"))
        self.assertEqual(value.status, "attention")
        public = json.dumps(value.to_dict(), ensure_ascii=False)
        self.assertIn("索引站点响应超时", public)
        self.assertNotIn("不属于当前请求", public)
        self.assertIn("未建立媒体库可见性复核", public)
        self.assertEqual(value.data["freshness"], "persisted_snapshot")

    def test_no_in_library_claim_when_only_download_completed(self):
        identifier = self.make_request()
        db.update_download_request(
            identifier, status="completed", gy_status="completed"
        )
        value = get_activity_timeline(self.resolved(identifier), ToolContext(owner="a"))
        self.assertEqual(value.status, "completed")
        self.assertTrue(value.data["gaps"])
        self.assertNotIn("已入库", value.summary)

    def test_deleted_request_is_explicit_not_found(self):
        value = get_activity_timeline(self.resolved(999999), ToolContext(owner="a"))
        self.assertFalse(value.ok)
        self.assertEqual(value.status, "not_found")

    def test_search_public_hides_identifiers_and_literal_wildcards(self):
        self.make_request(title="100%示例")
        self.make_request("b", "另一部")
        result = search_activities(
            search_arguments({"query": "%"}), ToolContext(owner="a")
        )
        self.assertEqual(len(result.data["items"]), 1)
        self.assertNotIn('"id"', json.dumps(result.to_dict()))
        self.assertEqual(result.references[0].kind, "activity_selection")

    def test_errors_do_not_publish_paths_or_tokens(self):
        identifier = self.make_request()
        db.update_download_request(
            identifier,
            status="failed",
            gy_status="failed",
            error="token=secret /volume/private",
        )
        result = get_activity_timeline(
            self.resolved(identifier), ToolContext(owner="a")
        )
        public = json.dumps(result.to_dict())
        self.assertNotIn("secret", public)
        self.assertNotIn("/volume", public)
        self.assertTrue(result.data["needs_attention"])

    def test_completed_batch_retains_successful_refs_in_original_order(self):
        result = ToolResult(
            True,
            "partial",
            "部分受理",
            data={
                "items": [
                    {"request_id": 5},
                    {"request_id": 7},
                    {"request_id": 0},
                    {"request_id": 5},
                ]
            },
        )
        attach_activity_reference(result, "ingest.submit")
        self.assertEqual(
            [item["id"] for item in result.references[0].value["items"]], [5, 7]
        )
        self.assertEqual(result.references[0].ttl_seconds, 86400)

    def test_validation_rejects_forged_objects_bool_limits_and_bad_index(self):
        for args in (
            {"activity_selection_ref": {"items": []}},
            {"activity_selection_ref": "ref_" + "a" * 24, "position": True},
        ):
            with self.assertRaises(ValueError):
                selection_arguments(args)
        with self.assertRaises(ValueError):
            search_arguments({"limit": True})

    def test_has_more_does_not_claim_all_records(self):
        self.make_request()
        self.make_request("b")
        result = search_activities(search_arguments({"limit": 1}), ToolContext())
        self.assertTrue(result.data["has_more"])
        self.assertEqual(len(result.data["items"]), 1)

    def test_invalid_snapshot_does_not_infer_actions(self):
        with self.assertRaises(ValueError):
            get_activity_timeline(
                {"activity_selection": {"items": [{"kind": "download", "id": "123"}]}},
                ToolContext(),
            )

    def test_resolved_historical_failure_is_not_current_attention(self):
        from unittest.mock import patch

        from app.agent.activity_actions import timeline_snapshot

        snapshot = {
            "record": {
                "status": "success",
                "title": "示例",
                "updated_at": "2026-09-05 10:00:00",
            },
            "steps": [
                {
                    "action": "move_rename",
                    "status": "success",
                    "finished_at": "2026-09-05 10:00:00",
                },
                {
                    "action": "move_rename",
                    "status": "failed",
                    "error": "旧失败已处理",
                    "finished_at": "2026-09-05 09:00:00",
                },
            ],
        }
        with patch(
            "app.agent.activity_actions.activity.snapshot", return_value=snapshot
        ):
            result = timeline_snapshot({"kind": "organize", "id": 1})
        self.assertFalse(result.data["needs_attention"])
        self.assertEqual(result.status, "completed")
        history = [stage for stage in result.data["stages"] if stage.get("historical")]
        self.assertEqual(len(history), 2)
        self.assertTrue(all(stage["updated_at"] for stage in history))
        self.assertIn("旧失败", history[0]["reason"])

    def test_manual_or_interrupted_is_actionable_not_normal_running(self):
        from unittest.mock import patch

        from app.agent.activity_actions import timeline_snapshot

        for state in ("manual", "interrupted", "rollback_failed"):
            snapshot = {"record": {"status": state, "title": "示例"}, "steps": []}
            with patch(
                "app.agent.activity_actions.activity.snapshot", return_value=snapshot
            ):
                result = timeline_snapshot({"kind": "organize", "id": 1})
            self.assertTrue(result.data["needs_attention"])
            self.assertEqual(result.status, "attention")
