from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import patch

from app import database as db
from app import sdk
from app.agent.kernel.projection import DefaultProjector
from app.agent.models import ToolResult
from app.repositories import activity, organize_confirmations
from app.repositories import (
    agent_download_verification as agent_download_verification_repo,
)
from app.repositories import download_requests as download_requests_repo
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
        identifier, _ = download_requests_repo.create_download_request(
            key, "magnet", title=title, origin="agent"
        )
        download_requests_repo.update_download_request(
            identifier, targets="guangya", status="downloading", gy_status="downloading"
        )
        return identifier

    def record_organize_receipt(self, identifier, source_id, stats):
        task_id = f"timeline-organize-{identifier}"
        run_id = db.add_task_run("guangya_organize", "download")
        db.finish_task_run(run_id, "success", result=json.dumps({
            "task_id": task_id, "sources": [{"id": source_id, "stats": {
                **stats, "notification_context": {"download_request_ids": [identifier]},
            }}],
        }))
        download_requests_repo.update_download_request(
            identifier, organize_run_id=run_id, organize_task_id=task_id, gy_target_dir=source_id, gy_isolated=1,
        )
        return task_id

    def test_stages_use_persisted_request_links_not_equal_titles(self):
        identifier = self.make_request()
        unrelated = self.make_request("b")
        run_id = db.add_task_run("strm", "organize")
        db.finish_task_run(run_id, "failed", error="索引站点响应超时")
        download_requests_repo.update_download_request(
            identifier,
            strm_run_id=run_id,
            strm_status="failed",
            strm_error="索引站点响应超时",
        )
        download_requests_repo.update_download_request(
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
        download_requests_repo.update_download_request(
            identifier, status="completed", gy_status="completed"
        )
        value = sdk.activity.timeline("download", identifier)
        self.assertEqual(value["status"], "completed")
        self.assertTrue(value["data"]["gaps"])
        self.assertNotIn("已入库", value["message"])

    def test_resubmitted_stages_end_the_original_attempt_without_claiming_completion(self):
        identifier = self.make_request()
        successor = self.make_request("successor")
        download_requests_repo.update_download_request(identifier, status="failed", gy_status="failed",
                                   organize_status="failed", strm_status="failed")
        self.assertTrue(download_requests_repo.mark_download_request_resubmitted(
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
        download_requests_repo.update_download_request(identifier, targets="both", status="manual_review",
                                   qb_status="downloading", gy_status="failed")
        download_requests_repo.mark_download_request_resubmitted(identifier, successor_request_id=successor, targets="guangya")
        for qb_status, expected, terminal in (("downloading", "in_progress", False), ("completed", "completed", True)):
            with self.subTest(qb_status=qb_status):
                download_requests_repo.update_download_request(identifier, qb_status=qb_status)
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
        download_requests_repo.update_download_request(identifier, status="completed", gy_status="completed",
                                   organize_status="failed", organize_run_id=runs["organize"],
                                   strm_status="failed", strm_run_id=runs["strm"])
        download_requests_repo.mark_download_request_resubmitted(identifier, successor_request_id=successor, targets="guangya")
        for transferred in ({"organize", "strm"}, {"organize"}, {"strm"}):
            with self.subTest(transferred=transferred):
                download_requests_repo.update_download_request(identifier, status="resubmitted", **{
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
        download_requests_repo.update_download_request(identifier, targets="qb", status="failed", qb_status="failed",
                                   qb_task_id=qb_hash, local_import_status="failed")
        download_requests_repo.mark_download_request_resubmitted(identifier, successor_request_id=successor, targets="qb")
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
                    download_requests_repo.update_download_request(identifier, local_import_status=status)
                    result = sdk.activity.timeline("download", identifier)
                    self.assertEqual((result["status"], result["data"]["terminal"]), (expected, True))
                    linked = [stage for stage in result["data"]["stages"] if stage["stage"].startswith("关联本地任务")]
                    self.assertEqual(len(linked), min(total, 20) + int(total > 20))
                    self.assertTrue(all(bool(stage.get("historical")) == (status == "resubmitted") for stage in linked))
                    self.assertTrue(all(stage["status"] == "failed" for stage in linked if stage["stage"] == "关联本地任务"))

    def test_handoff_does_not_hide_independent_library_verification(self):
        identifier = self.make_request()
        successor = self.make_request("successor")
        download_requests_repo.update_download_request(identifier, status="failed", gy_status="failed")
        download_requests_repo.mark_download_request_resubmitted(identifier, successor_request_id=successor, targets="guangya")
        targets = [{"title": "示例剧", "tmdb_id": "123", "season": 2, "episode": 3,
                    "as_of": "2026-11-01", "library_name": "剧集"}]
        self.assertEqual(agent_download_verification_repo.enqueue_agent_download_verification(identifier, targets=targets, owner="fixture"), "queued")
        pending = sdk.activity.timeline("download", identifier)
        self.assertEqual((pending["status"], pending["data"]["terminal"]), ("in_progress", False))
        job = agent_download_verification_repo.claim_due_agent_download_verification()
        assert job is not None
        self.assertTrue(agent_download_verification_repo.update_agent_download_verification(
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
        download_requests_repo.update_download_request(
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
                "request",
                "organize_evidence",
                "destination_evidence",
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

    def test_guangya_receiving_distinguishes_routing_staging_recycle_and_finalized_parent(self):
        identifier = self.make_request()

        def guangya():
            return sdk.activity.timeline("download", identifier)["data"]["destination_evidence"]["guangya"]

        download_requests_repo.update_download_request(
            identifier, gy_status="failed", gy_requested_target_dir="receive-dir", gy_target_dir="receive-dir",
        )
        failed = guangya()
        self.assertEqual(failed["requested_directory_id"], "receive-dir")
        self.assertIsNone(failed["receiving"])
        self.assertEqual(failed["check_directory_ids"], [])

        download_requests_repo.update_download_request(identifier, status="submitting", gy_status="submitting")
        self.assertTrue(download_requests_repo.bind_download_request_guangya_staging(
            identifier, staging_id="staging-dir", parent_id="receive-dir",
            staging_name="MF-《示例》（第一季）", target_name="下载 / MF-《示例》（第一季）",
        ))
        staging = guangya()
        self.assertEqual(staging["status"], "submitting")
        self.assertEqual(staging["receiving"], {
            "kind": "staging", "state": "recorded", "directory_id": "staging-dir",
            "directory_name": "MF-《示例》（第一季）", "parent_id": "receive-dir", "parent_name": "下载",
        })
        self.assertEqual(staging["check_directory_ids"], ["staging-dir"])

        download_requests_repo.update_download_request(
            identifier, status="completed", gy_status="completed", organize_status="requires_manual",
            gy_staging_cleanup_status="retained",
        )
        pending = guangya()
        self.assertEqual(pending["organize_status"], "requires_manual")
        self.assertEqual(pending["receiving"]["state"], "retained")
        audit_id = db.add_organize_delete_audit(
            trigger="download_staging_cleanup", file_id="staging-dir", reason="测试", status="pending",
        )
        self.assertEqual(guangya()["receiving"]["state"], "unverified")
        self.assertEqual(guangya()["check_directory_ids"], [])
        db.update_organize_delete_audit(audit_id, status="success")
        self.assertEqual(guangya()["receiving"]["state"], "recycled")
        download_requests_repo.update_download_request(identifier, gy_staging_name="")
        data = sdk.activity.timeline("download", identifier)["data"]
        receiving = data["destination_evidence"]["guangya"]["receiving"]
        self.assertEqual(receiving["directory_id"], "staging-dir")
        self.assertIn("原目录名未留存", receiving["historical_path_note"])
        self.assertTrue(any("是否已入库未知" in gap for gap in data["gaps"]))
        self.assertEqual(guangya()["check_directory_ids"], [])

        download_requests_repo.update_download_request(
            identifier, organize_status="skipped", gy_isolated=0, gy_staging_cleanup_status="completed",
            gy_target_dir="receive-dir", gy_target_name="现在的默认配置名", gy_staging_name="原暂存目录",
        )
        finalized = guangya()
        self.assertEqual(finalized["receiving"], {
            "kind": "finalized", "state": "recorded", "directory_id": "receive-dir",
        })
        self.assertEqual(finalized["check_directory_ids"], ["receive-dir"])
        download_requests_repo.update_download_request(identifier, gy_target_dir="0", gy_staging_cleanup_status="")
        self.assertEqual(guangya()["receiving"]["kind"], "direct")
        self.assertEqual(guangya()["check_directory_ids"], [])

    def test_both_targets_keep_qb_observation_and_historical_guangya_separate(self):
        identifier = self.make_request()
        successor = self.make_request("successor")
        download_requests_repo.update_download_request(
            identifier, targets="both", status="manual_review", qb_status="completed", qb_task_id="a" * 40,
            qb_content_path="/private-qb-volume/《本地内容》（2025）", gy_status="failed", gy_isolated=1,
            gy_target_dir="old-staging", gy_staging_name="旧暂存",
        )
        with db.get_conn() as conn:
            conn.execute("UPDATE download_requests SET qb_save_path=? WHERE id=?", ("/private-qb-volume", identifier))
        self.assertTrue(download_requests_repo.mark_download_request_resubmitted(
            identifier, successor_request_id=successor, targets="guangya",
        ))
        evidence = sdk.activity.timeline("download", identifier)["data"]["destination_evidence"]
        self.assertEqual(evidence["qb"]["observed_content_name"], "《本地内容》（2025）")
        self.assertEqual(evidence["qb"]["task_hash"], "a" * 40)
        self.assertEqual(evidence["qb"]["save_path_policy"], "configured")
        self.assertEqual(evidence["qb"]["status"], "completed")
        self.assertFalse(evidence["qb"]["historical"])
        self.assertTrue(evidence["guangya"]["historical"])
        self.assertEqual(evidence["guangya"]["receiving"]["directory_id"], "old-staging")
        self.assertEqual(evidence["guangya"]["check_directory_ids"], [])
        self.assertNotIn("private-qb-volume", json.dumps(evidence))

    def test_destination_and_organize_receipts_filter_sensitive_values_before_sdk_projection(self):
        identifier = self.make_request()
        name = "《合法作品》（第01集）.mkv"
        fake_url = "https://private-download.invalid/watch?X-Emby-Token=unpublished-token"
        self.record_organize_receipt(identifier, "staging-29", {
            "media_items": [
                {"title": "《合法作品》（第一季）", "filename": name, "media_type": "movie/tv", "episode": 1},
                {"title": fake_url, "filename": "WEB_SECRET_KEY=unpublished-key",
                 "tmdb_id": {"nested_secret": "unpublished-struct"}, "media_type": "来源/私有分类路径"},
            ],
            "strm_changes": [
                {"file_id": "video-29", "parent_id": "archive-29", "name": name,
                 "rel_dir": "剧集/《合法作品》（2025）/Season 01", "kind": "video", "action": "upsert"},
                {"file_id": fake_url, "parent_id": "C:\\private-parent-mount", "name": fake_url,
                 "rel_dir": "/mnt/private-absolute-root/Series", "action": "upsert"},
                {"file_id": "video-30", "name": "电影%EF%BC%8Fprivate-encoded-child",
                 "rel_dir": "C:/private-drive-mount/Series", "action": "remove"},
                {"file_id": "video-31", "rel_dir": "Series/../private-traversal-name"},
            ],
        })
        download_requests_repo.update_download_request(
            identifier, gy_staging_name=fake_url, gy_staging_parent_dir="/private-root/parent",
            gy_requested_target_dir=fake_url, targets="both", qb_status="completed",
            qb_content_path="/media/X-Emby-Token=unpublished-qb-key",
        )
        native = sdk.activity.timeline("download", identifier)
        data = native["data"]
        self.assertEqual(data["organize_evidence"]["media"][0]["media_type"], "movie/tv")
        self.assertNotIn("media_type", data["organize_evidence"]["media"][1])
        self.assertEqual(data["organize_evidence"]["files"][0]["name"], name)
        self.assertEqual(data["organize_evidence"]["files"][0]["rel_dir"], "剧集/《合法作品》（2025）/Season 01")
        self.assertEqual(data["destination_evidence"]["guangya"]["receiving"], {
            "kind": "staging", "state": "recorded", "directory_id": "staging-29",
        })
        model = json.loads(DefaultProjector().project(native).model_message())["data"]
        self.assertEqual(model["destination_evidence"]["guangya"]["check_directory_ids"], ["staging-29"])
        self.assertEqual(model["organize_evidence"]["files"][0]["file_id"], "video-29")
        self.assertEqual(model["organize_evidence"]["files"][0]["parent_id"], "archive-29")
        for public in (json.dumps(data), json.dumps(model)):
            for private in ("unpublished-", "private-download.invalid", "private-parent-mount", "private-absolute-root",
                            "private-drive-mount", "private-traversal-name", "private-encoded-child", "private-root"):
                with self.subTest(private=private):
                    self.assertNotIn(private, public)

    def test_confirmed_receipt_is_scoped_to_the_bound_source_and_timeline(self):
        identifier = self.make_request()
        other = self.make_request("another")
        task_id = self.record_organize_receipt(identifier, "owned-staging", {"need_confirm": 1})
        for token, source, request, status in (
            ("owned-executed", "owned-staging", identifier, "completed"),
            ("owned-pending", "owned-staging", identifier, "pending"),
            ("other-request", "owned-staging", other, "completed"),
            ("other-source", "other-staging", identifier, "completed"),
        ):
            organize_confirmations.create_organize_confirmation(
                token=token, fingerprint=token, chat_id="unpublished-chat", source_name="测试",
                directory_path="历史来源", organize_task_id=task_id, expires_at="2090-01-01",
                payload={"source_dir_id": source, "download_request_ids": [request]},
            )
            organize_confirmations.update_organize_confirmation(token, status=status, result_json=json.dumps({
                "strm_changes": [{"file_id": token + "-video", "parent_id": "actual-archive", "action": "upsert"}],
            }))

        original = db.get_conn
        statements = []

        @contextmanager
        def traced():
            with original() as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        with patch.object(db, "get_conn", traced):
            snapshot = activity.snapshot("download", identifier)
        assert snapshot is not None
        self.assertNotIn("destination_evidence", snapshot)
        self.assertFalse(any("organize_confirmations" in sql or "organize_delete_audit" in sql for sql in statements))
        data = sdk.activity.timeline("download", identifier)["data"]
        self.assertEqual(data["organize_evidence"]["files"], [
            {"file_id": "owned-executed-video", "parent_id": "actual-archive", "action": "upsert"},
        ])
        self.assertNotIn("unpublished-chat", json.dumps(data))

    def test_download_exposes_only_its_bound_source_receipt(self):
        identifier = self.make_request()
        other = self.make_request("other")
        def source(source_id, request_ids, title):
            return {"id": source_id, "stats": {
                "scan_complete": True,
                "notification_context": {"download_request_ids": request_ids, "chat_id": "secret-chat"},
                "media_items": [{"title": title, "tmdb_id": "123", "media_type": "tv", "season": 2,
                                 "episode": 6, "filename": "renamed.S02E06.mkv", "private": "secret"}],
                "strm_changes": [{"file_id": "video-1", "parent_id": "season-2", "name": "renamed.S02E06.mkv",
                                  "kind": "video", "action": "upsert", "etag": "secret-etag"}],
            }}
        payload = {"task_id": "bound-task", "sources": [
            source("mine", [identifier], "请求对应媒体"), source("other", [other], "不属于当前请求"),
        ]}
        run_id = db.add_task_run("guangya_organize", "download")
        db.finish_task_run(run_id, "success", result=json.dumps(payload))
        download_requests_repo.update_download_request(identifier, organize_run_id=run_id, organize_task_id="bound-task",
                                   gy_target_dir="mine", gy_isolated=1, organize_status="completed")
        result = sdk.activity.timeline("download", identifier)
        evidence = result["data"]["organize_evidence"]
        self.assertEqual(evidence["status"], "recorded")
        self.assertEqual(evidence["media"][0]["episode"], 6)
        self.assertEqual(evidence["files"][0]["file_id"], "video-1")
        self.assertFalse(evidence["truncated"])
        self.assertEqual(evidence["freshness"], "persisted_snapshot")
        public = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("不属于当前请求", public)
        self.assertNotIn("secret", public)
        self.assertIn("未建立媒体库可见性复核", public)
        download_requests_repo.update_download_request(identifier, gy_isolated=0)
        shared = sdk.activity.timeline("download", identifier)["data"]["organize_evidence"]
        self.assertEqual(shared["status"], "unavailable")
        self.assertEqual(shared["files"], [])
        download_requests_repo.update_download_request(identifier, gy_isolated=1)

        for invalid in (
            {**payload, "task_id": "old-task"},
            {"task_id": "bound-task", "sources": [source("mine", [identifier, other], "共享来源")]},
            {"task_id": "bound-task", "sources": [source("mine", [], "旧数据未记录归属")]},
            {"task_id": "bound-task", "sources": [source("other", [identifier], "目录已改变")]},
        ):
            with self.subTest(payload=invalid):
                db.finish_task_run(run_id, "success", result=json.dumps(invalid))
                missing = sdk.activity.timeline("download", identifier)["data"]["organize_evidence"]
                self.assertEqual(missing["status"], "unavailable")
                self.assertEqual(missing["media"], [])

        stats = payload["sources"][0]["stats"]
        stats["media_items"] *= 51
        db.finish_task_run(run_id, "success", result=json.dumps(payload))
        download_requests_repo.update_download_request(identifier, organize_status="resubmitted")
        bounded = sdk.activity.timeline("download", identifier)["data"]["organize_evidence"]
        self.assertEqual((len(bounded["media"]), bounded["media_count"]), (50, 51))
        self.assertTrue(bounded["truncated"])
        self.assertTrue(bounded["historical"])

    def test_pack_timeline_keeps_each_target_result_after_restart(self):
        identifier = self.make_request()
        targets = [{"title": "示例剧", "tmdb_id": "123", "season": 2, "episode": episode,
                    "as_of": "2026-11-01", "library_name": "剧集"} for episode in (3, 4)]
        self.assertEqual(agent_download_verification_repo.enqueue_agent_download_verification(identifier, targets=targets, owner="a"), "queued")
        job = agent_download_verification_repo.claim_due_agent_download_verification()
        assert job is not None
        outcomes = [{**target, "result": "visible" if target["episode"] == 3 else "missing"} for target in job["targets"]]
        self.assertTrue(agent_download_verification_repo.update_agent_download_verification(
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
