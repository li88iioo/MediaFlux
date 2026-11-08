from __future__ import annotations

import json
from unittest.mock import patch
from typing import Any, cast

from app import database as db
from app import sdk
from app.agent.models import ToolResult
from app.repositories import activity
from app.repositories.activity import timeline_snapshot
from tests.support import IsolatedDatabaseTestCase


class ActivityClosureTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_download_verification_notification_outbox")
            conn.execute("DELETE FROM agent_download_verifications")
            conn.execute("DELETE FROM download_request_keys")
            conn.execute("DELETE FROM download_log")
            conn.execute("DELETE FROM download_requests")
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_media_sources")
            conn.execute("DELETE FROM task_runs")

    def make_request(self, key="a", title="示例剧"):
        identifier, _ = db.create_download_request(
            key, "magnet", title=title, origin="agent"
        )
        db.update_download_request(
            identifier, targets="guangya", status="downloading", gy_status="downloading"
        )
        return identifier

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
        value = sdk.activity.timeline("download", identifier)
        self.assertEqual(value["status"], "attention")
        public = json.dumps(value, ensure_ascii=False)
        self.assertIn("索引站点响应超时", public)
        self.assertNotIn("不属于当前请求", public)
        self.assertIn("未建立媒体库可见性复核", public)
        self.assertEqual(value["data"]["freshness"], "persisted_snapshot")

    def test_no_in_library_claim_when_only_download_completed(self):
        identifier = self.make_request()
        db.update_download_request(
            identifier, status="completed", gy_status="completed"
        )
        value = sdk.activity.timeline("download", identifier)
        self.assertEqual(value["status"], "completed")
        self.assertTrue(value["data"]["gaps"])
        self.assertNotIn("已入库", value["message"])

    def test_resubmitted_stages_end_the_original_attempt_without_claiming_completion(self):
        identifier = self.make_request()
        successor = self.make_request("successor")
        db.update_download_request(identifier, status="failed", gy_status="failed",
                                   organize_status="failed", strm_status="failed")
        self.assertTrue(db.mark_download_request_resubmitted(
            identifier, successor_request_id=successor, targets="guangya"
        ))
        result = sdk.activity.timeline("download", identifier)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["data"]["terminal"])
        self.assertFalse(result["data"]["needs_attention"])
        self.assertEqual({stage["status"] for stage in result["data"]["stages"]}, {"resubmitted"})
        self.assertIn("移交", result["message"])
        self.assertIn("不代表实际下载或入库完成", result["message"])
        self.assertIn("未建立媒体库可见性复核", "；".join(result["data"]["gaps"]))
        self.assertFalse(sdk.activity.timeline("download", successor)["data"]["terminal"])

    def test_resubmitted_request_waits_for_the_backend_that_was_not_transferred(self):
        identifier = self.make_request()
        successor = self.make_request("successor")
        db.update_download_request(identifier, targets="both", status="manual_review",
                                   qb_status="downloading", gy_status="failed")
        db.mark_download_request_resubmitted(identifier, successor_request_id=successor, targets="guangya")
        for qb_status, expected, terminal in (("downloading", "in_progress", False), ("completed", "completed", True)):
            with self.subTest(qb_status=qb_status):
                db.update_download_request(identifier, qb_status=qb_status)
                result = sdk.activity.timeline("download", identifier)
                self.assertEqual((result["status"], result["data"]["terminal"]), (expected, terminal))
                self.assertFalse(result["data"]["needs_attention"])
                self.assertEqual([stage["status"] for stage in result["data"]["stages"]],
                                 ["resubmitted", qb_status, "resubmitted"])

    def test_linked_runs_are_historical_only_for_the_transferred_postprocess(self):
        identifier = self.make_request()
        successor = self.make_request("successor")
        runs = {prefix: db.add_task_run(prefix, "resubmit-fixture") for prefix in ("organize", "strm")}
        for prefix, run_id in runs.items():
            db.finish_task_run(run_id, "failed", error=f"{prefix} 原运行失败")
        db.update_download_request(identifier, status="completed", gy_status="completed",
                                   organize_status="failed", organize_run_id=runs["organize"],
                                   strm_status="failed", strm_run_id=runs["strm"])
        db.mark_download_request_resubmitted(identifier, successor_request_id=successor, targets="guangya")
        for transferred in ({"organize", "strm"}, {"organize"}, {"strm"}):
            with self.subTest(transferred=transferred):
                db.update_download_request(identifier, status="resubmitted", **{
                    prefix + "_status": "resubmitted" if prefix in transferred else "failed"
                    for prefix in runs
                })
                result = sdk.activity.timeline("download", identifier)
                expected = "completed" if len(transferred) == 2 else "attention"
                self.assertEqual((result["status"], result["data"]["terminal"]), (expected, True))
                stages = {stage["stage"]: stage for stage in result["data"]["stages"]}
                for prefix in runs:
                    stage = stages["关联运行 · " + prefix]
                    self.assertEqual(stage["status"], "failed")
                    self.assertEqual(bool(stage.get("historical")), prefix in transferred)
                    self.assertIn("原运行失败", stage["reason"])

    def test_linked_local_failures_and_overflow_follow_only_local_import_handoff(self):
        identifier = self.make_request()
        successor = self.make_request("successor")
        qb_hash = "a" * 40
        db.update_download_request(identifier, targets="qb", status="failed", qb_status="failed",
                                   qb_task_id=qb_hash, local_import_status="failed")
        db.mark_download_request_resubmitted(identifier, successor_request_id=successor, targets="qb")
        previous = 0
        for total in (1, 21):
            with db.get_conn() as conn:
                for index in range(previous, total):
                    source = conn.execute(
                        "INSERT INTO local_media_sources(name,local_root,created_at,updated_at) VALUES(?,?,?,?)",
                        (f"handoff-{index}", f"/fixture/{index}", db.now(), db.now()),
                    ).lastrowid
                    conn.execute(
                        "INSERT INTO local_media_tasks(source_id,qb_hash,content_path,operation_token,title,status,error,created_at,updated_at) "
                        "VALUES(?,?,'/fixture',?,'旧本地任务','failed','原本地任务失败',?,?)",
                        (source, qb_hash, f"handoff-{index}", db.now(), db.now()),
                    )
            previous = total
            for status, expected in (("resubmitted", "completed"), ("failed", "attention")):
                with self.subTest(total=total, local_import_status=status):
                    db.update_download_request(identifier, local_import_status=status)
                    result = sdk.activity.timeline("download", identifier)
                    self.assertEqual((result["status"], result["data"]["terminal"]), (expected, True))
                    linked = [stage for stage in result["data"]["stages"] if stage["stage"].startswith("关联本地任务")]
                    self.assertEqual(len(linked), min(total, 20) + int(total > 20))
                    self.assertTrue(all(bool(stage.get("historical")) == (status == "resubmitted") for stage in linked))
                    self.assertTrue(all(stage["status"] == "failed" for stage in linked if stage["stage"] == "关联本地任务"))

    def test_handoff_does_not_hide_independent_library_verification(self):
        identifier = self.make_request()
        successor = self.make_request("successor")
        db.update_download_request(identifier, status="failed", gy_status="failed")
        db.mark_download_request_resubmitted(identifier, successor_request_id=successor, targets="guangya")
        targets = [{"title": "示例剧", "tmdb_id": "123", "season": 2, "episode": 3,
                    "as_of": "2026-11-01", "library_name": "剧集"}]
        self.assertEqual(db.enqueue_agent_download_verification(identifier, targets=targets, owner="fixture"), "queued")
        pending = sdk.activity.timeline("download", identifier)
        self.assertEqual((pending["status"], pending["data"]["terminal"]), ("in_progress", False))
        job = db.claim_due_agent_download_verification()
        assert job is not None
        self.assertTrue(db.update_agent_download_verification(
            identifier, status="attention", result="missing", attempts=3, next_check_at=db.now(),
            expected_lease_generation=job["lease_generation"], targets=[{**targets[0], "result": "missing"}],
        ))
        result = sdk.activity.timeline("download", identifier)
        self.assertEqual((result["status"], result["data"]["terminal"]), ("attention", True))
        verification = next(stage for stage in result["data"]["stages"] if stage["stage"] == "媒体库可见性复核")
        self.assertTrue(verification["needs_attention"])
        self.assertFalse(verification.get("historical", False))

    def test_deleted_request_is_explicit_not_found(self):
        value = sdk.activity.timeline("download", 999999)
        self.assertFalse(value["ok"])
        self.assertEqual(value["status"], "not_found")

    def test_native_search_retains_ids_and_matches_literal_wildcards(self):
        self.make_request(title="100%示例")
        self.make_request("b", "另一部")
        result = sdk.activity.search("%")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["kind"], "download")
        self.assertGreater(result["items"][0]["id"], 0)

    def test_errors_do_not_publish_paths_or_tokens(self):
        identifier = self.make_request()
        db.update_download_request(
            identifier,
            status="failed",
            gy_status="failed",
            error="token=secret /volume/private",
        )
        result = sdk.activity.timeline("download", identifier)
        public = json.dumps(result)
        self.assertNotIn("secret", public)
        self.assertNotIn("/volume", public)
        self.assertTrue(result["data"]["needs_attention"])

    def test_shared_timeline_exports_safe_facts_and_ids_without_raw_request(self):
        identifier = self.make_request()
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE download_requests SET source_value=?,torrent_data=?,chat_id=?,"
                "user_id=?,message_id=?,qb_task_id=?,gy_task_id=?,qb_content_path=?,"
                "error=? WHERE id=?",
                (
                    "https://source.invalid/file?token=source-secret",
                    b"private-torrent-payload",
                    "private-chat",
                    "private-user",
                    "private-message",
                    "private-qb-id",
                    "private-gy-id",
                    "/volume/private-media",
                    "token=error-secret /volume/private-media",
                    identifier,
                ),
            )
        stored = activity.snapshot("download", identifier)
        assert stored is not None
        self.assertEqual(stored["record"]["torrent_data"], b"private-torrent-payload")
        result = timeline_snapshot("download", identifier)
        public = json.dumps(result, ensure_ascii=False, allow_nan=False)
        for private in (
            "source-secret",
            "private-torrent",
            "private-chat",
            "private-user",
            "private-message",
            "private-qb-id",
            "private-gy-id",
            "/volume",
            "error-secret",
        ):
            with self.subTest(private=private):
                self.assertNotIn(private, public)
        self.assertEqual(set(result), {"ok", "status", "message", "data", "error"})
        self.assertEqual(
            set(result["data"]),
            {
                "title",
                "kind",
                "stages",
                "explanation",
                "gaps",
                "terminal",
                "needs_attention",
                "freshness",
                "observed_at",
                "activities",
            },
        )
        self.assertEqual(
            result["data"]["activities"], [{"kind": "download", "id": identifier}]
        )
        native = sdk.activity.timeline("download", identifier)
        self.assertEqual(native["data"]["activities"], result["data"]["activities"])

    def test_pack_timeline_keeps_each_target_result_after_restart(self):
        identifier = self.make_request()
        targets = [{"title": "示例剧", "tmdb_id": "123", "season": 2, "episode": episode,
                    "as_of": "2026-11-01", "library_name": "剧集"} for episode in (3, 4)]
        self.assertEqual(db.enqueue_agent_download_verification(identifier, targets=targets, owner="a"), "queued")
        job = db.claim_due_agent_download_verification()
        assert job is not None
        outcomes = [{**target, "result": "visible" if target["episode"] == 3 else "missing"} for target in job["targets"]]
        self.assertTrue(db.update_agent_download_verification(
            identifier, status="attention", result="missing", attempts=3, next_check_at=db.now(),
            expected_lease_generation=job["lease_generation"], targets=outcomes,
        ))
        db.init_db()
        result = timeline_snapshot("download", identifier)
        stage = next(stage for stage in result["data"]["stages"] if stage["stage"] == "媒体库可见性复核")
        self.assertEqual(stage["status"], "attention")
        self.assertEqual(stage["targets"], [{
            "tmdb_id": "123", "season": 2, "episode": target["episode"],
            "library_name": "剧集", "result": target["result"],
        } for target in outcomes])
        self.assertNotIn("owner", stage)

    def test_shared_timeline_distinguishes_missing_from_invalid_identity(self):
        missing = timeline_snapshot("download", 999999)
        self.assertEqual(
            (missing["ok"], missing["status"], missing["data"]),
            (False, "not_found", {}),
        )
        with patch.object(activity, "snapshot") as read:
            kind: Any
            identifier: Any
            for kind, identifier in (
                ("unknown", 1),
                ([], 1),
                ("download", True),
                ("download", "1"),
                ("download", 0),
            ):
                with (
                    self.subTest(kind=kind, identifier=identifier),
                    self.assertRaises(ValueError),
                ):
                    timeline_snapshot(cast(Any, kind), cast(Any, identifier))
        read.assert_not_called()

    def test_native_url_receipt_retains_request_for_activity_and_recent_followup(self):
        from app.agent.domains.downloads.recent_submissions import RecentDownloadSubmissionStore
        from app.agent.domains.library.missing_workflow_runtime import MissingMediaWorkflowRuntime

        store = RecentDownloadSubmissionStore()
        runtime = MissingMediaWorkflowRuntime(recent_download_store=store)
        for outer, status in (("accepted", "submitted"), ("partial", "partial"), ("outcome_unknown", "manual_review")):
            with self.subTest(status=status):
                result = ToolResult(outer != "outcome_unknown", outer, "请求回执", data={
                    "request_id": 5, "source_type": "direct_url", "target": "qb", "status": status,
                    "succeeded": ["qb"] if outer != "outcome_unknown" else [], "failed": [], "created": True,
                })
                self.assertEqual(result.data["request_id"], 5)
                runtime.complete_submission(owner="url-owner", tool_name="downloads.submit", result=result, followups=())
                self.assertEqual(store.get(owner="url-owner")[0].request_id, 5)
                self.assertEqual(store.get(owner="url-owner")[0].dispatch_status, status)
                self.assertFalse(store.get(owner="another-owner"))

    def test_native_validation_rejects_bool_limits_and_invalid_identity(self):
        with self.assertRaises(ValueError):
            sdk.activity.search(limit=True)
        with self.assertRaises(ValueError):
            sdk.METHODS["activity.timeline"]("download", "123")

    def test_has_more_does_not_claim_all_records(self):
        self.make_request()
        self.make_request("b")
        result = sdk.activity.search(limit=1)
        self.assertTrue(result["has_more"])
        self.assertEqual(len(result["items"]), 1)

    def test_resolved_historical_failure_is_not_current_attention(self):
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
        with patch("app.repositories.activity.snapshot", return_value=snapshot):
            result = timeline_snapshot("organize", 1)
        self.assertFalse(result["data"]["needs_attention"])
        self.assertEqual(result["status"], "completed")
        history = [
            stage for stage in result["data"]["stages"] if stage.get("historical")
        ]
        self.assertEqual(len(history), 2)
        self.assertTrue(all(stage["updated_at"] for stage in history))
        self.assertIn("旧失败", history[0]["reason"])

    def test_manual_or_interrupted_is_actionable_not_normal_running(self):
        for state in ("manual", "interrupted", "rollback_failed"):
            snapshot = {"record": {"status": state, "title": "示例"}, "steps": []}
            with patch("app.repositories.activity.snapshot", return_value=snapshot):
                result = timeline_snapshot("organize", 1)
            self.assertTrue(result["data"]["needs_attention"])
            self.assertEqual(result["status"], "attention")
