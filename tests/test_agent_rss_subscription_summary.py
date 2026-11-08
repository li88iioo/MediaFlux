"""RSS 单订阅与订阅列表安全摘要契约。"""

from __future__ import annotations

import inspect
import json
from typing import Any, cast
from unittest.mock import patch

from app import database as db
from app import sdk
from app.modules import rss_subscriptions
from app.repositories import rss as rss_repository
from tests.support import IsolatedDatabaseTestCase

_SNAPSHOT = "2026-08-01 12:00:00"


def _clear_rss() -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM rss_entries")
        conn.execute("DELETE FROM rss_items")


def _set_entry(
    entry_id: int,
    *,
    status: str | None,
    processed: int = 0,
    created_at: str = _SNAPSHOT,
    submitted_at: str = "",
    processed_at: str | None = None,
) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE rss_entries SET status=?,processed=?,created_at=?,submitted_at=?,processed_at=? WHERE id=?",
            (status, processed, created_at, submitted_at, processed_at, entry_id),
        )


class RssSubscriptionSummaryTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        _clear_rss()

    def test_native_signatures_and_identifiers_are_strict(self):
        for invalid in (True, 0, "12", 2**63):
            with self.assertRaises(rss_subscriptions.RSSSubscriptionError):
                cast(Any, sdk.rss.subscription)(invalid)
        with self.assertRaises(TypeError):
            cast(Any, sdk.rss.subscription)(subscription_id=12, extra=1)
        with self.assertRaises(TypeError):
            cast(Any, sdk.rss.subscriptions)(extra=1)

    def test_summary_never_returns_subscription_or_entry_secrets(self):
        subscription_name = "private-subscription-name"
        secret_values = (
            "rss-passkey-secret",
            "exclude-secret",
            "guid-secret",
            "payload-secret",
            "/volume/private/rss",
            "cloud-directory-secret",
        )
        subscription_id = db.add_rss_subscription(
            name=subscription_name,
            urls=f"https://example.invalid/feed?passkey={secret_values[0]}",
            exclude_keywords=secret_values[1],
            refresh_interval_minutes=30,
            qb_save_path=secret_values[4],
            gy_target_dir=secret_values[5],
        )
        entry_id = db.add_rss_entry_with_media(
            subscription_id,
            "private-entry-title",
            secret_values[2],
            payload=secret_values[3],
        )["id"]
        assert entry_id is not None
        _set_entry(entry_id, status="failed", created_at="2026-07-30 12:00:00")
        with patch("app.modules.rss_subscriptions.db.now", return_value=_SNAPSHOT):
            listed = sdk.rss.subscriptions()
            detail = sdk.rss.subscription(subscription_id)
        self.assertTrue(listed["ok"])
        self.assertTrue(detail["ok"])
        self.assertEqual(detail["data"]["subscription_number"], subscription_id)
        self.assertEqual(detail["data"]["name"], subscription_name)
        self.assertEqual(listed["data"]["items"][0]["name"], subscription_name)
        self.assertEqual(detail["data"]["entry_counts"]["failed"], 1)
        serialized = json.dumps(
            {"listed": listed, "detail": detail}, ensure_ascii=False
        )
        for secret in (secret_values[0], secret_values[2], secret_values[3], "private-entry-title"):
            self.assertNotIn(secret, serialized)

    def test_recent_activity_total_is_not_truncated_by_subscription_breakdown(self):
        for index in range(20):
            subscription_id = db.add_rss_subscription(
                name=f"Feed {index:02d}",
                urls=f"https://example.invalid/{index}",
                enabled=1,
            )
            entry_id = db.add_rss_entry_with_media(
                subscription_id,
                f"entry-{index}",
                f"guid-{index}",
                payload="magnet:?xt=test",
            )["id"]
            assert entry_id is not None
            _set_entry(
                entry_id,
                status="downloaded",
                processed=1,
                processed_at="2026-08-01 11:00:00",
            )
        with patch("app.modules.rss_subscriptions.db.now", return_value=_SNAPSHOT):
            recent = sdk.rss.activity()
        self.assertEqual(recent["data"]["downloaded"], 20)
        self.assertEqual(len(recent["data"]["subscriptions"]), 20)
        self.assertFalse(recent["data"]["has_more"])
        self.assertIsNone(recent["data"]["next"])
        first = sdk.rss.activity(limit=7)
        second = sdk.rss.activity(**first["data"]["next"])
        self.assertTrue(first["data"]["has_more"])
        self.assertTrue({item["id"] for item in first["data"]["subscriptions"]}.isdisjoint(item["id"] for item in second["data"]["subscriptions"]))

    def test_list_query_limits_subscriptions_before_entry_aggregation(self):
        source = inspect.getsource(
            rss_repository._query_rss_subscription_safe_summaries
        )
        self.assertIn("WITH selected_items AS", source)
        self.assertIn("FROM selected_items i", source)
        self.assertIn("SELECT COUNT(*) FROM rss_items", source)
        self.assertNotIn("COUNT(*) OVER()", source)
