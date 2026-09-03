"""Media Agent 外部影视探索搜索测试。"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.agent.discovery_actions import search_arguments, search_discovery
from app.agent.errors import AgentToolError
from app.discovery.models import MediaCard
from app.discovery.search import DiscoverySearchResult

_SECRET_POSTER = "https://image.example/private/poster?api_key=secret"
_SECRET_BACKDROP = "https://image.example/private/backdrop?token=secret"


def _identity(arguments):
    return dict(arguments)


def _card(index: int = 1, **overrides) -> MediaCard:
    values = {
        "provider": "tmdb",
        "external_id": str(1000 + index),
        "media_type": "movie",
        "title": f"示例影片 {index}",
        "original_title": f"Demo Movie {index}",
        "year": "2026",
        "overview": "剧情简介" * 180,
        "poster_key": _SECRET_POSTER,
        "backdrop_key": _SECRET_BACKDROP,
        "rating": 8.2,
        "rating_source": "tmdb",
        "release_date": "2026-08-01",
        "tmdb_id": str(1000 + index),
    }
    values.update(overrides)
    return MediaCard(**values)


def _result(
    *, items=(), attempted=("tmdb",), succeeded=("tmdb",), errors=(), has_more=False
) -> DiscoverySearchResult:
    return DiscoverySearchResult(
        query="沙丘2",
        page=1,
        items=tuple(items),
        has_more=has_more,
        providers_attempted=tuple(attempted),
        providers_succeeded=tuple(succeeded),
        errors=tuple(errors),
    )


class FakeDiscoverySearchService:
    def __init__(self, result: DiscoverySearchResult):
        self.result = result
        self.calls: list[tuple[str, int, list[str] | None]] = []

    def search(self, query: str, page: int, providers):
        self.calls.append((query, page, providers))
        return self.result


class AgentDiscoverySearchTests(unittest.TestCase):
    def test_arguments_normalize_and_reject_unsafe_fields(self):
        self.assertEqual(
            search_arguments(
                {
                    "query": "  沙丘２  ",
                    "page": 2,
                    "providers": ["TMDB", "tmdb", "Bangumi"],
                    "limit": 10,
                }
            ),
            {
                "query": "沙丘2",
                "page": 2,
                "providers": ["tmdb", "bangumi"],
                "limit": 10,
            },
        )
        invalid = (
            {},
            {"query": ""},
            {"query": "x\ny"},
            {"query": "x", "page": True},
            {"query": "x", "page": 101},
            {"query": "x", "limit": 0},
            {"query": "x", "providers": []},
            {"query": "x", "providers": ["unknown"]},
            {"query": "x", "url": "https://example.invalid"},
            {"query": "x", "token": "secret"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                search_arguments(arguments)

    def test_disabled_feature_does_not_create_or_call_service(self):
        service = Mock()
        with (
            patch("app.agent.discovery_actions.config.get_bool", return_value=False),
            patch(
                "app.agent.discovery_actions.get_discovery_search_service",
                return_value=service,
            ) as getter,
        ):
            result = search_discovery({"query": "沙丘2"})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        getter.assert_not_called()
        service.search.assert_not_called()

    def test_success_uses_safe_allowlist_and_limit(self):
        service = FakeDiscoverySearchService(
            _result(items=(_card(1), _card(2)), has_more=True)
        )
        with (
            patch("app.agent.discovery_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.discovery_actions.get_discovery_search_service",
                return_value=service,
            ),
        ):
            result = search_discovery(
                {"query": "沙丘2", "page": 1, "providers": ["tmdb"], "limit": 1}
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "success")
        self.assertEqual(service.calls, [("沙丘2", 1, ["tmdb"])])
        self.assertEqual(result.data["total"], 2)
        self.assertEqual(result.data["returned"], 1)
        self.assertEqual(len(result.data["items"][0]["overview"]), 500)
        serialized = repr(result.to_dict())
        self.assertNotIn("poster_key", serialized)
        self.assertNotIn("backdrop_key", serialized)
        self.assertNotIn(_SECRET_POSTER, serialized)
        self.assertNotIn(_SECRET_BACKDROP, serialized)

    def test_error_retry_after_is_bounded(self):
        result = _result(
            attempted=("tmdb",),
            succeeded=(),
            errors=(
                {"provider": "tmdb", "code": "rate_limited", "retry_after": 999999},
            ),
        )
        service = FakeDiscoverySearchService(result)
        with (
            patch("app.agent.discovery_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.discovery_actions.get_discovery_search_service",
                return_value=service,
            ),
        ):
            response = search_discovery({"query": "沙丘2"})
        self.assertEqual(response.data["errors"][0]["retry_after"], 86400)

    def test_partial_empty_and_full_failure_semantics(self):
        cases = (
            (
                _result(
                    items=(_card(),),
                    attempted=("tmdb", "douban"),
                    succeeded=("tmdb",),
                    errors=(
                        {
                            "provider": "douban",
                            "code": "unavailable",
                            "message": "token=message-should-not-leak",
                            "retry_after": 0,
                            "detail": "token=should-not-leak",
                        },
                    ),
                ),
                True,
                "partial",
            ),
            (_result(items=()), True, "empty"),
            (
                _result(
                    items=(),
                    attempted=("tmdb",),
                    succeeded=(),
                    errors=(
                        {
                            "provider": "tmdb",
                            "code": "authentication",
                            "message": "api_key=message-should-not-leak",
                            "retry_after": 0,
                            "detail": "api_key=should-not-leak",
                        },
                    ),
                ),
                False,
                "unavailable",
            ),
        )
        for raw, ok, status in cases:
            with (
                self.subTest(status=status),
                patch("app.agent.discovery_actions.config.get_bool", return_value=True),
                patch(
                    "app.agent.discovery_actions.get_discovery_search_service",
                    return_value=FakeDiscoverySearchService(raw),
                ),
            ):
                result = search_discovery({"query": "沙丘2"})
            self.assertEqual(result.ok, ok)
            self.assertEqual(result.status, status)
            self.assertNotIn("should-not-leak", repr(result.to_dict()))
