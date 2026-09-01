"""Agent 全库缺集巡检变化通知的安全契约测试。"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from app.modules.agent_library_patrol_notifications import (
    build_library_patrol_event,
    build_patrol_result_fingerprint,
    load_patrol_notification_payload,
    serialize_patrol_notification_payload,
    send_library_patrol_notification_result,
)
from app.modules.telegram_notification_center import NotificationPublishResult
from app.modules.telegram_notification_policy import NotificationImportance
from app.notifier import render_event


def _projection() -> dict:
    return {
        "as_of": "2026-08-03",
        "patrol_status": "updates_available",
        "findings_truncated": False,
        "checked_series_count": 8,
        "updates_available_count": 1,
        "missing_episode_count": 2,
        "inconclusive_count": 0,
        "unmapped_series_count": 0,
        "options": [{
            "position": 1,
            "title": "The Show",
            "tmdb_id": "12345",
            "season": 2,
            "missing_count": 2,
            "episode_sample": [3, 4],
        }],
    }


class AgentLibraryPatrolNotificationTests(unittest.TestCase):
    def test_event_contains_only_safe_projection_fields(self):
        projection = _projection()
        event = build_library_patrol_event(projection)
        rendered = render_event(event)

        self.assertEqual(event.layout, "relaxed")
        self.assertIn("- <b>🔎 已核对剧集：</b> 8", rendered)
        self.assertIn("- <b>🧩 已播缺集：</b> 2", rendered)
        self.assertIn("The Show", rendered)
        self.assertIn("S02", rendered)
        self.assertIn("E03", rendered)
        for secret in (
            "private.invalid", "/volume/", "token=", "password=", "server_url",
        ):
            self.assertNotIn(secret, rendered)

    def test_payload_round_trip_fails_closed_for_extra_fields(self):
        payload = serialize_patrol_notification_payload(_projection())
        self.assertEqual(load_patrol_notification_payload(payload), _projection())

        tampered = _projection()
        tampered["server_url"] = "https://private.invalid?token=SECRET"
        with self.assertRaises(ValueError):
            serialize_patrol_notification_payload(tampered)

    def test_fingerprint_ignores_date_title_and_order_but_tracks_business_result(self):
        original = _projection()
        original["options"].append({
            "position": 2,
            "title": "Second Show",
            "tmdb_id": "67890",
            "season": 1,
            "missing_count": 1,
            "episode_sample": [7],
        })
        equivalent = copy.deepcopy(original)
        equivalent["as_of"] = "2026-08-04"
        equivalent["options"][0]["title"] = "另一个安全译名"
        equivalent["options"].reverse()
        for position, item in enumerate(equivalent["options"], start=1):
            item["position"] = position
        self.assertEqual(
            build_patrol_result_fingerprint(original),
            build_patrol_result_fingerprint(equivalent),
        )

        changed = copy.deepcopy(original)
        changed["options"][0]["episode_sample"] = [3, 5]
        self.assertNotEqual(
            build_patrol_result_fingerprint(original),
            build_patrol_result_fingerprint(changed),
        )

    def test_updates_event_exposes_only_fixed_short_patrol_actions(self):
        event = build_library_patrol_event(_projection())
        self.assertEqual(
            [(action.label, action.callback_data) for action in event.actions],
            [
                ("查看巡检摘要", "agp:summary"),
                ("为缺集找资源", "agp:resources"),
            ],
        )
        for action in event.actions:
            self.assertLessEqual(len(action.callback_data.encode("utf-8")), 64)
            for private_value in ("12345", "/volume/", "token", "password"):
                self.assertNotIn(private_value, action.callback_data)


    def test_delivery_omits_actions_when_telegram_agent_is_not_actionable(self):
        captured = []
        with patch(
            "app.modules.agent_library_patrol_notifications.get",
            return_value="",
        ), patch(
            "app.modules.telegram_notification_center.publish_notification_event",
            side_effect=lambda _key, event, **_kwargs: (
                captured.append(event)
                or NotificationPublishResult(True, delivered=True, status="sent")
            ),
        ):
            self.assertTrue(send_library_patrol_notification_result(_projection()).ok)
        self.assertEqual(captured[0].actions, ())

    def test_delivery_keeps_actions_for_configured_telegram_agent(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "-100",
            "TG_AGENT_ALLOWED_USER_IDS": "200,201",
        }
        captured = []
        with patch(
            "app.modules.agent_library_patrol_notifications.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.modules.telegram_notification_center.publish_notification_event",
            side_effect=lambda _key, event, **_kwargs: (
                captured.append(event)
                or NotificationPublishResult(True, delivered=True, status="sent")
            ),
        ):
            self.assertTrue(send_library_patrol_notification_result(_projection()).ok)
        self.assertEqual(
            [action.callback_data for action in captured[0].actions],
            ["agp:summary", "agp:resources"],
        )

    def test_up_to_date_event_has_no_candidate_lines_or_actions(self):
        projection = _projection()
        projection.update({
            "patrol_status": "up_to_date",
            "updates_available_count": 0,
            "missing_episode_count": 0,
            "options": [],
        })
        event = build_library_patrol_event(projection)
        self.assertEqual(event.lines, ())
        self.assertEqual(event.actions, ())
        self.assertEqual(event.layout, "relaxed")
        self.assertIn("恢复正常", str(event.title))

    def test_recovery_is_result_while_available_updates_remain_actionable(self):
        captured = []

        def publish(_key, _event, **kwargs):
            captured.append(kwargs["importance"])
            return NotificationPublishResult(True, delivered=True, status="sent")

        recovered = _projection()
        recovered.update({
            "patrol_status": "up_to_date",
            "updates_available_count": 0,
            "missing_episode_count": 0,
            "options": [],
        })
        with patch(
            "app.modules.agent_library_patrol_notifications.get",
            return_value="",
        ), patch(
            "app.modules.telegram_notification_center.publish_notification_event",
            side_effect=publish,
        ):
            self.assertTrue(send_library_patrol_notification_result(_projection()).ok)
            self.assertTrue(send_library_patrol_notification_result(recovered).ok)

        self.assertEqual(captured, [
            NotificationImportance.ACTION,
            NotificationImportance.RESULT,
        ])


if __name__ == "__main__":
    unittest.main()
