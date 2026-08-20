from __future__ import annotations

import json
from unittest.mock import patch

from app import database as db
from app.modules.rss import RSSEngine
from tests.support import IsolatedDatabaseTestCase


class RSSGuangYaDedupeTests(IsolatedDatabaseTestCase):
    def _entry(self, suffix: str, infohash: str = "", *, url: str = "") -> tuple[int, int]:
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
        self.assertIsNotNone(entry_id)
        return subscription_id, int(entry_id)

    def test_same_infohash_across_subscriptions_is_submitted_once(self) -> None:
        infohash = "a" * 40
        _, first = self._entry("first", infohash)
        _, second = self._entry("second", infohash)
        engine = RSSEngine()

        with patch.object(engine, "_push_guangya", return_value={"ok": True}) as push:
            result = engine.download_many([first, second])

        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["existing_count"], 1)
        self.assertEqual(result["failure_count"], 0)
        push.assert_called_once()
        self.assertEqual(str(db.get_rss_entry(first)["status"]), "downloaded")
        self.assertEqual(str(db.get_rss_entry(second)["status"]), "downloaded")

    def test_same_opaque_url_across_subscriptions_is_submitted_once(self) -> None:
        url = "https://example.invalid/download?id=opaque-same"
        _, first = self._entry("opaque-first", url=url)
        _, second = self._entry("opaque-second", url=url)
        engine = RSSEngine()

        with patch.object(engine, "_push_guangya", return_value={"ok": True}) as push:
            first_result = engine.download(first)
            second_result = engine.download(second)

        self.assertTrue(first_result["ok"])
        self.assertTrue(second_result["ok"])
        self.assertTrue(second_result["existing"])
        push.assert_called_once()

    def test_known_submission_failure_releases_claim_for_next_entry(self) -> None:
        infohash = "b" * 40
        _, first = self._entry("known-failure", infohash)
        _, second = self._entry("known-retry", infohash)
        engine = RSSEngine()

        with patch.object(
            engine,
            "_push_guangya",
            side_effect=[{"ok": False, "error": "光鸭未登录"}, {"ok": True}],
        ) as push:
            result = engine.download_many([first, second])

        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["existing_count"], 0)
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(push.call_count, 2)
        self.assertEqual(str(db.get_rss_entry(first)["status"]), "failed")
        self.assertEqual(str(db.get_rss_entry(second)["status"]), "downloaded")

    def test_unknown_submission_outcome_requires_manual_review(self) -> None:
        infohash = "c" * 40
        _, first = self._entry("unknown", infohash)
        _, second = self._entry("unknown-duplicate", infohash)
        engine = RSSEngine()

        with patch.object(
            engine,
            "_push_guangya",
            return_value={"ok": False, "outcome_unknown": True, "error": "timeout"},
        ) as push:
            first_result = engine.download(first)
        with patch.object(engine, "_push_guangya", return_value={"ok": True}) as duplicate_push:
            second_result = engine.download(second)

        self.assertFalse(first_result["ok"])
        self.assertFalse(second_result["ok"])
        self.assertTrue(second_result["review_required"])
        self.assertIn("待核对", second_result["error"])
        push.assert_called_once()
        duplicate_push.assert_not_called()
        self.assertEqual(str(db.get_rss_entry(first)["failure_code"]), "guangya_outcome_unknown")
        self.assertEqual(str(db.get_rss_entry(second)["status"]), "pending")

    def test_opaque_unknown_claim_requires_owner_reset_before_retry(self) -> None:
        url = "https://example.invalid/download?id=opaque-unknown"
        _, first = self._entry("opaque-unknown", url=url)
        engine = RSSEngine()

        with patch.object(
            engine,
            "_push_guangya",
            return_value={"ok": False, "outcome_unknown": True, "error": "timeout"},
        ) as push:
            first_result = engine.download(first)
            blocked_result = engine.download(first)

        self.assertFalse(first_result["ok"])
        self.assertFalse(blocked_result["ok"])
        self.assertTrue(blocked_result["review_required"])
        push.assert_called_once()
        self.assertEqual(db.update_rss_entries_processed([first], False), 1)

        with patch.object(engine, "_push_guangya", return_value={"ok": True}) as retry_push:
            retry_result = engine.download(first)
        self.assertTrue(retry_result["ok"])
        retry_push.assert_called_once()

    def test_claim_and_entry_are_acquired_atomically_with_fencing_token(self) -> None:
        infohash = "d" * 40
        _, first = self._entry("atomic-first", infohash)
        _, second = self._entry("atomic-second", infohash)

        first_claim = db.claim_rss_guangya_download(infohash, first)
        self.assertEqual(first_claim["status"], "claimed")
        self.assertTrue(first_claim["lease_token"])
        self.assertEqual(str(db.get_rss_entry(first)["status"]), "submitting")
        self.assertEqual(db.claim_rss_guangya_download(infohash, second)["status"], "busy")
        self.assertFalse(
            db.finalize_rss_guangya_download(
                infohash, first, "wrong-token", outcome="submitted"
            )
        )
        self.assertTrue(
            db.finalize_rss_guangya_download(
                infohash,
                first,
                first_claim["lease_token"],
                outcome="submitted",
            )
        )
        self.assertEqual(str(db.get_rss_entry(first)["status"]), "downloaded")

    def test_late_success_can_finalize_a_stale_unknown_claim(self) -> None:
        infohash = "e" * 40
        _, first = self._entry("late-success", infohash)
        claim = db.claim_rss_guangya_download(infohash, first)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_guangya_download_claims SET updated_at='2000-01-01 00:00:00' "
                "WHERE infohash=?",
                (infohash,),
            )
        db.recover_stale_submitting_rss_entries(stale_minutes=15)
        self.assertEqual(str(db.get_rss_entry(first)["failure_code"]), "guangya_outcome_unknown")
        self.assertTrue(
            db.finalize_rss_guangya_download(
                infohash, first, claim["lease_token"], outcome="submitted"
            )
        )
        self.assertEqual(str(db.get_rss_entry(first)["status"]), "downloaded")
        _, second = self._entry("late-success-duplicate", infohash)
        self.assertEqual(db.claim_rss_guangya_download(infohash, second)["status"], "submitted")
        self.assertEqual(str(db.get_rss_entry(second)["status"]), "downloaded")

    def test_manual_reset_clears_unknown_claim_for_explicit_retry(self) -> None:
        infohash = "f" * 40
        _, first = self._entry("manual-reset", infohash)
        claim = db.claim_rss_guangya_download(infohash, first)
        self.assertTrue(
            db.finalize_rss_guangya_download(
                infohash, first, claim["lease_token"], outcome="unknown"
            )
        )
        self.assertEqual(db.update_rss_entries_processed([first], False), 1)
        retry = db.claim_rss_guangya_download(infohash, first)
        self.assertEqual(retry["status"], "claimed")

    def test_deleting_subscription_clears_unresolved_claim(self) -> None:
        infohash = "1" * 40
        subscription_id, first = self._entry("delete-unresolved", infohash)
        claim = db.claim_rss_guangya_download(infohash, first)
        self.assertTrue(
            db.finalize_rss_guangya_download(
                infohash, first, claim["lease_token"], outcome="unknown"
            )
        )
        db.delete_rss_subscription(subscription_id)
        _, second = self._entry("after-delete", infohash)
        self.assertEqual(db.claim_rss_guangya_download(infohash, second)["status"], "claimed")

    def test_push_guangya_preserves_known_failure_shape(self) -> None:
        engine = RSSEngine()
        with patch(
            "app.modules.offline.submit_offline",
            return_value={"ok": False, "error": "光鸭未登录"},
        ):
            result = engine._push_guangya("magnet:?xt=urn:btih:" + "2" * 40)
        self.assertFalse(result["ok"])
        self.assertNotIn("outcome_unknown", result)
