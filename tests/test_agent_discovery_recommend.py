"""Media Agent 外部影视默认推荐列表测试。"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.agent.discovery_actions import recommend_arguments, recommend_discovery
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
        "provider": "tmdb",
        "external_id": str(2000 + index),
        "media_type": "movie",
        "title": f"推荐影片 {index}",
        "original_title": f"Recommended Movie {index}",
        "year": "2026",
        "overview": "安全简介\x00" * 80,
        "poster_key": "https://image.example/poster?token=secret",
        "backdrop_key": "https://image.example/backdrop?api_key=secret",
        "rating": 8.6,
        "rating_source": "tmdb",
        "release_date": "2026-08-01",
        "tmdb_id": str(2000 + index),
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


class AgentDiscoveryRecommendTests(unittest.TestCase):
    def test_arguments_normalize_defaults_and_reject_unsafe_fields(self):
        self.assertEqual(
            recommend_arguments(
                {
                    "provider": " ＤＯＵＢＡＮ ",
                    "media_type": " ＴＶ ",
                    "page": 2,
                    "limit": 8,
                }
            ),
            {"provider": "douban", "media_type": "tv", "page": 2, "limit": 8},
        )
        self.assertEqual(
            recommend_arguments({}),
            {"provider": "tmdb", "media_type": "movie", "page": 1, "limit": 10},
        )
        self.assertEqual(
            recommend_arguments(
                {
                    "provider": "tmdb",
                    "media_type": "tv",
                    "year": "2025",
                    "region": "美国",
                    "genre": "科幻",
                }
            ),
            {
                "provider": "tmdb",
                "media_type": "tv",
                "page": 1,
                "limit": 10,
                "year": "2025",
                "region": "美国",
                "genre": "科幻",
            },
        )
        invalid = (
            None,
            {"provider": "bangumi"},
            {"provider": 1},
            {"media_type": "all"},
            {"media_type": False},
            {"page": True},
            {"page": 101},
            {"limit": 0},
            {"limit": 21},
            {"year": "202A"},
            {"year": "1899"},
            {"region": "\x00"},
            {"genre": "\x00"},
            {"filters": {"sort_by": "vote_average.desc"}},
            {"url": "https://example.invalid"},
            {"token": "secret"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                recommend_arguments(arguments)

    def test_filtered_tmdb_recommendation_uses_discover_filters(self):
        page = DiscoveryPage(
            items=(
                _card(1, media_type="tv", year="2025"),
                _card(2, media_type="tv", year="2024"),
            ),
            page=1,
            has_more=False,
            provider=ProviderHealth(name="tmdb", status="healthy"),
        )
        service = FakeDiscoveryService(page)
        with (
            patch("app.agent.discovery_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.discovery_actions.get_discovery_service",
                return_value=service,
            ),
        ):
            result = recommend_discovery(
                {
                    "provider": "tmdb",
                    "media_type": "tv",
                    "year": "2025",
                    "region": "美国",
                    "genre": "科幻",
                }
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["returned"], 1)
        self.assertEqual(result.data["items"][0]["year"], "2025")
        self.assertEqual(
            result.data["filters"], {"year": "2025", "region": "美国", "genre": "科幻"}
        )
        self.assertEqual(
            service.calls,
            [
                (
                    "tmdb",
                    "discover",
                    "tv",
                    1,
                    {
                        "first_air_date_year": "2025",
                        "with_original_language": "en",
                        "with_genres": "10765",
                    },
                )
            ],
        )

    def test_disabled_feature_does_not_create_or_call_service(self):
        service = Mock()
        with (
            patch("app.agent.discovery_actions.config.get_bool", return_value=False),
            patch(
                "app.agent.discovery_actions.get_discovery_service",
                return_value=service,
            ) as getter,
        ):
            result = recommend_discovery({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        getter.assert_not_called()
        service.list_items.assert_not_called()

    def test_tmdb_and_douban_use_fixed_categories_and_safe_allowlist(self):
        for provider, category in (("tmdb", "discover"), ("douban", "recommend")):
            page = DiscoveryPage(
                items=(_card(1, provider=provider), _card(2, provider=provider)),
                page=2,
                has_more=True,
                cached=True,
                stale=True,
                provider=ProviderHealth(
                    name=provider, status="degraded", retry_after=12
                ),
            )
            service = FakeDiscoveryService(page)
            with (
                self.subTest(provider=provider),
                patch("app.agent.discovery_actions.config.get_bool", return_value=True),
                patch(
                    "app.agent.discovery_actions.get_discovery_service",
                    return_value=service,
                ),
            ):
                result = recommend_discovery(
                    {"provider": provider, "media_type": "movie", "page": 2, "limit": 1}
                )
            self.assertTrue(result.ok)
            self.assertEqual(result.status, "success")
            self.assertEqual(service.calls, [(provider, category, "movie", 2, {})])
            self.assertEqual(result.data["total"], 2)
            self.assertEqual(result.data["returned"], 1)
            self.assertTrue(result.data["has_more"])
            self.assertTrue(result.data["cached"])
            self.assertTrue(result.data["stale"])
            self.assertEqual(result.data["provider_status"], "degraded")
            self.assertEqual(result.data["retry_after"], 12)
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
                result = recommend_discovery({"provider": "tmdb"})
            self.assertFalse(result.ok)
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(result.data["retry_after"], expected_retry)
            self.assertIn(
                result.data["provider_status"],
                {"healthy", "degraded", "disabled", "unavailable", "not_configured"},
            )
            self.assertNotIn("should-not-leak", repr(result.to_dict()))
