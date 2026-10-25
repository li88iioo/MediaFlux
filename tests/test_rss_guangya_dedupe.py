from __future__ import annotations

import json
from unittest.mock import patch

from app import database as db
from app.modules.rss import RSSEngine
from tests.support import IsolatedDatabaseTestCase
from tests.support import seed_rss_entry_state


def _clear() -> None:
    with db.get_conn() as conn:
        for table in (
            "download_log",
            "download_request_keys",
            "download_requests",
            "rss_entry_media",
            "rss_entries",
            "rss_items",
        ):
            conn.execute(f"DELETE FROM {table}")


def _legacy_delivery_schema() -> None:
    """模拟真实 v35 升级，不依赖启动时按 key 猜测归属的旧运行时路径。"""
    with db.get_conn() as conn:
        conn.execute("DROP INDEX idx_rss_entries_delivery_request")
        conn.execute("ALTER TABLE rss_entries DROP COLUMN download_request_id")
        conn.execute("PRAGMA user_version=35")


def _submitted_result(request_id: int, *, task_id: str = "gy-task") -> dict:
    return {
        "ok": True,
        "task_ids": [task_id],
        "batch_count": 1,
        "selected_count": 1,
        "selection_mode": "files",
        "decision": {
            "target_dir_id": "target-id",
            "target_dir_name": "动漫",
        },
        "staging": {
            "id": f"stage-{request_id}",
            "parent_id": "target-id",
            "parent_name": "动漫",
            "name": f"MF-{request_id}",
            "isolated": True,
            "cleanup_status": "pending",
        },
    }


class RSSGuangYaUnifiedDownloadTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        _clear()

    @staticmethod
    def _entry(suffix: str, infohash: str = "", *, url: str = "") -> tuple[int, int]:
        subscription_id = db.add_rss_subscription(
            f"guangya-{suffix}",
            f"https://example.invalid/{suffix}.xml",
            download_method="guangya",
            gy_target_dir="target-id",
            gy_target_dir_name="动漫",
        )
        entry_id = db.add_rss_entry_with_media(
            subscription_id,
            f"Episode {suffix}",
            f"guid-{suffix}",
            payload=json.dumps(
                {"torrent_url": url or f"magnet:?xt=urn:btih:{infohash}"},
                ensure_ascii=False,
            ),
        )["id"]
        assert entry_id is not None
        return subscription_id, int(entry_id)

    @staticmethod
    def _successful_submit(_url: str, **kwargs) -> dict:
        request_id = int(kwargs["task_key"])
        snapshot = _submitted_result(request_id)["staging"]
        kwargs["on_staging_created"](snapshot)
        return _submitted_result(request_id)

    @patch(
        "app.modules.download_dispatcher.submit_offline",
        side_effect=_successful_submit,
    )
    def test_same_infohash_across_subscriptions_creates_one_tracked_request(
        self, submit
    ) -> None:
        infohash = "a" * 40
        first_sub, first = self._entry("first", infohash)
        _, second = self._entry("second", infohash)

        result = RSSEngine().download_many([first, second])

        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["existing_count"], 1)
        self.assertEqual(result["failure_count"], 0)
        submit.assert_called_once()
        request_id = int((result["succeeded"] + result["existing"])[0]["request_id"])
        request = db.get_download_request(request_id)
        assert request is not None
        self.assertEqual(request["origin"], f"rss:{first_sub}")
        self.assertEqual(request["gy_status"], "submitted")
        self.assertEqual(request["gy_task_id"], "gy-task")
        self.assertEqual(json.loads(request["gy_task_ids"]), ["gy-task"])
        self.assertEqual(request["gy_target_dir"], "target-id")
        self.assertEqual(request["gy_target_name"], "动漫")
        self.assertEqual(request["gy_isolated"], 1)
        self.assertEqual(request["gy_staging_parent_dir"], "target-id")
        self.assertEqual(request["gy_staging_name"], f"MF-{request_id}")
        get_rss_entry_result = db.get_rss_entry(first)
        assert get_rss_entry_result is not None
        self.assertEqual(get_rss_entry_result["status"], "downloaded")
        get_rss_entry_result_2 = db.get_rss_entry(second)
        assert get_rss_entry_result_2 is not None
        self.assertEqual(get_rss_entry_result_2["status"], "downloaded")

        kwargs = submit.call_args.kwargs
        self.assertTrue(kwargs["isolate_task"])
        self.assertEqual(kwargs["task_key"], str(request_id))
        self.assertEqual(kwargs["target_dir_id"], "target-id")
        self.assertEqual(kwargs["target_dir_name"], "动漫")

    @patch(
        "app.modules.download_dispatcher.submit_offline",
        side_effect=_successful_submit,
    )
    def test_same_opaque_url_across_subscriptions_is_submitted_once(self, submit) -> None:
        url = "https://example.invalid/download?id=opaque-same&token=private"
        _, first = self._entry("opaque-first", url=url)
        _, second = self._entry("opaque-second", url=url)
        engine = RSSEngine()

        first_result = engine.download(first)
        second_result = engine.download(second)

        self.assertTrue(first_result["ok"])
        self.assertTrue(second_result["ok"])
        self.assertTrue(second_result["existing"])
        submit.assert_called_once()
        logs = db.list_download_logs(source="guangya", limit=10)
        self.assertEqual(len(logs), 2)
        self.assertTrue(all("private" not in str(row["path"] or "") for row in logs))
        self.assertTrue(all(int(row["request_id"] or 0) for row in logs))

    @patch("app.modules.download_dispatcher.submit_offline")
    def test_known_failure_allows_a_new_request_attempt(self, submit) -> None:
        outcomes = iter((
            {"ok": False, "error": "光鸭未登录"},
            "success",
        ))

        def submit_side_effect(url: str, **kwargs):
            outcome = next(outcomes)
            if outcome == "success":
                return self._successful_submit(url, **kwargs)
            return outcome

        submit.side_effect = submit_side_effect
        infohash = "b" * 40
        _, first = self._entry("known-failure", infohash)
        _, second = self._entry("known-retry", infohash)
        engine = RSSEngine()

        first_result = engine.download(first)
        get_rss_entry_result_3 = db.get_rss_entry(first)
        assert get_rss_entry_result_3 is not None
        self.assertEqual(get_rss_entry_result_3["status"], "failed")
        get_rss_entry_result_4 = db.get_rss_entry(first)
        assert get_rss_entry_result_4 is not None
        self.assertEqual(get_rss_entry_result_4["failure_code"], "guangya_submit_failed")
        second_result = engine.download(second)

        self.assertFalse(first_result["ok"])
        self.assertTrue(second_result["ok"])
        self.assertNotEqual(first_result["request_id"], second_result["request_id"])
        self.assertEqual(submit.call_count, 2)
        get_rss_entry_result_5 = db.get_rss_entry(first)
        assert get_rss_entry_result_5 is not None
        self.assertEqual(get_rss_entry_result_5["status"], "downloaded")
        get_rss_entry_result_6 = db.get_rss_entry(first)
        assert get_rss_entry_result_6 is not None
        self.assertEqual(get_rss_entry_result_6["failure_code"], "")
        get_download_request_result = db.get_download_request(first_result["request_id"])
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result["status"], "failed")
        get_rss_entry_result_7 = db.get_rss_entry(second)
        assert get_rss_entry_result_7 is not None
        self.assertEqual(get_rss_entry_result_7["status"], "downloaded")

    @patch("app.modules.download_dispatcher.submit_offline")
    def test_unknown_outcome_blocks_duplicate_without_a_second_submission(
        self, submit
    ) -> None:
        submit.return_value = {
            "ok": False,
            "outcome_unknown": True,
            "task_ids": ["gy-possibly-accepted"],
            "batch_count": 1,
            "selected_count": 1,
            "error": "timeout",
            "decision": {
                "target_dir_id": "target-id",
                "target_dir_name": "动漫",
            },
            "staging": {
                "id": "stage-unknown",
                "parent_id": "target-id",
                "name": "MF-unknown",
                "isolated": True,
                "cleanup_status": "retained",
            },
        }
        infohash = "c" * 40
        _, first = self._entry("unknown", infohash)
        _, second = self._entry("unknown-duplicate", infohash)
        engine = RSSEngine()

        first_result = engine.download(first)
        second_result = engine.download(second)

        self.assertFalse(first_result["ok"])
        self.assertTrue(first_result["review_required"])
        self.assertFalse(second_result["ok"])
        self.assertTrue(second_result["review_required"])
        submit.assert_called_once()
        get_rss_entry_result_8 = db.get_rss_entry(first)
        assert get_rss_entry_result_8 is not None
        self.assertEqual(get_rss_entry_result_8["failure_code"], "guangya_outcome_unknown")
        get_rss_entry_result_9 = db.get_rss_entry(second)
        assert get_rss_entry_result_9 is not None
        self.assertEqual(get_rss_entry_result_9["failure_code"], "guangya_outcome_unknown")
        request = db.get_download_request(first_result["request_id"])
        assert request is not None
        self.assertEqual(request["status"], "submitted")
        self.assertEqual(request["gy_status"], "outcome_unknown")
        self.assertEqual(request["gy_task_id"], "gy-possibly-accepted")

    @patch("app.modules.download_dispatcher.submit_offline")
    def test_partial_submission_is_not_reported_as_rss_success(self, submit) -> None:
        submit.return_value = {
            "ok": False,
            "partial_success": True,
            "outcome_unknown": True,
            "task_ids": ["gy-accepted"],
            "batch_count": 2,
            "selected_count": 24,
            "error": "第二批结果未知",
            "decision": {
                "target_dir_id": "target-id",
                "target_dir_name": "动漫",
            },
            "staging": {
                "id": "stage-partial",
                "parent_id": "target-id",
                "name": "MF-partial",
                "isolated": True,
                "cleanup_status": "retained",
            },
        }
        _, entry_id = self._entry("partial", "d" * 40)

        result = RSSEngine().download(entry_id)

        self.assertFalse(result["ok"])
        self.assertTrue(result["review_required"])
        get_rss_entry_result_10 = db.get_rss_entry(entry_id)
        assert get_rss_entry_result_10 is not None
        self.assertEqual(get_rss_entry_result_10["failure_code"], "guangya_outcome_unknown")
        request = db.get_download_request(result["request_id"])
        assert request is not None
        self.assertEqual(request["gy_status"], "outcome_unknown")
        self.assertEqual(json.loads(request["gy_task_ids"]), ["gy-accepted"])

    @patch(
        "app.modules.download_dispatcher.submit_offline",
        side_effect=_successful_submit,
    )
    def test_processed_entry_never_resubmits(self, submit) -> None:
        _, entry_id = self._entry("processed", "e" * 40)
        seed_rss_entry_state(entry_id, "downloaded")

        result = RSSEngine().download(entry_id)

        self.assertTrue(result["ok"])
        self.assertTrue(result["existing"])
        submit.assert_not_called()


