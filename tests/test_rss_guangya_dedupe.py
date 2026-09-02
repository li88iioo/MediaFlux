from __future__ import annotations

import json
from unittest.mock import patch

from app import database as db
from app.modules.rss import RSSEngine
from tests.support import IsolatedDatabaseTestCase


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
        entry_id = db.add_rss_entry(
            subscription_id,
            f"Episode {suffix}",
            f"guid-{suffix}",
            payload=json.dumps(
                {"torrent_url": url or f"magnet:?xt=urn:btih:{infohash}"},
                ensure_ascii=False,
            ),
        )
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
        self.assertEqual(request["origin"], f"rss:{first_sub}")
        self.assertEqual(request["gy_status"], "submitted")
        self.assertEqual(request["gy_task_id"], "gy-task")
        self.assertEqual(json.loads(request["gy_task_ids"]), ["gy-task"])
        self.assertEqual(request["gy_target_dir"], "target-id")
        self.assertEqual(request["gy_target_name"], "动漫")
        self.assertEqual(request["gy_isolated"], 1)
        self.assertEqual(request["gy_staging_parent_dir"], "target-id")
        self.assertEqual(request["gy_staging_name"], f"MF-{request_id}")
        self.assertEqual(db.get_rss_entry(first)["status"], "downloaded")
        self.assertEqual(db.get_rss_entry(second)["status"], "downloaded")

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
        second_result = engine.download(second)

        self.assertFalse(first_result["ok"])
        self.assertTrue(second_result["ok"])
        self.assertNotEqual(first_result["request_id"], second_result["request_id"])
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(db.get_rss_entry(first)["status"], "failed")
        self.assertEqual(db.get_rss_entry(first)["failure_code"], "guangya_submit_failed")
        self.assertEqual(db.get_rss_entry(second)["status"], "downloaded")

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
        self.assertEqual(db.get_rss_entry(first)["failure_code"], "guangya_outcome_unknown")
        self.assertEqual(db.get_rss_entry(second)["failure_code"], "guangya_outcome_unknown")
        request = db.get_download_request(first_result["request_id"])
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
        self.assertEqual(db.get_rss_entry(entry_id)["failure_code"], "guangya_outcome_unknown")
        request = db.get_download_request(result["request_id"])
        self.assertEqual(request["gy_status"], "outcome_unknown")
        self.assertEqual(json.loads(request["gy_task_ids"]), ["gy-accepted"])

    @patch(
        "app.modules.download_dispatcher.submit_offline",
        side_effect=_successful_submit,
    )
    def test_processed_entry_never_resubmits(self, submit) -> None:
        _, entry_id = self._entry("processed", "e" * 40)
        db.update_rss_entry_status(entry_id, "downloaded")

        result = RSSEngine().download(entry_id)

        self.assertTrue(result["ok"])
        self.assertTrue(result["existing"])
        submit.assert_not_called()
