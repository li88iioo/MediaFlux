"""Media Agent Bangumi 放送日历测试。"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.agent.discovery_actions import bangumi_calendar, calendar_arguments
from app.agent.errors import AgentToolError
from app.discovery.models import (
    DiscoveryPage,
    MediaCard,
    ProviderHealth,
    ProviderRateLimited,
)


def _identity(arguments):
    return dict(arguments)


def _card(index: int = 1, **overrides) -> MediaCard:
    values = {
        "provider": "bangumi",
        "external_id": str(3000 + index),
        "media_type": "tv",
        "title": f"放送动画 {index}",
        "original_title": f"Calendar Anime {index}",
        "year": "2026",
        "overview": "安全简介\x00" * 80,
        "poster_key": "https://image.example/poster?token=secret",
        "backdrop_key": "https://image.example/backdrop?api_key=secret",
        "rating": 8.2,
        "rating_source": "bangumi",
        "release_date": "2026-08-01",
        "bangumi_id": str(3000 + index),
        "weekday": 6,
        "state": "watchlisted",
    }
    values.update(overrides)
    return MediaCard(**values)


class FakeDiscoveryService:
    def __init__(self, page: DiscoveryPage):
        self.page = page
        self.calls: list[tuple[str, str, str, int, dict]] = []

    def list_items(self, provider, category, media_type, page, filters):
        self.calls.append((provider, category, media_type, page, dict(filters)))
        return self.page


class AgentBangumiCalendarTests(unittest.TestCase):
    def test_arguments_default_and_fail_closed(self):
        self.assertEqual(calendar_arguments({}), {"page": 1, "limit": 10})
        self.assertEqual(
            calendar_arguments({"weekday": 7, "page": 2, "limit": 8}),
            {"weekday": 7, "page": 2, "limit": 8},
        )
        invalid = (
            None,
            {"weekday": None},
            {"page": None},
            {"limit": None},
            {"weekday": True},
            {"weekday": "6"},
            {"weekday": 0},
            {"weekday": 8},
            {"page": False},
            {"page": 101},
            {"limit": 0},
            {"limit": 21},
            {"provider": "bangumi"},
            {"category": "today"},
            {"url": "https://example.invalid"},
            {"token": "secret"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                calendar_arguments(arguments)

    def test_disabled_feature_does_not_create_service(self):
        service = Mock()
        with (
            patch("app.agent.discovery_actions.config.get_bool", return_value=False),
            patch(
                "app.agent.discovery_actions.get_discovery_service",
                return_value=service,
            ) as getter,
        ):
            result = bangumi_calendar({"weekday": 6})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        getter.assert_not_called()
        service.list_items.assert_not_called()

    def test_fixed_service_mapping_and_safe_public_payload(self):
        page = DiscoveryPage(
            items=(_card(1), _card(2, weekday=7)),
            page=2,
            has_more=True,
            cached=True,
            stale=True,
            provider=ProviderHealth(name="bangumi", status="degraded", retry_after=12),
        )
        service = FakeDiscoveryService(page)
        with (
            patch("app.agent.discovery_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.discovery_actions.get_discovery_service",
                return_value=service,
            ),
        ):
            result = bangumi_calendar({"weekday": 6, "page": 2, "limit": 1})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "success")
        self.assertEqual(
            service.calls, [("bangumi", "calendar", "tv", 2, {"weekday": "6"})]
        )
        self.assertEqual(result.data["weekday"], 6)
        self.assertEqual(result.data["total"], 2)
        self.assertEqual(result.data["returned"], 1)
        self.assertTrue(result.data["has_more"])
        self.assertTrue(result.data["cached"])
        self.assertTrue(result.data["stale"])
        self.assertEqual(result.data["provider_status"], "degraded")
        self.assertEqual(result.data["retry_after"], 12)
        self.assertEqual(result.data["items"][0]["weekday"], 6)
        serialized = repr(result.to_dict()).casefold()
        for secret in (
            "poster_key",
            "backdrop_key",
            "watchlisted",
            "api_key",
            "token=secret",
            "\\x00",
        ):
            self.assertNotIn(secret, serialized)

    def test_all_week_uses_empty_filters_and_empty_status(self):
        service = FakeDiscoveryService(DiscoveryPage(items=(), page=1))
        with (
            patch("app.agent.discovery_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.discovery_actions.get_discovery_service",
                return_value=service,
            ),
        ):
            result = bangumi_calendar({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "empty")
        self.assertEqual(service.calls, [("bangumi", "calendar", "tv", 1, {})])
        self.assertIsNone(result.data["weekday"])

    def test_provider_and_unexpected_errors_are_generic(self):

        class FailingService:
            def __init__(self, error):
                self.error = error

            def list_items(self, *_args, **_kwargs):
                raise self.error

        for error, expected_retry in (
            (
                ProviderRateLimited(
                    "请求受限", retry_after=42, detail="Bearer should-not-leak"
                ),
                42,
            ),
            (RuntimeError("should-not-leak"), 0),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch("app.agent.discovery_actions.config.get_bool", return_value=True),
                patch(
                    "app.agent.discovery_actions.get_discovery_service",
                    return_value=FailingService(error),
                ),
            ):
                result = bangumi_calendar({"weekday": 6})
            self.assertFalse(result.ok)
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(result.data["retry_after"], expected_retry)
            self.assertNotIn("should-not-leak", repr(result.to_dict()))