class RSSDownloadRetryFeedbackTests(IsolatedDatabaseTestCase):
    """运行真实RSS/dispatcher/SQLite链路，仅替换下载器网络边界。"""

    def setUp(self):
        from types import SimpleNamespace
        from app.modules import download_dispatcher as dispatcher

        _clear()
        self.dispatcher = dispatcher
        self.enterContext(patch.object(
            dispatcher, "get", side_effect=lambda key, default="": (
                "http://qb.invalid" if key == "QB_URL" else default
            ),
        ))
        self.enterContext(patch.object(
            dispatcher, "analyze_offline_url", return_value=SimpleNamespace(allowed=True, reason=""),
        ))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("unexpected network")))
        self.serial = 0

    def failed_entry(self, backend="guangya", *, url="", payload=None):
        self.serial += 1
        sub = db.add_rss_subscription(
            f"retry-{self.serial}", f"https://feed.invalid/{self.serial}",
            download_method=backend, gy_target_dir="target-id", gy_target_dir_name="测试",
        )
        entry = int(db.add_rss_entry_with_media(
            sub, f"same-title-{self.serial}", f"guid-{self.serial}",
            payload=json.dumps(payload if payload is not None else {
                "torrent_url": url or f"magnet:?xt=urn:btih:{self.serial:040x}"
            }),
        )["id"])
        method = "_submit_guangya" if backend == "guangya" else "_submit_qb"
        with patch.object(self.dispatcher, method, return_value={"ok": False, "error": "fixture rejected"}):
            result = RSSEngine().download(entry)
        self.assertFalse(result["ok"], result)
        get_rss_entry_result_11 = db.get_rss_entry(entry)
        assert get_rss_entry_result_11 is not None
        self.assertEqual(get_rss_entry_result_11["status"], "failed")
        return entry, result.get("request_id")

    def retry(self, request_id, backend):
        method = "_submit_guangya" if backend == "guangya" else "_submit_qb"
        with patch.object(self.dispatcher, method, return_value={"ok": True, "task_id": "accepted"}) as submit:
            result = self.dispatcher.resubmit_download_request(request_id, backend)
        self.assertTrue(result["ok"], result)
        submit.assert_called_once()
        return result

    def test_download_center_successor_updates_rss_for_both_backends(self):
        for backend in ("guangya", "qb"):
            with self.subTest(backend=backend):
                entry, request = self.failed_entry(backend)
                result = self.retry(request, backend)
                self.assertNotEqual(result["request_id"], request)
                row = db.get_rss_entry(entry)
                assert row is not None
                self.assertEqual((row["status"], row["processed"], row["failure_code"]), ("downloaded", 1, ""))
                db.record_rss_entry_failure(entry, "unknown_failure", False)
                get_rss_entry_result_12 = db.get_rss_entry(entry)
                assert get_rss_entry_result_12 is not None
                self.assertEqual(get_rss_entry_result_12["status"], "downloaded")
                with patch.object(self.dispatcher, "_submit_guangya") as gy, patch.object(self.dispatcher, "_submit_qb") as qb:
                    repeated = self.dispatcher.resubmit_download_request(result["request_id"], backend)
                    self.assertFalse(repeated["ok"])
                    db.list_rss_entries().items
                    db.list_rss_entries().items
                    gy.assert_not_called()
                    qb.assert_not_called()

    def test_peer_success_unknown_outcome_and_in_place_retry(self):
        for backend, prefix, peer in (("guangya", "gy", "qb"), ("qb", "qb", "gy")):
            with self.subTest(backend=backend):
                entry, request = self.failed_entry(backend)
                assert request is not None
                db.update_download_request(request, status="submitted", targets="both", **{f"{peer}_status": "submitted"})
                db.list_rss_entries().items
                get_rss_entry_result_13 = db.get_rss_entry(entry)
                assert get_rss_entry_result_13 is not None
                self.assertEqual(get_rss_entry_result_13["status"], "failed")
                db.update_download_request(request, **{f"{prefix}_status": "outcome_unknown"})
                get_rss_entry_result_14 = db.get_rss_entry(entry)
                assert get_rss_entry_result_14 is not None
                self.assertEqual(get_rss_entry_result_14["status"], "failed")
                db.update_download_request(request, **{f"{prefix}_status": "failed"})
                result = self.retry(request, backend)
                self.assertEqual(result["request_id"], request)
                get_rss_entry_result_15 = db.get_rss_entry(entry)
                assert get_rss_entry_result_15 is not None
                self.assertEqual(get_rss_entry_result_15["status"], "downloaded")
                get_download_request_result_2 = db.get_download_request(request)
                assert get_download_request_result_2 is not None
                self.assertEqual(get_download_request_result_2[f"{peer}_status"], "submitted")

    def test_manual_processed_state_is_not_rewritten(self):
        entry, request = self.failed_entry()
        db.update_rss_entries_processed([entry], True)
        self.retry(request, "guangya")
        db.record_rss_entry_failure(entry, "unknown_failure", False)
        get_rss_entry_result_16 = db.get_rss_entry(entry)
        assert get_rss_entry_result_16 is not None
        self.assertEqual(get_rss_entry_result_16["status"], "skipped")

    def test_startup_backfill_pages_past_more_than_500_unmatched_failures(self):
        entry, _request = self.failed_entry()
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET download_request_key='',download_backend='' WHERE id=?",
                (entry,),
            )
            sub_id = int(conn.execute(
                "SELECT rss_item_id FROM rss_entries WHERE id=?", (entry,)
            ).fetchone()[0])
            timestamp = db.now()
            conn.executemany(
                "INSERT INTO rss_entries(rss_item_id,title,status,processed,payload,created_at) "
                "VALUES(?,?,'failed',0,?,?)",
                [
                    (
                        sub_id,
                        f"unmatched-{index}",
                        json.dumps({"torrent_url": f"magnet:?xt=urn:btih:{index + 1:040x}"}),
                        timestamp,
                    )
                    for index in range(501)
                ],
            )

        _legacy_delivery_schema()
        db.init_db()

        recovered = db.get_rss_entry(entry)
        assert recovered is not None
        self.assertTrue(recovered["download_request_key"])
        self.assertEqual(recovered["status"], "failed")
        with db.get_conn() as conn:
            unmatched = conn.execute(
                "SELECT COUNT(*) FROM rss_entries WHERE title LIKE 'unmatched-%' "
                "AND status='failed' AND download_request_key=''"
            ).fetchone()[0]
        self.assertEqual(int(unmatched), 501)

    def test_legacy_http_torrent_failure_reconciles_only_verified_identity(self):
        from tests.test_download_http_identity_lifecycle import TORRENT_A, MIME

        fetch = self.enterContext(patch("app.modules.rss._fetch_rss_payload"))
        entries = []
        for number, suffix in enumerate(("verified", "purged", "different-source")):
            fetch.return_value = (TORRENT_A.replace(b"Movie.mkv", f"Film{number}.mkv".encode()), {"content-type": MIME})
            entry, request = self.failed_entry(url=f"https://feed.invalid/{suffix}.torrent")
            entries.append(entry)
            with db.get_conn() as conn:
                conn.execute("UPDATE rss_entries SET download_request_key='',download_backend='',download_request_id=NULL WHERE id=?", (entry,))
            # Retried through the same shared pipeline, but the pre-upgrade RSS row has no binding.
            result = self.retry(request, "guangya")
            get_rss_entry_result_17 = db.get_rss_entry(entry)
            assert get_rss_entry_result_17 is not None
            self.assertEqual(get_rss_entry_result_17["status"], "failed")
            if suffix == "purged":
                with db.get_conn() as conn:
                    conn.execute("UPDATE download_requests SET torrent_data=NULL WHERE id=?", (request,))
            elif suffix == "different-source":
                with db.get_conn() as conn:
                    conn.execute("UPDATE rss_entries SET payload=? WHERE id=?", (json.dumps({"torrent_url": "https://other.invalid/unrelated.torrent"}), entry))
            # Complete this request so the next fixture may submit identical torrent content.
            db.update_download_request(result["request_id"], status="completed", gy_status="completed")
        _legacy_delivery_schema()
        db.init_db()
        get_rss_entry_result_18 = db.get_rss_entry(entries[0])
        assert get_rss_entry_result_18 is not None
        self.assertEqual(get_rss_entry_result_18["status"], "downloaded")
        get_rss_entry_result_19 = db.get_rss_entry(entries[1])
        assert get_rss_entry_result_19 is not None
        self.assertEqual(get_rss_entry_result_19["status"], "failed")
        get_rss_entry_result_20 = db.get_rss_entry(entries[2])
        assert get_rss_entry_result_20 is not None
        self.assertEqual(get_rss_entry_result_20["status"], "failed")

    def test_bad_payload_does_not_abort_other_batch_entries(self):
        bad, _ = self.failed_entry(payload=[])
        good, _ = self.failed_entry()
        db.update_rss_entries_processed([bad, good], False)
        with patch.object(self.dispatcher, "_submit_guangya", return_value={"ok": True, "task_id": "accepted"}) as submit:
            result = RSSEngine().download_many([bad, good])
        get_rss_entry_result_21 = db.get_rss_entry(bad)
        assert get_rss_entry_result_21 is not None
        self.assertEqual(get_rss_entry_result_21["status"], "failed")
        get_rss_entry_result_22 = db.get_rss_entry(good)
        assert get_rss_entry_result_22 is not None
        self.assertEqual(get_rss_entry_result_22["status"], "downloaded", result)
        submit.assert_called_once()

    def test_old_schema_adds_binding_columns_without_losing_failed_entry(self):
        entry, request = self.failed_entry()
        _legacy_delivery_schema()
        with db.get_conn() as conn:
            conn.execute("DROP INDEX idx_rss_entries_download_request")
            conn.execute("ALTER TABLE rss_entries DROP COLUMN download_request_key")
            conn.execute("ALTER TABLE rss_entries DROP COLUMN download_backend")
        db.init_db()
        row = db.get_rss_entry(entry)
        assert row is not None
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["download_request_key"])
        self.retry(request, "guangya")
        get_rss_entry_result_23 = db.get_rss_entry(entry)
        assert get_rss_entry_result_23 is not None
        self.assertEqual(get_rss_entry_result_23["status"], "downloaded")
