"""多入口逐项批量确认的冻结范围、只读复核和一次性认领行为。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.modules import organize_confirmation_batches as batches
from app.modules import organize_confirmations as confirmations
from app.modules import telegram_organize_lifecycle  # noqa: F401 -- 在测试替换通知函数前加载其正式导入绑定。
from app.modules.agent_recognition_review import RecognitionReviewDecision
from app.modules.organize import OrganizeRules, organize_rules_snapshot
from tests.support import IsolatedDatabaseTestCase


class ConfirmationBatchTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_confirmations")
        self.rules = OrganizeRules(target_dir_id="target")
        self.enterContext(patch.object(OrganizeRules, "from_config", return_value=self.rules))
        self.enterContext(patch.object(OrganizeRules, "for_source", return_value=self.rules))
        self.enterContext(patch.object(confirmations, "wake_confirmation_dispatcher"))
        self.enterContext(patch.object(confirmations, "wake_recognition_review_dispatcher"))

    def row(self, token: str):
        row = db.get_organize_confirmation(token)
        assert row is not None
        return row

    def create(self, token: str, *, batch="", chat="chat", review=False):
        payload: dict = {
            "identity": f"作品{token}", "directory": f"/incoming/{token}",
            "reason": "同一番号存在相互冲突的元数据", "source_name": "来源A",
            "source_dir_id": "source", "source_parent_id": "parent",
            "files": [{"file_id": token, "name": f"{token}.mkv", "parent_id": "parent", "size": 100000000}],
            "candidates": [{"title": f"候选{i}-{token}", "year": str(2020 + i),
                            "tmdb_id": str(100 + i), "provider": "tmdb", "media_type": "movie"}
                           for i in range(2)],
            "rules": organize_rules_snapshot(self.rules),
        }
        if batch:
            payload.update(_confirmation_batch=batch, review_only=True)
        db.create_organize_confirmation(
            token=token, fingerprint=confirmations._fingerprint(payload), chat_id=chat,
            source_name="来源A", directory_path=payload["directory"], payload=payload,
            expires_at=(datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
            organize_task_id="task1", review_requested=review, review_ready=review,
        )
        return payload

    def test_cross_page_selection_is_frozen_and_two_works_use_distinct_choices(self):
        for index in range(25):
            self.create(str(index))
        page = batches.list_confirmations(page_size=20)
        self.assertEqual((page["total"], len(page["items"]), len(page["confirmation_ids"])), (25, 20, 25))
        frozen = page["confirmation_ids"]
        self.create("new")
        self.assertEqual(batches.list_confirmations(confirmation_ids=frozen)["total"], 25)
        entries = [{"confirmation_id": "0", "candidate_index": 0}, {"confirmation_id": "24", "candidate_index": 1}]
        preview = batches.preview_choices(entries)
        self.assertNotEqual(preview["items"][0]["candidate"]["title"], preview["items"][1]["candidate"]["title"])
        self.assertEqual(self.row("0")["status"], "pending")
        result = batches.submit_choices(entries, preview["digest"])
        self.assertEqual((result["accepted"], result["failed"]), (2, 0))
        self.assertEqual(self.row("24")["selected_index"], 1)
        queued_at = self.row("0")["queued_at"]
        self.assertEqual(batches.submit_choices(entries, preview["digest"])["accepted"], 2)
        self.assertEqual(self.row("0")["queued_at"], queued_at)

    def test_manual_draft_survives_late_model_result_without_executing(self):
        self.create("one", review=True)
        db.claim_next_organize_confirmation_review()
        batches.save_selection("one", 1)
        self.assertFalse(db.complete_organize_confirmation_review(
            "one", status="approved", result={"candidate_index": 0},
        ))
        view = batches.list_confirmations()["items"][0]
        self.assertEqual(view["status"], "pending")
        self.assertEqual(view["suggestion"]["candidate_index"], 1)
        self.assertEqual(view["suggestion"]["source"], "human")

    def test_selection_limit_does_not_truncate_total_or_pages(self):
        self.create("seed")
        # 复制持久记录以验证真实分页；避免为同一场景重复1000次建票事务。
        row = dict(self.row("seed"))
        row.pop("id")
        columns = list(row)
        with db.get_conn() as conn:
            conn.executemany(
                f"INSERT INTO organize_confirmations ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                [tuple({**row, "token": f"item-{index}", "fingerprint": f"fp-{index}"}[key]
                       for key in columns) for index in range(1000)],
            )
        result = batches.list_confirmations(page=11, page_size=100)
        self.assertEqual((result["total"], result["pages"], result["counts"]), (1001, 11, {"pending": 1001}))
        self.assertEqual([item["confirmation_id"] for item in result["items"]], ["seed"])
        self.assertEqual(len(result["confirmation_ids"]), 1000)
        self.assertTrue(result["selection_truncated"])
        self.assertEqual(batches.list_confirmations(page=12, page_size=100)["items"], [])

    def test_expired_projection_matches_filters_and_counts(self):
        self.create("one")
        self.create("two")
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET expires_at='2000-01-01' WHERE token='one'")
        self.assertEqual(batches.list_confirmations(status="all")["counts"], {"pending": 1, "expired": 1})
        self.assertEqual([item["confirmation_id"] for item in batches.list_confirmations(status="expired")["items"]], ["one"])

    def test_rescan_keeps_superseded_group_visible_in_original_batch_progress(self):
        payload = self.create("old", batch="old-batch")
        self.create("other", batch="old-batch")
        payload["_confirmation_batch"] = "new-batch"
        db.create_organize_confirmation(
            token="new", fingerprint=confirmations._fingerprint(payload), chat_id="chat",
            source_name="来源A", directory_path=payload["directory"], payload=payload,
            expires_at=self.row("old")["expires_at"],
        )
        old_batch = batches.list_confirmations(batch_id="old-batch", status="all")
        self.assertEqual(old_batch["counts"], {"expired": 1, "pending": 1})
        self.assertEqual(old_batch["total"], 2)
        self.assertEqual(batches.list_confirmations(status="all")["total"], 2)

    def test_retry_choice_is_rejected_before_saving_an_unexecutable_suggestion(self):
        payload = self.create("retry")
        payload["_retry_selected_index"] = 0
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='retry'", (json.dumps(payload),))
        with self.assertRaisesRegex(ValueError, "绑定其他候选"):
            batches.save_selection("retry", 1)
        self.assertIsNone(batches.list_confirmations()["items"][0]["suggestion"])
        batches.save_selection("retry", 0)
        self.assertEqual(batches.list_confirmations()["items"][0]["suggestion"]["candidate_index"], 0)

    def test_bulk_review_records_suggestion_but_does_not_claim_files(self):
        self.create("one", review=True, batch="b")
        row = db.claim_next_organize_confirmation_review()
        decision = RecognitionReviewDecision(status="approved", candidate_index=1, confidence=.99,
                                             reason_code="evidence_consistent", summary="核对年份一致")
        with patch.object(confirmations, "_recognition_review_is_enabled", return_value=True), \
                patch("app.modules.agent_recognition_review.review_confirmation_payload", return_value=decision), \
                patch("app.modules.telegram_confirmation_batch.publish_batch_summary"), \
                patch.object(confirmations, "start_confirmation") as start:
            self.assertEqual(confirmations._process_recognition_review_row(row), "approved")
        start.assert_not_called()
        self.assertEqual(self.row("one")["status"], "pending")
        self.assertEqual(batches.list_confirmations()["items"][0]["suggestion"]["candidate_index"], 1)

    def test_read_only_request_revokes_automatic_claim_even_if_worker_already_read_payload(self):
        self.create("one", review=True)
        row = db.claim_next_organize_confirmation_review()
        self.assertIsNotNone(row)
        with patch.object(confirmations, "_recognition_review_is_enabled", return_value=True):
            result = batches.request_reviews(["one"])
        self.assertEqual(result["unchanged"], 1)
        with self.assertRaises(ValueError):
            db.claim_organize_confirmation("one", chat_id="chat", selected_index=0, actor="agent")
        self.assertEqual(self.row("one")["status"], "pending")
        preview = batches.preview_choices([{"confirmation_id": "one", "candidate_index": 1}])
        result = batches.submit_choices(preview["entries"], preview["digest"], actor="agent")
        self.assertEqual(result["accepted"], 1)
        row = self.row("one")
        self.assertEqual(row["confirmation_actor"], "agent")
        self.assertTrue(json.loads(row["payload_json"])["_explicit_approval"])

    def test_wrong_chat_changed_candidate_and_duplicate_group_are_rejected(self):
        self.create("one")
        entries = [{"confirmation_id": "one", "candidate_index": 0}]
        with self.assertRaises(ValueError):
            batches.preview_choices(entries, chat_id="other")
        self.assertEqual(batches.list_confirmations(chat_id="other")["items"], [])
        with self.assertRaises(ValueError):
            batches.preview_choices(entries * 2)
        preview = batches.preview_choices(entries)
        with self.assertRaisesRegex(ValueError, "预览已变化"):
            batches.submit_choices([{"confirmation_id": "one", "candidate_index": 1}], preview["digest"])
        row = self.row("one")
        payload = json.loads(row["payload_json"])
        payload["files"][0]["size"] += 1
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE token='one'", (json.dumps(payload),))
        with self.assertRaisesRegex(ValueError, "预览已变化"):
            batches.submit_choices(entries, preview["digest"])

    def test_expiry_and_rules_changes_reject_preview_without_cloud_writes(self):
        self.create("one")
        entries = [{"confirmation_id": "one", "candidate_index": 0}]
        self.rules.target_dir_id = "another-target"
        with self.assertRaisesRegex(ValueError, "整理规则已变化"):
            batches.preview_choices(entries)
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET expires_at='2000-01-01' WHERE token='one'")
        self.assertEqual(batches.list_confirmations()["items"], [])
        with self.assertRaisesRegex(ValueError, "过期"):
            batches.preview_choices(entries)

    def test_queue_rejection_does_not_replay_accepted_items(self):
        self.create("one")
        self.create("two")
        entries = [{"confirmation_id": token, "candidate_index": 0} for token in ("one", "two")]
        preview = batches.preview_choices(entries)
        original = confirmations.start_confirmation
        def racing_start(token, *args, **kwargs):
            if token == "two":
                raise ValueError("人工已选择其他候选")
            return original(token, *args, **kwargs)
        with patch.object(confirmations, "start_confirmation", side_effect=racing_start):
            result = batches.submit_choices(entries, preview["digest"])
        self.assertEqual((result["accepted"], result["failed"]), (1, 1))
        self.assertEqual(self.row("one")["status"], "queued")
        self.assertEqual(self.row("two")["status"], "pending")

    def test_notification_and_review_mode_do_not_change_file_identity(self):
        payload = self.create("one")
        fingerprint = confirmations._fingerprint(payload)
        payload.update(_confirmation_batch="b", review_only=True, _explicit_approval=True)
        self.assertEqual(confirmations._fingerprint(payload), fingerprint)

    def test_api_roundtrip_uses_saved_selection_and_same_durable_queue(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routes import logs_api
        app = FastAPI()
        app.include_router(logs_api.router)
        self.create("one")
        self.create("unselected")
        with patch.object(logs_api, "require_api_login"), TestClient(app) as client:
            response = client.post("/api/logs/organize/confirmations/selection", json={"confirmation_id": "one", "candidate_index": 1})
            self.assertEqual(response.status_code, 200)
            view = client.post("/api/logs/organize/confirmations/query", json={"confirmation_ids": ["one"]}).json()
            self.assertEqual(view["confirmation_ids"], ["one"])
            self.assertEqual([item["confirmation_id"] for item in view["items"]], ["one"])
            self.assertEqual(client.get("/api/logs/organize/confirmations",
                                        params={"confirmation_ids": "one", "status": "all"}).json(), view)
            self.assertEqual(view["items"][0]["suggestion"]["candidate_index"], 1)
            entries = [{"confirmation_id": "one", "candidate_index": 1}]
            preview = client.post("/api/logs/organize/confirmations/preview", json={"entries": entries})
            self.assertEqual(preview.status_code, 200)
            accepted = client.post("/api/logs/organize/confirmations/submit", json={"entries": entries, "digest": preview.json()["digest"]})
            self.assertEqual(accepted.json()["accepted"], 1)
            self.assertEqual(client.post("/api/logs/organize/confirmations/query", json={"confirmation_ids": ["one"]}).json()["items"][0]["status"], "queued")
            invalid = client.post("/api/logs/organize/confirmations/review", json={"confirmation_ids": "one"})
            self.assertEqual(invalid.status_code, 400)

    def test_agent_and_notifications_off_still_persist_web_candidates(self):
        payload = self.create("fixture")
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_confirmations")
        group = {**payload, "files": payload["files"]}
        with patch.object(confirmations, "_recognition_review_is_enabled", return_value=False), \
                patch.object(confirmations.Organizer, "_validated_task_confirmation_groups", return_value=([group], 1)):
            self.assertEqual(confirmations.schedule_guangya_recognition_reviews({"task_id": "silent"}, self.rules), 1)
        view = batches.list_confirmations(task_id="silent")
        self.assertEqual(view["total"], 1)
        self.assertFalse(view["items"][0]["review_status"])

    def test_batch_downstream_remains_in_same_records(self):
        self.create("one", batch="batch")
        with patch("app.modules.telegram_confirmation_batch.publish_batch_summary", return_value=True) as publish:
            self.assertTrue(confirmations.update_confirmation_lifecycle_downstream(
                "one", chat_id="chat", strm_status="完成", media_refresh="已排队",
            ))
        publish.assert_called_once_with("batch", chat_id="chat")
        self.assertEqual(batches.list_confirmations()["items"][0]["downstream"]["strm"], "完成")

    def test_sdk_and_web_read_execution_receipt_without_requerying_cloud(self):
        from app import sdk
        from app.routes import logs_api
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        self.create("done", batch="batch")
        receipt = {
            "total": 3, "moved": 1, "renamed": 1, "skipped": 1, "failed": 1,
            "scan_complete": True, "warnings": ["刮削失败 api_key=private-live-key"],
            "scan_errors": ["扫描失败 access_token=private-scan-key", "目录暂不可读"],
            "media_items": [{"title": "作品", "category": "电影/作品 (2026)",
                             "filename": "作品.2026.mp4", "size": 100000000,
                             "poster_path": "unneeded-poster"}],
            "_downstream": {"strm": "已排队", "media_refresh": "等待 STRM 完成"},
            "internal_context": "must-not-export",
        }
        db.update_organize_confirmation("done", status="completed", result_json=json.dumps(receipt),
                                       completed_at="2026-11-14 10:00:00")
        log_id = db.add_organize_log("guangya", "/source", "/archive", "done", "success",
                                     operation_token="recognition-confirm:done")
        app = FastAPI()
        app.include_router(logs_api.router)
        with patch("socket.socket.connect", side_effect=AssertionError("查询回执不应访问外部服务")), \
                patch.object(logs_api, "require_api_login"), TestClient(app) as client:
            sdk_result = sdk.activity.organize_confirmations(confirmation_ids=["done"], status="all")
            response = client.post("/api/logs/organize/confirmations/query",
                                   json={"confirmation_ids": ["done"], "status": "all"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), sdk_result)
        item = sdk_result["items"][0]
        self.assertEqual(item["status"], "completed")  # 队列结束，但实际执行仍有失败。
        self.assertEqual(item["execution"]["failed"], 1)
        self.assertEqual(item["execution"]["moved"], 1)
        self.assertEqual(item["execution"]["warnings"], ["刮削失败 api_key=********"])
        self.assertEqual(item["execution"]["scan_errors"], ["扫描失败 access_token=********", "目录暂不可读"])
        self.assertEqual(item["execution"]["media_items"], [{
            "title": "作品", "category": "电影/作品 (2026)", "filename": "作品.2026.mp4", "size": 100000000,
        }])
        self.assertEqual(item["completed_at"], "2026-11-14 10:00:00")
        self.assertEqual(item["log_ids"], [log_id])
        self.assertEqual(item["downstream"], receipt["_downstream"])
        self.assertNotIn("internal_context", item["execution"])
        self.assertNotIn("private-live-key", json.dumps(item))
        self.assertNotIn("private-scan-key", json.dumps(item))

    def test_missing_execution_receipt_stays_unknown_instead_of_zero_success(self):
        for token, status, result in (("queued", "queued", {}), ("legacy", "completed", {}),
                                      ("cancelled", "cancelled", {"resolution": "cancelled"})):
            self.create(token)
            db.update_organize_confirmation(token, status=status, result_json=json.dumps(result))
        items = {item["confirmation_id"]: item for item in batches.list_confirmations(status="all")["items"]}
        self.assertIsNone(items["queued"]["execution"])
        self.assertIsNone(items["legacy"]["execution"])
        self.assertEqual(items["cancelled"]["execution"], {"resolution": "cancelled"})
        self.assertEqual(items["legacy"]["downstream"], {})

    def test_exact_log_links_survive_same_names_and_receipt_survives_log_clear(self):
        for token in ("first", "second"):
            self.create(token)
            db.update_organize_confirmation(token, status="completed", result_json=json.dumps({
                "total": 1, "moved": 1, "failed": 0,
            }))
        ids = [db.add_organize_log("guangya", "/same", "/same-target", "same-file", "success",
                                   operation_token=f"recognition-confirm:{token}")
               for token in ("first", "second", "unrelated")]
        items = {item["confirmation_id"]: item for item in batches.list_confirmations(status="all")["items"]}
        self.assertEqual(items["first"]["log_ids"], ids[:1])
        self.assertEqual(items["second"]["log_ids"], ids[1:2])
        db.clear_organize_logs(origin="guangya", status="success")
        after = {item["confirmation_id"]: item for item in batches.list_confirmations(status="all")["items"]}
        for token in items:
            self.assertEqual(after[token]["log_ids"], [])
            self.assertEqual(after[token]["execution"], items[token]["execution"])

    def test_background_probe_resolves_batch_thread_without_losing_per_item_identity(self):
        from app.modules.organize_probe_notifications import build_notification_context, publish_probe_scope
        from app.notifier import NotificationEvent
        self.create("one", batch="batch")
        context = build_notification_context(confirmation_token="one", chat_id="chat")
        self.assertEqual(context["notification_threads"][0]["token"], "one")
        event = NotificationEvent("⚠️ 整理待确认批次", state="confirmation_batch")
        with patch("app.modules.telegram_notification_center.get_notification_thread_snapshot", return_value=SimpleNamespace(event=event)) as lookup, \
                patch("app.modules.telegram_confirmation_batch.publish_batch_summary", return_value=True):
            self.assertTrue(publish_probe_scope({"context": context}, strm_status="完成", media_refresh="已排队"))
        self.assertEqual(lookup.call_args.args[0], "confirmation-batch:batch")
        self.assertEqual(batches.list_confirmations()["items"][0]["downstream"]["media_refresh"], "已排队")

    def test_episode_mapping_is_visible_and_bound_to_approval(self):
        from app.modules.episode_research_service import POLICY_VERSION, proposal_receipt
        from app.repositories.episode_research import put_episode_research_cache
        self.create("one")
        row = self.row("one")
        payload = json.loads(row["payload_json"])
        payload["candidates"][0]["media_type"] = "tv"
        payload["files"][0].update(season=1, episode=19)
        mapping = {"file_index": 0, "source_season": 1, "source_episode": 19,
                   "target_season": 2, "target_episode": 6}
        proposal = {"version": POLICY_VERSION, "status": "verified", "case_key": "a" * 64,
                    "candidate_index": 0, "tmdb_id": "100", "group_id": "b" * 24,
                    "group_fingerprint": "c" * 64, "mappings": [mapping]}
        receipt = proposal_receipt(proposal)
        audit = {"entry_mode": "episode_research", "candidate_index": 0, "episode_research_receipt": receipt}
        put_episode_research_cache("a" * 64, {"proposal": proposal}, status="verified", ttl_seconds=100, policy_version=POLICY_VERSION)
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=?,review_result_json=? WHERE token='one'",
                         (json.dumps(payload), json.dumps(audit)))
        entries = [{"confirmation_id": "one", "candidate_index": 0}]
        preview = batches.preview_choices(entries)
        self.assertEqual(preview["items"][0]["episode_mappings"], [mapping])
        self.assertIn("Season 2", preview["items"][0]["target"])
        # 即使仍是同一个候选作品，研究季集映射改变也必须使旧确认失效。
        audit["episode_research_receipt"]["mapping_digest"] = "d" * 64
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET review_result_json=? WHERE token='one'", (json.dumps(audit),))
        with self.assertRaisesRegex(ValueError, "预览已变化"):
            batches.submit_choices(entries, preview["digest"])

    def test_review_change_between_preview_check_and_claim_cannot_change_write_scope(self):
        self.create("one", review=True)
        entries = [{"confirmation_id": "one", "candidate_index": 0}]
        preview = batches.preview_choices(entries)
        original = confirmations.start_confirmation
        def racing_start(token, *args, **kwargs):
            batches.save_selection(token, 1)
            return original(token, *args, **kwargs)
        with patch.object(confirmations, "start_confirmation", side_effect=racing_start):
            result = batches.submit_choices(entries, preview["digest"])
        self.assertEqual((result["accepted"], result["failed"]), (0, 1))
        self.assertEqual(self.row("one")["status"], "pending")
