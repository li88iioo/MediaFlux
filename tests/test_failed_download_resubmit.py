"""云端下载完成但落稳失败后的重投、RSS reset 与活跃任务防重回归。"""
from __future__ import annotations

from tests.support import assert_present

from typing import cast
from starlette.requests import Request

import json
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.modules import download_dispatcher as dispatcher
from app.modules.rss import RSSEngine
from app.repositories.rss import bind_rss_entry_download
from app.routes import downloads_api, rss_api
from tests.support import (
    InitializedWebTestCase,
    isolated_test_database,
    seed_rss_entry_state,
)

_SETTLE_ERROR = "云端文件长时间未落稳；读取隔离目录失败: GuangYaReadRejected"
_ACTIVE = ("submitting", "submitted", "downloading", "outcome_unknown")


class FailedDownloadResubmitTests(InitializedWebTestCase):
    def setUp(self) -> None:
        self.enterContext(isolated_test_database())
        self.enterContext(patch.object(dispatcher, "get", return_value="http://qb.invalid"))
        self.enterContext(patch.object(
            dispatcher, "analyze_offline_url", return_value=SimpleNamespace(allowed=True, reason="")
        ))
        self.enterContext(patch.object(rss_api, "require_api_login"))
        self.enterContext(patch.object(downloads_api, "require_api_login"))
        self.sequence = 0

    def resource(self, **updates) -> tuple[int, int]:
        self.sequence += 1
        source = f"magnet:?xt=urn:btih:{self.sequence:040x}"
        item = dispatcher.DownloadInput("magnet", "Episode", source)
        request_id = int(dispatcher.create_request(item, "", "", origin="rss:test")["id"])
        fields = {
            "status": "completed", "targets": "guangya", "gy_status": "completed",
            "organize_started": -1, "organize_status": "failed",
            "organize_error": _SETTLE_ERROR, "organize_task_id": "",
            "strm_status": "skipped", "gy_target_dir": "old-isolated-stage",
            "gy_requested_target_dir": "target-parent", "gy_staging_parent_dir": "target-parent",
        }
        fields.update(updates)
        db.update_download_request(request_id, **fields)
        sub_id = db.add_rss_subscription(
            f"failed-{self.sequence}", f"https://example.invalid/{self.sequence}.xml",
            download_method="guangya", gy_target_dir="target-parent",
        )
        entry_id = int(db.add_rss_entry_with_media(
            sub_id, "Episode", f"entry-{self.sequence}",
            payload=json.dumps({"torrent_url": source}),
        )["id"])
        seed_rss_entry_state(entry_id, "submitting")
        bind_rss_entry_download(entry_id, dispatcher.request_key(item), "guangya")
        seed_rss_entry_state(entry_id, "downloaded")
        return request_id, entry_id

    @staticmethod
    def snapshot(entry_id: int) -> dict:
        row = db.get_rss_entry(entry_id)
        return {field: assert_present(row)[field] for field in (
            "id", "status", "processed", "created_at", "failure_code", "failure_retryable"
        )}

    @staticmethod
    def submitted(_row, **_kwargs) -> dict:
        return {"ok": True, "task_ids": ["gy-new-task"], "task_id": "gy-new-task"}

    def test_completed_download_with_failed_settle_is_retryable(self):
        request_id, _entry_id = self.resource()
        capabilities = dispatcher.download_resubmit_capabilities(db.get_download_request(request_id))
        self.assertTrue(capabilities["guangya"]["enabled"], capabilities)

    def test_resubmit_creates_successor_and_releases_old_attention(self):
        request_id, _entry_id = self.resource()
        with (
            patch.object(dispatcher, "_submit_guangya", side_effect=self.submitted) as gy,
            patch.object(dispatcher, "_submit_qb") as qb,
        ):
            result = dispatcher.resubmit_download_request(request_id, "guangya")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["created"])
        self.assertNotEqual(result["request_id"], request_id)
        gy.assert_called_once()
        qb.assert_not_called()
        successor = db.get_download_request(result["request_id"])
        assert successor is not None
        self.assertEqual(successor["gy_status"], "submitted")
        self.assertEqual(successor["gy_requested_target_dir"], "target-parent")
        self.assertNotEqual(successor["gy_target_dir"], "old-isolated-stage")
        old = db.get_download_request(request_id)
        assert old is not None
        self.assertEqual(old["organize_status"], "resubmitted")
        self.assertEqual(old["organize_error"], _SETTLE_ERROR)
        self.assertEqual(db.count_download_requests_requiring_attention(request_id), 0)

    def test_download_page_batch_does_not_skip_failed_settle(self):
        request_id, _entry_id = self.resource()
        with patch.object(dispatcher, "_submit_guangya", side_effect=self.submitted) as backend:
            response = downloads_api.resubmit_issues_batch(
                cast(Request, None), {"request_ids": [request_id], "target": "guangya"}
            )
        body = json.loads(bytes(response.body))
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(body["succeeded"], 1, body)
        self.assertEqual(body["skipped"], 0, body)
        backend.assert_called_once()

    def test_normal_completed_backend_stays_nonretryable(self):
        request_id, _entry_id = self.resource(organize_status="completed", organize_started=1)
        capabilities = dispatcher.download_resubmit_capabilities(db.get_download_request(request_id))
        self.assertFalse(capabilities["guangya"]["enabled"])
        with patch.object(dispatcher, "_submit_guangya") as backend:
            result = dispatcher.resubmit_download_request(request_id, "guangya")
        self.assertFalse(result["ok"])
        backend.assert_not_called()

    def test_active_selected_backend_cannot_bypass_dedupe(self):
        for status in (*_ACTIVE, "resubmitted"):
            with self.subTest(status=status):
                request_id, _entry_id = self.resource(gy_status=status)
                with patch.object(dispatcher, "_submit_guangya") as backend:
                    result = dispatcher.resubmit_download_request(
                        request_id, "guangya", allow_completed=True
                    )
                self.assertFalse(result["ok"], result)
                backend.assert_not_called()

    def test_terminal_root_with_active_peer_is_not_archived(self):
        for status in _ACTIVE:
            with self.subTest(status=status):
                request_id, entry_id = self.resource(qb_status=status, targets="both")
                get_download_request_result = db.get_download_request(request_id)
                assert get_download_request_result is not None
                key = get_download_request_result["request_key"]
                capabilities = dispatcher.download_resubmit_capabilities(
                    db.get_download_request(request_id), allow_completed=True
                )
                self.assertFalse(capabilities["guangya"]["enabled"], capabilities)
                with patch.object(dispatcher, "_submit_guangya") as backend:
                    result = dispatcher.resubmit_download_request(request_id, "guangya")
                    rss_result = RSSEngine().download(entry_id)
                self.assertFalse(result["ok"], result)
                self.assertTrue(rss_result["existing"], rss_result)
                backend.assert_not_called()
                get_download_request_result_2 = db.get_download_request(request_id)
                assert get_download_request_result_2 is not None
                self.assertEqual(get_download_request_result_2["request_key"], key)
                self.assertEqual(db.update_rss_entries_processed([entry_id], False), 0)

    def test_rss_reset_api_reopens_only_linked_failed_download(self):
        request_id, entry_id = self.resource()
        healthy_id, healthy_entry = self.resource(organize_status="completed", organize_started=1)
        before = db.get_rss_entry(entry_id)
        response = rss_api.mark_entries(cast(Request, None), {
            "entry_ids": [entry_id, healthy_entry], "processed": False,
        })
        body = json.loads(bytes(response.body))
        self.assertEqual(body["updated"], 1, body)
        reset = db.get_rss_entry(entry_id)
        assert reset is not None
        self.assertEqual((reset["status"], reset["processed"], reset["processed_at"]), ("pending", 0, None))
        assert before is not None
        self.assertEqual(reset["download_request_key"], before["download_request_key"])
        self.assertEqual(reset["download_backend"], "guangya")
        get_rss_entry_result = db.get_rss_entry(healthy_entry)
        assert get_rss_entry_result is not None
        self.assertEqual(get_rss_entry_result["status"], "downloaded")
        get_download_request_result_3 = db.get_download_request(request_id)
        assert get_download_request_result_3 is not None
        self.assertEqual(get_download_request_result_3["organize_status"], "failed")
        get_download_request_result_4 = db.get_download_request(healthy_id)
        assert get_download_request_result_4 is not None
        self.assertEqual(get_download_request_result_4["organize_status"], "completed")

    def test_legacy_failed_settle_receipt_is_retryable_after_schema_upgrade(self):
        request_id, entry_id = self.resource()
        # 旧库只有内容 key：必须经一次 schema 迁移建立归属，不能在查询时猜当前 owner。
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET download_request_id=NULL WHERE id=?", (entry_id,))
            conn.execute("PRAGMA user_version=35")
        db.init_db()
        entry = assert_present(db.get_rss_entry(entry_id))
        self.assertEqual(entry["delivery_request_id"], request_id)
        engine = RSSEngine()
        self.addCleanup(engine.close)
        with patch.object(dispatcher, "_submit_guangya", side_effect=self.submitted) as backend:
            result = engine.download(entry_id)
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(result["request_id"], request_id)
        backend.assert_called_once()

    def test_rss_direct_download_retries_failed_settle_without_manual_reset(self):
        request_id, entry_id = self.resource()
        with patch.object(dispatcher, "_submit_guangya", side_effect=self.submitted) as backend:
            result = RSSEngine().download(entry_id)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["existing"], result)
        self.assertNotEqual(result["request_id"], request_id)
        backend.assert_called_once()
        get_rss_entry_result_2 = db.get_rss_entry(entry_id)
        assert get_rss_entry_result_2 is not None
        self.assertEqual(get_rss_entry_result_2["status"], "downloaded")
        get_download_request_result_5 = db.get_download_request(request_id)
        assert get_download_request_result_5 is not None
        self.assertEqual(get_download_request_result_5["organize_status"], "resubmitted")
        with db.get_conn() as conn:
            log = conn.execute(
                "SELECT rss_item_id,path FROM download_log WHERE request_id=? AND source='guangya'",
                (result["request_id"],),
            ).fetchone()
        get_rss_entry_result_3 = db.get_rss_entry(entry_id)
        assert get_rss_entry_result_3 is not None
        self.assertEqual(log["rss_item_id"], get_rss_entry_result_3["rss_item_id"])
        self.assertEqual(log["path"], "[magnet]")

    def test_rss_download_after_reset_uses_the_same_successor_retry(self):
        request_id, entry_id = self.resource()
        self.assertEqual(db.update_rss_entries_processed([entry_id], False), 1)
        with patch.object(dispatcher, "_submit_guangya", side_effect=self.submitted) as backend:
            result = RSSEngine().download(entry_id)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["existing"], result)
        self.assertNotEqual(result["request_id"], request_id)
        backend.assert_called_once()

    def test_binding_failed_settle_does_not_reproject_it_as_downloaded(self):
        request_id, entry_id = self.resource()
        seed_rss_entry_state(entry_id, "pending")
        self.assertTrue(db.claim_rss_entry(entry_id))
        get_download_request_result_6 = db.get_download_request(request_id)
        assert get_download_request_result_6 is not None
        bind_rss_entry_download(entry_id, get_download_request_result_6["request_key"], "guangya")
        row = db.get_rss_entry(entry_id)
        assert row is not None
        self.assertEqual((row["status"], row["processed"]), ("submitting", 0))

    def test_binding_active_download_still_projects_backend_acceptance(self):
        request_id, entry_id = self.resource(status="submitted", gy_status="submitted")
        seed_rss_entry_state(entry_id, "pending")
        self.assertTrue(db.claim_rss_entry(entry_id))
        get_download_request_result_7 = db.get_download_request(request_id)
        assert get_download_request_result_7 is not None
        bind_rss_entry_download(entry_id, get_download_request_result_7["request_key"], "guangya")
        row = db.get_rss_entry(entry_id)
        assert row is not None
        self.assertEqual((row["status"], row["processed"]), ("downloaded", 1))

    def test_rss_healthy_active_and_unknown_downloads_keep_dedupe(self):
        for status in ("completed", *_ACTIVE):
            with self.subTest(status=status):
                _request_id, entry_id = self.resource(
                    gy_status=status, organize_status="completed" if status == "completed" else "failed"
                )
                self.assertEqual(db.update_rss_entries_processed([entry_id], False), 0)
                with patch.object(dispatcher, "_submit_guangya") as backend:
                    result = RSSEngine().download(entry_id)
                self.assertTrue(result["existing"], result)
                backend.assert_not_called()

    def test_duplicate_rss_entries_submit_failed_source_only_once(self):
        request_id, entry_id = self.resource()
        entry = db.get_rss_entry(entry_id)
        assert entry is not None
        second_id = int(db.add_rss_entry_with_media(
            entry["rss_item_id"], "Duplicate", "duplicate-guid", payload=entry["payload"]
        )["id"])
        seed_rss_entry_state(second_id, "submitting")
        bind_rss_entry_download(second_id, entry["download_request_key"], "guangya")
        seed_rss_entry_state(second_id, "downloaded")
        with patch.object(dispatcher, "_submit_guangya", side_effect=self.submitted) as backend:
            result = RSSEngine().download_many([entry_id, second_id])
        self.assertEqual(result["success_count"], 1, result)
        self.assertEqual(result["existing_count"], 1, result)
        self.assertEqual(result["failure_count"], 0, result)
        backend.assert_called_once()
        get_download_request_result_8 = db.get_download_request(request_id)
        assert get_download_request_result_8 is not None
        self.assertEqual(get_download_request_result_8["organize_status"], "resubmitted")

    def test_failed_rss_resubmit_does_not_clear_original_attention(self):
        request_id, entry_id = self.resource()
        with patch.object(dispatcher, "_submit_guangya", return_value={"ok": False, "error": "Rejected"}):
            result = RSSEngine().download(entry_id)
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["existing"], result)
        row = db.get_rss_entry(entry_id)
        assert row is not None
        self.assertEqual((row["status"], row["processed"]), ("failed", 0))
        get_download_request_result_9 = db.get_download_request(request_id)
        assert get_download_request_result_9 is not None
        self.assertEqual(get_download_request_result_9["organize_status"], "failed")
        self.assertEqual(db.count_download_requests_requiring_attention(request_id), 1)

    def test_unknown_rss_resubmit_keeps_manual_review_and_dedupe(self):
        request_id, entry_id = self.resource()
        with patch.object(dispatcher, "_submit_guangya", return_value={
            "ok": False, "outcome_unknown": True, "error": "Receipt unavailable",
        }) as backend:
            result = RSSEngine().download(entry_id)
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["review_required"], result)
        get_rss_entry_result_4 = db.get_rss_entry(entry_id)
        assert get_rss_entry_result_4 is not None
        self.assertEqual(get_rss_entry_result_4["processed"], 0)
        get_download_request_result_10 = db.get_download_request(request_id)
        assert get_download_request_result_10 is not None
        self.assertEqual(get_download_request_result_10["organize_status"], "failed")
        backend.assert_called_once()
        with patch.object(dispatcher, "_submit_guangya") as backend:
            repeated = RSSEngine().download(entry_id)
        self.assertFalse(repeated["ok"], repeated)
        backend.assert_not_called()

    def test_snapshot_reset_can_reopen_linked_downloaded_failure(self):
        _request_id, entry_id = self.resource()
        self.assertEqual(db.update_rss_entries_processed_snapshot([self.snapshot(entry_id)], False), 1)
        row = db.get_rss_entry(entry_id)
        assert row is not None
        self.assertEqual((row["status"], row["processed"]), ("pending", 0))

    def test_snapshot_reset_is_atomic_when_another_download_is_healthy(self):
        _request_id, entry_id = self.resource()
        _healthy_request, healthy_entry = self.resource(organize_status="completed", organize_started=1)
        rows = [self.snapshot(entry_id), self.snapshot(healthy_entry)]
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO rss_entry_media(rss_entry_id,skip_reason,created_at,updated_at) "
                "VALUES(?,'keep','old','old')",
                [(entry_id,), (healthy_entry,)],
            )
        self.assertEqual(db.update_rss_entries_processed_snapshot(rows, False), 0)
        get_rss_entry_result_5 = db.get_rss_entry(entry_id)
        assert get_rss_entry_result_5 is not None
        self.assertEqual(get_rss_entry_result_5["status"], "downloaded")
        get_rss_entry_result_6 = db.get_rss_entry(healthy_entry)
        assert get_rss_entry_result_6 is not None
        self.assertEqual(get_rss_entry_result_6["status"], "downloaded")
        with db.get_conn() as conn:
            media = conn.execute("SELECT skip_reason,updated_at FROM rss_entry_media WHERE rss_entry_id IN (?,?)",
                                 (entry_id, healthy_entry)).fetchall()
        self.assertEqual([tuple(row) for row in media], [("keep", "old"), ("keep", "old")])
        # 普通显式入口只更新合格条目；Agent 快照入口则必须全批次原子拒绝。
        self.assertEqual(db.update_rss_entries_processed([entry_id, healthy_entry], False), 1)
        with db.get_conn() as conn:
            media = {row["rss_entry_id"]: row["skip_reason"] for row in conn.execute(
                "SELECT rss_entry_id,skip_reason FROM rss_entry_media WHERE rss_entry_id IN (?,?)",
                (entry_id, healthy_entry),
            )}
        self.assertEqual(media, {entry_id: "", healthy_entry: "keep"})

    def test_retry_fence_blocks_backend_or_peer_becoming_active_after_capability_check(self):
        for field in ("gy_status", "qb_status"):
            for status in _ACTIVE:
                with self.subTest(field=field, status=status):
                    request_id, _entry_id = self.resource()
                    get_download_request_result_11 = db.get_download_request(request_id)
                    assert get_download_request_result_11 is not None
                    original_key = get_download_request_result_11["request_key"]
                    original = dispatcher.download_resubmit_capabilities

                    def checked_then_changed(
                        row, *, source_id=request_id, changed_field=field,
                        changed_status=status, capability_fn=original, **kwargs,
                    ):
                        capability = capability_fn(row, **kwargs)
                        db.update_download_request(source_id, **{changed_field: changed_status})
                        return capability

                    with (
                        patch.object(dispatcher, "download_resubmit_capabilities", side_effect=checked_then_changed),
                        patch.object(dispatcher, "_submit_guangya") as backend,
                    ):
                        result = dispatcher.resubmit_download_request(request_id, "guangya")
                    self.assertFalse(result["ok"], result)
                    self.assertTrue(result.get("retry_blocked"), result)
                    get_download_request_result_12 = db.get_download_request(request_id)
                    assert get_download_request_result_12 is not None
                    self.assertEqual(get_download_request_result_12["request_key"], original_key)
                    backend.assert_not_called()
