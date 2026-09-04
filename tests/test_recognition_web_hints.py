"""自动整理专用 Tavily 标题线索的安全与预算契约。"""
from __future__ import annotations

import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.modules import recognition_web_hints as hints


class RecognitionWebHintsTests(unittest.TestCase):
    def setUp(self) -> None:
        hints.reset_recognition_web_hints_for_tests()

    def tearDown(self) -> None:
        hints.reset_recognition_web_hints_for_tests()

    @staticmethod
    def _get(key: str, default: str = "") -> str:
        return {
            "TAVILY_API_KEY": "tvly-test-key",
            "ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT": "20",
            "TAVILY_CACHE_TTL_SECONDS": "900",
            "TAVILY_TIMEOUT_SECONDS": "10",
        }.get(key, default)

    def test_disabled_unsafe_and_misconfigured_requests_do_not_spend_budget(self):
        reserve = Mock(return_value=True)
        with patch.object(hints, "get_bool", return_value=False):
            result = hints.search_recognition_titles(
                "Movie", reserve_daily=reserve
            )
        self.assertEqual(result.status, "disabled")
        reserve.assert_not_called()

        with patch.object(hints, "get_bool", return_value=True), patch.object(
            hints, "get", side_effect=self._get
        ):
            result = hints.search_recognition_titles(
                "Movie api_key=top-secret-value", reserve_daily=reserve
            )
        self.assertEqual(result.status, "unsafe_input")
        reserve.assert_not_called()

        with patch.object(hints, "get_bool", return_value=True), patch.object(
            hints, "get", return_value=""
        ):
            result = hints.search_recognition_titles(
                "Movie", reserve_daily=reserve
            )
        self.assertEqual(result.status, "misconfigured")
        reserve.assert_not_called()

    def test_successful_title_hint_is_cached_without_second_credit(self):
        reserve = Mock(return_value=True)
        queries: list[str] = []

        async def fake_request(query: str, **_kwargs):
            queries.append(query)
            return hints.RecognitionWebHintResult(
                titles=("Corrected Movie",), attempted=True, status="ok"
            )

        with patch.object(hints, "get_bool", return_value=True), patch.object(
            hints, "get", side_effect=self._get
        ), patch.object(hints, "_request_titles", side_effect=fake_request):
            first = hints.search_recognition_titles(
                "Wrong Movie",
                media_type="tv",
                year="2024",
                reserve_daily=reserve,
                runner=asyncio.run,
            )
            second = hints.search_recognition_titles(
                "Wrong Movie",
                media_type="tv",
                year="2024",
                reserve_daily=reserve,
                runner=asyncio.run,
            )

        self.assertEqual(first.titles, ("Corrected Movie",))
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(queries, ['"Wrong Movie" 2024 TV series'])
        reserve.assert_called_once_with(20)

    def test_concurrent_identical_hint_waits_for_cached_owner_result(self):
        both_reserved = threading.Event()
        reserve_lock = threading.Lock()
        reserve_count = 0
        request_calls = 0
        reserve_daily = Mock(return_value=True)
        original_reserve = hints._singleflight.reserve

        def reserve(key):
            nonlocal reserve_count
            lease = original_reserve(key)
            with reserve_lock:
                reserve_count += 1
                if reserve_count == 2:
                    both_reserved.set()
            return lease

        async def request(*_args, **_kwargs):
            nonlocal request_calls
            request_calls += 1
            if not await asyncio.to_thread(both_reserved.wait, 2):
                raise RuntimeError("concurrent waiter did not reserve")
            return hints.RecognitionWebHintResult(
                titles=("Corrected Movie",), attempted=True, status="ok"
            )

        start = threading.Barrier(2)
        results = [None, None]
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                start.wait(timeout=2)
                results[index] = hints.search_recognition_titles(
                    "Wrong Movie",
                    media_type="movie",
                    year="2024",
                    reserve_daily=reserve_daily,
                    runner=asyncio.run,
                )
            except BaseException as exc:  # pragma: no cover - 线程错误回传
                errors.append(exc)

        with (
            patch.object(hints, "get_bool", return_value=True),
            patch.object(hints, "get", side_effect=self._get),
            patch.object(hints, "_request_titles", side_effect=request),
            patch.object(hints._singleflight, "reserve", side_effect=reserve),
        ):
            threads = [
                threading.Thread(target=worker, args=(index,)) for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(request_calls, 1)
        reserve_daily.assert_called_once_with(20)
        self.assertEqual(
            sorted(result.cached for result in results if result is not None),
            [False, True],
        )
        self.assertTrue(
            all(
                result is not None and result.titles == ("Corrected Movie",)
                for result in results
            )
        )

    def test_known_result_site_suffix_adds_clean_title_variant(self):
        response = SimpleNamespace(
            status_code=200,
            text=json.dumps({
                "results": [
                    {"title": "A Certain Magical Index | MyAnimeList.net"},
                    {"title": "Violet Evergarden - Wikipedia"},
                    {"title": "Title - Untrusted Example Site"},
                ]
            }),
        )

        class Client:
            async def post_json(self, _url, **_kwargs):
                return response

            async def aclose(self):
                return None

        result = asyncio.run(
            hints._request_titles(
                '"A Certain Magical Index" TV series',
                api_key="tvly-test-key",
                client_factory=lambda **_kwargs: Client(),
            )
        )

        self.assertEqual(
            result.titles,
            (
                "A Certain Magical Index | MyAnimeList.net",
                "A Certain Magical Index",
                "Violet Evergarden - Wikipedia",
                "Violet Evergarden",
                "Title - Untrusted Example Site",
            ),
        )

    def test_exhausted_budget_never_starts_network_request(self):
        request = Mock()
        with patch.object(hints, "get_bool", return_value=True), patch.object(
            hints, "get", side_effect=self._get
        ), patch.object(hints, "_request_titles", request):
            result = hints.search_recognition_titles(
                "Movie",
                reserve_daily=lambda _limit: False,
                runner=asyncio.run,
            )
        self.assertEqual(result.status, "budget_exhausted")
        request.assert_not_called()

    def test_fixed_host_request_returns_only_safe_unique_titles(self):
        response = SimpleNamespace(
            status_code=200,
            text=json.dumps({
                "results": [
                    {"title": "Corrected Movie"},
                    {"title": "corrected movie"},
                    {"title": "Movie api_key=top-secret-value"},
                ]
            }),
        )

        class Client:
            def __init__(self):
                self.calls = []
                self.closed = False

            async def post_json(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return response

            async def aclose(self):
                self.closed = True

        client = Client()
        factory = Mock(return_value=client)
        with patch.object(hints, "get", side_effect=self._get):
            result = asyncio.run(
                hints._request_titles(
                    '"Wrong Movie" 2024 movie',
                    api_key="tvly-test-key",
                    client_factory=factory,
                )
            )

        self.assertEqual(result.titles, ("Corrected Movie",))
        self.assertTrue(client.closed)
        factory.assert_called_once()
        options = factory.call_args.kwargs
        self.assertEqual(options["allowed_hosts"], {"api.tavily.com"})
        self.assertEqual(options["max_redirects"], 0)
        _, request_options = client.calls[0]
        self.assertFalse(request_options["json"]["include_answer"])
        self.assertFalse(request_options["json"]["include_raw_content"])
        self.assertFalse(request_options["json"]["include_images"])
        self.assertEqual(request_options["max_redirects"], 0)


if __name__ == "__main__":
    unittest.main()
