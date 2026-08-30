from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import database
from app.discovery.cache import DiscoveryCache
from app.discovery.models import (
    DiscoveryPage,
    MediaCard,
    ProviderHealth,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderUnavailable,
)
from app.discovery.registry import (
    ProviderRegistry,
    list_filter_definitions,
    list_section_definitions,
    validate_filters,
    validate_request,
)
from app.discovery.service import (
    DiscoveryService,
    get_discovery_service,
    shutdown_discovery_service,
)


class FakeProvider:
    name = "tmdb"

    def __init__(self):
        self.calls = 0
        self.detail_calls = 0
        self.close_calls = 0
        self.closed = threading.Event()
        self.error = None

    def list_items(self, category, media_type, page, filters):
        self.calls += 1
        if self.error:
            raise self.error
        return DiscoveryPage(
            items=[MediaCard(provider="tmdb", external_id=str(page), media_type=media_type, title=f"Page {page}")],
            page=page,
            has_more=page < 2,
            provider=ProviderHealth(name="tmdb"),
        )

    def get_detail(self, external_id, media_type):
        self.detail_calls += 1
        return MediaCard(provider="tmdb", external_id=external_id, media_type=media_type, title="Detail")

    def close(self):
        self.close_calls += 1
        self.closed.set()

    def health(self):
        return ProviderHealth(name="tmdb", status="healthy")


class DiscoveryRegistryTests(unittest.TestCase):
    def test_section_keys_are_unique_and_compatible(self):
        sections = list_section_definitions(douban_enabled=True)
        keys = [item["key"] for item in sections]
        self.assertEqual(len(keys), len(set(keys)))
        for item in sections:
            self.assertIn(item["provider"], {"tmdb", "douban", "bangumi"})
            self.assertIn(item["media_type"], {"movie", "tv", "all"})
            self.assertTrue(item["category"])

    def test_every_section_triple_is_unique_and_valid(self):
        sections = list_section_definitions(douban_enabled=True)
        triples = [(item["provider"], item["category"], item["media_type"]) for item in sections]
        self.assertEqual(len(triples), len(set(triples)))
        for triple in triples:
            self.assertEqual(validate_request(*triple), triple)
        self.assertEqual(validate_request("TMDB", "TRENDING_WEEK", "ALL"), ("tmdb", "trending_week", "all"))
        with self.assertRaises(ValueError):
            validate_request("tmdb", "trending_week", "movie")

    def test_douban_sections_report_disabled_without_feature_flag(self):
        sections = list_section_definitions(douban_enabled=False)
        douban = [item for item in sections if item["provider"] == "douban"]
        self.assertTrue(douban)
        self.assertTrue(all(item["enabled"] is False for item in douban))

    def test_douban_sections_default_enabled_when_config_is_absent(self):
        with patch(
            "app.discovery.registry.config.get",
            side_effect=lambda key, default="": default,
        ):
            sections = list_section_definitions()

        douban = [item for item in sections if item["provider"] == "douban"]
        self.assertTrue(douban)
        self.assertTrue(all(item["enabled"] is True for item in douban))

    def test_douban_sections_respect_explicit_disabled_config(self):
        with patch("app.discovery.registry.config.get", return_value="0"):
            sections = list_section_definitions()

        douban = [item for item in sections if item["provider"] == "douban"]
        self.assertTrue(douban)
        self.assertTrue(all(item["enabled"] is False for item in douban))

    def test_filter_validation_rejects_unknown_or_invalid_values(self):
        valid = validate_filters(" TMDB ", " DISCOVER ", " MOVIE ", {"with_genres": "16, 35", "with_original_language": "JA"})
        self.assertEqual(valid, {"with_genres": "16,35", "with_original_language": "ja", "sort_by": "popularity.desc"})
        definitions = list_filter_definitions(" TMDB ", " MOVIE ")
        self.assertEqual(definitions["defaults"]["sort_by"], "popularity.desc")
        by_key = {item["key"]: item for item in definitions["filters"]}
        self.assertEqual(by_key["with_genres"]["label"], "类型")
        self.assertIn(
            {"value": "28", "label": "动作"},
            by_key["with_genres"]["options"],
        )
        self.assertIn(
            {"value": "99", "label": "纪录片"},
            by_key["with_genres"]["options"],
        )
        self.assertIn(
            {"value": "zh", "label": "中文"},
            by_key["with_original_language"]["options"],
        )
        self.assertIn(
            {"value": "popularity.desc", "label": "热度从高到低"},
            by_key["sort_by"]["options"],
        )
        self.assertEqual(
            validate_filters(
                "tmdb", "discover", "tv",
                {"first_air_date_year": "2025", "with_original_language": "EN"},
            ),
            {
                "first_air_date_year": "2025",
                "with_original_language": "en",
                "sort_by": "popularity.desc",
            },
        )
        with self.assertRaises(ValueError):
            validate_filters("tmdb", "discover", "movie", {"api_key": "leak"})
        with self.assertRaises(ValueError):
            validate_filters("tmdb", "discover", "movie", {"first_air_date_year": "2025"})
        with self.assertRaises(ValueError):
            validate_filters("tmdb", "discover", "tv", {"primary_release_year": "2025"})
        with self.assertRaises(ValueError):
            validate_filters("tmdb", "discover", "tv", {"first_air_date_year": "25"})
        with self.assertRaises(ValueError):
            validate_filters("bangumi", "calendar", "tv", {"weekday": "8"})
        self.assertEqual(validate_filters("bangumi", "calendar", "tv", {}), {})
        self.assertEqual(list_filter_definitions("bangumi", "tv")["defaults"], {"weekday": ""})
        self.assertEqual(
            list_filter_definitions("tmdb", "movie")["defaults"],
            {"with_genres": "", "with_original_language": "", "sort_by": "popularity.desc"},
        )
        self.assertEqual(
            list_filter_definitions("douban", "movie")["defaults"],
            {"sort": "recommend", "tags": ""},
        )
        self.assertEqual(
            validate_filters(
                "douban", "recommend", "movie", {"sort": "RANK"}
            ),
            {"sort": "rank"},
        )
        for legacy_sort in ("T", "U", "S", "R"):
            with self.subTest(sort=legacy_sort), self.assertRaises(ValueError):
                validate_filters(
                    "douban", "recommend", "movie", {"sort": legacy_sort}
                )

    def test_empty_default_filters_are_skipped_for_initial_tmdb_and_douban_requests(self):
        cases = (
            ("tmdb", "discover", "movie", {"sort_by": "popularity.desc"}),
            ("tmdb", "discover", "tv", {"sort_by": "popularity.desc"}),
            ("douban", "recommend", "movie", {"sort": "recommend"}),
            ("douban", "recommend", "tv", {"sort": "recommend"}),
        )

        for provider, category, media_type, expected in cases:
            with self.subTest(provider=provider, media_type=media_type):
                defaults = list_filter_definitions(provider, media_type)["defaults"]
                self.assertEqual(
                    validate_filters(provider, category, media_type, defaults),
                    expected,
                )

    def test_registry_close_is_idempotent_and_rejects_future_access(self):
        provider = FakeProvider()
        registry = ProviderRegistry({"tmdb": provider, "alias": provider})

        registry.close()
        registry.close()

        self.assertEqual(provider.close_calls, 1)
        with self.assertRaises(ProviderUnavailable):
            registry.get("tmdb")
        with self.assertRaises(ProviderUnavailable):
            registry.register(provider)


class DiscoveryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "service.db"
        self.db_patch = patch("app.database.DB_PATH", self.db_path)
        self.db_patch.start()
        database.init_db()
        self.now = datetime(2026, 7, 25, 12, 0, 0)
        self.cache = DiscoveryCache(clock=lambda: self.now)
        self.provider = FakeProvider()
        self.registry = ProviderRegistry({"tmdb": self.provider})
        self.service = DiscoveryService(
            registry=self.registry,
            cache=self.cache,
            cache_ttl_seconds=60,
            stale_ttl_seconds=300,
            refresh_submit=lambda fn: fn(),
        )

    def tearDown(self):
        self.service.shutdown(timeout_seconds=0.5)
        self.db_patch.stop()
        self.temp.cleanup()

    def test_fresh_cache_hit_skips_provider_and_marks_cached(self):
        first = self.service.list_items("tmdb", "popular", "movie", 1, {})
        second = self.service.list_items("tmdb", "popular", "movie", 1, {})
        self.assertEqual(self.provider.calls, 1)
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertFalse(second.stale)

    def test_stale_cache_returns_old_page_and_refreshes_once(self):
        self.service.list_items("tmdb", "popular", "movie", 1, {})
        self.now += timedelta(seconds=61)
        stale = self.service.list_items("tmdb", "popular", "movie", 1, {})
        self.assertTrue(stale.cached)
        self.assertTrue(stale.stale)
        self.assertEqual(self.provider.calls, 2)
        refreshed = self.service.list_items("tmdb", "popular", "movie", 1, {})
        self.assertFalse(refreshed.stale)
        self.assertEqual(self.provider.calls, 2)

    def test_stale_payload_survives_provider_failure_on_subsequent_read(self):
        self.service.list_items("tmdb", "popular", "movie", 1, {})
        self.now += timedelta(seconds=61)
        self.provider.error = ProviderUnavailable("upstream down")
        page = self.service.list_items("tmdb", "popular", "movie", 1, {})
        second = self.service.list_items("tmdb", "popular", "movie", 1, {})
        self.assertTrue(page.stale)
        self.assertTrue(second.stale)
        self.assertEqual(second.items[0].title, "Page 1")

    def test_expired_failure_raises_structured_error(self):
        self.service.list_items("tmdb", "popular", "movie", 1, {})
        self.now += timedelta(seconds=301)
        self.provider.error = ProviderUnavailable("upstream down")
        with self.assertRaises(ProviderUnavailable):
            self.service.list_items("tmdb", "popular", "movie", 1, {})
        with self.assertRaises(ProviderUnavailable):
            self.service.list_items("tmdb", "popular", "movie", 1, {})
        self.assertEqual(self.provider.calls, 2)

    def test_concurrent_provider_failure_is_loaded_once_and_shared_from_error_cache(self):
        started = threading.Event()
        release = threading.Event()
        initial_reads = threading.Barrier(2)
        read_counts: dict[int, int] = {}
        read_counts_lock = threading.Lock()
        original_get = self.cache.get

        def synchronized_get(key):
            thread_id = threading.get_ident()
            with read_counts_lock:
                read_counts[thread_id] = read_counts.get(thread_id, 0) + 1
                is_initial_read = read_counts[thread_id] == 1
            lookup = original_get(key)
            if is_initial_read:
                initial_reads.wait(2)
            return lookup

        def blocking_failure(category, media_type, page, filters):
            self.provider.calls += 1
            started.set()
            release.wait(2)
            raise ProviderUnavailable("upstream down")

        self.provider.list_items = blocking_failure
        request_barrier = threading.Barrier(3)

        def request_page():
            request_barrier.wait(2)
            with self.assertRaises(ProviderUnavailable) as raised:
                self.service.list_items("tmdb", "popular", "movie", 1, {})
            return raised.exception

        with patch.object(self.cache, "get", side_effect=synchronized_get):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(request_page) for _ in range(2)]
                request_barrier.wait(2)
                self.assertTrue(started.wait(1))
                release.set()
                errors = [future.result(2) for future in futures]

        self.assertEqual(self.provider.calls, 1)
        self.assertTrue(all(error.code == "unavailable" for error in errors))

    def test_concurrent_stale_reads_trigger_one_provider_refresh(self):
        self.service.list_items("tmdb", "popular", "movie", 1, {})
        self.now += timedelta(seconds=61)
        submitted = []
        submitted_lock = threading.Lock()

        def submit(refresh):
            with submitted_lock:
                submitted.append(refresh)

        refresh_now = [0.0]
        service = DiscoveryService(
            registry=self.registry, cache=self.cache, cache_ttl_seconds=60, stale_ttl_seconds=300,
            refresh_submit=submit, refresh_clock=lambda: refresh_now[0],
        )
        request_barrier = threading.Barrier(5)

        def request_page():
            request_barrier.wait(2)
            return service.list_items("tmdb", "popular", "movie", 1, {})

        with ThreadPoolExecutor(max_workers=4) as request_pool:
            futures = [request_pool.submit(request_page) for _ in range(4)]
            request_barrier.wait(2)
            pages = [future.result(1) for future in futures]

        self.assertTrue(all(page.stale for page in pages))
        self.assertEqual(len(submitted), 1)

        self.provider.error = ProviderUnavailable("refresh failed")
        submitted[0]()
        retry = service.list_items("tmdb", "popular", "movie", 1, {})
        self.assertTrue(retry.stale)
        self.assertEqual(len(submitted), 1)
        self.assertEqual(self.provider.calls, 2)

        refresh_now[0] = 31.0
        after_cooldown = service.list_items("tmdb", "popular", "movie", 1, {})
        self.assertTrue(after_cooldown.stale)
        self.assertEqual(len(submitted), 2)

    def test_shutdown_does_not_submit_stale_refresh_to_closed_executor(self):
        self.service.list_items("tmdb", "popular", "movie", 1, {})
        self.now += timedelta(seconds=61)
        service = DiscoveryService(
            registry=self.registry, cache=self.cache,
            cache_ttl_seconds=60, stale_ttl_seconds=300,
        )
        service.shutdown()

        page = service.list_items("tmdb", "popular", "movie", 1, {})

        self.assertTrue(page.stale)
        self.assertEqual(self.provider.calls, 1)

    def test_shutdown_is_bounded_and_late_refresh_cannot_publish_state(self):
        service = DiscoveryService(
            registry=self.registry, cache=self.cache, cache_ttl_seconds=60,
            stale_ttl_seconds=300,
        )
        service.list_items("tmdb", "popular", "movie", 1, {})
        initial = self.cache.get(
            self.cache.make_key("tmdb", "popular", "movie", 1, {})
        ).payload
        self.now += timedelta(seconds=61)
        started = threading.Event()
        release = threading.Event()

        def blocking_refresh(category, media_type, page, filters):
            del category, filters
            self.provider.calls += 1
            started.set()
            release.wait(2)
            return DiscoveryPage(
                items=[MediaCard(
                    provider="tmdb", external_id=str(page), media_type=media_type,
                    title="Late refresh",
                )],
                page=page, has_more=False, provider=ProviderHealth(name="tmdb"),
            )

        self.provider.list_items = blocking_refresh
        stale = service.list_items("tmdb", "popular", "movie", 1, {})
        self.assertTrue(stale.stale)
        self.assertTrue(started.wait(1))

        shutdown_started = time.monotonic()
        self.assertFalse(service.shutdown(timeout_seconds=0.02))
        self.assertLess(time.monotonic() - shutdown_started, 0.5)
        self.assertFalse(self.provider.closed.is_set())

        release.set()
        self.assertTrue(self.provider.closed.wait(1))
        current = self.cache.get(
            self.cache.make_key("tmdb", "popular", "movie", 1, {})
        ).payload
        self.assertEqual(current, initial)
        self.assertFalse(service._pending_refreshes)
        self.assertFalse(service._refresh_cooldowns)
        self.assertTrue(service.shutdown(timeout_seconds=0.5))
        self.assertEqual(self.provider.close_calls, 1)

    def test_shutdown_timeout_is_not_blocked_by_synchronous_submitter(self):
        submit_started = threading.Event()
        release_submit = threading.Event()

        def blocking_submit(callback):
            submit_started.set()
            release_submit.wait(2)
            callback()

        service = DiscoveryService(
            registry=self.registry, cache=self.cache, cache_ttl_seconds=60,
            stale_ttl_seconds=300, refresh_submit=blocking_submit,
        )
        service.list_items("tmdb", "popular", "movie", 1, {})
        self.now += timedelta(seconds=61)
        pages = []
        request = threading.Thread(
            target=lambda: pages.append(
                service.list_items("tmdb", "popular", "movie", 1, {})
            )
        )
        request.start()
        self.assertTrue(submit_started.wait(1))

        shutdown_started = time.monotonic()
        self.assertFalse(service.shutdown(timeout_seconds=0.02))
        self.assertLess(time.monotonic() - shutdown_started, 0.5)

        release_submit.set()
        request.join(1)
        self.assertFalse(request.is_alive())
        self.assertTrue(pages[0].stale)
        self.assertEqual(self.provider.calls, 1)
        self.assertTrue(self.provider.closed.wait(1))
        self.assertTrue(service.shutdown(timeout_seconds=0.5))

    def test_closed_service_rejects_new_network_work_without_default_executor(self):
        scraper_called = threading.Event()
        scraper = SimpleNamespace(
            search_candidates=lambda *_args, **_kwargs: scraper_called.set() or []
        )
        service = DiscoveryService(
            registry=self.registry, cache=self.cache, scraper_factory=lambda: scraper,
        )
        self.assertTrue(service.shutdown(timeout_seconds=0.5))

        with self.assertRaises(ProviderUnavailable):
            service.list_items("tmdb", "popular", "movie", 99, {})
        with self.assertRaises(ProviderUnavailable):
            service.get_detail("tmdb", "movie", "99")
        with self.assertRaises(ProviderUnavailable):
            asyncio.run(
                service.map_to_tmdb_async(
                    "douban", "closed-1", "movie", "Closed", "2026"
                )
            )

        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.provider.detail_calls, 0)
        self.assertFalse(scraper_called.is_set())
        self.assertEqual(self.provider.close_calls, 1)

    def test_error_cache_preserves_structured_provider_error(self):
        self.provider.error = ProviderRateLimited("slow down", retry_after=42)
        with self.assertRaises(ProviderRateLimited) as first:
            self.service.list_items("tmdb", "popular", "movie", 1, {})
        with self.assertRaises(ProviderRateLimited) as second:
            self.service.list_items("tmdb", "popular", "movie", 1, {})
        self.assertEqual((first.exception.retry_after, second.exception.retry_after), (42, 42))
        self.assertEqual(self.provider.calls, 1)

    def test_error_cache_preserves_invalid_response_type_and_http_semantics(self):
        self.provider.error = ProviderInvalidResponse("bad payload")

        with self.assertRaises(ProviderInvalidResponse) as first:
            self.service.list_items("tmdb", "popular", "movie", 1, {})
        with self.assertRaises(ProviderInvalidResponse) as second:
            self.service.list_items("tmdb", "popular", "movie", 1, {})

        self.assertEqual(
            [(error.exception.code, error.exception.status_code) for error in (first, second)],
            [("invalid_response", 502), ("invalid_response", 502)],
        )
        self.assertEqual(self.provider.calls, 1)

    def test_background_refresh_does_not_query_watchlist_for_discarded_result(self):
        self.service.list_items("tmdb", "popular", "movie", 1, {})
        self.now += timedelta(seconds=61)
        with patch("app.discovery.service.database.list_media_watchlist_keys", wraps=database.list_media_watchlist_keys) as lookup:
            self.service.list_items("tmdb", "popular", "movie", 1, {})
        lookup.assert_called_once()

    def test_page_and_filter_validation_happens_before_provider(self):
        with self.assertRaises(ValueError):
            self.service.list_items("tmdb", "popular", "movie", 0, {})
        with self.assertRaises(ValueError):
            self.service.list_items("tmdb", "discover", "movie", 1, {"bad": "x"})
        self.assertEqual(self.provider.calls, 0)

    def test_watchlist_state_is_attached_with_one_batch_lookup(self):
        database.add_media_watchlist("tmdb", "1", "movie", "Page 1")
        with patch("app.discovery.service.database.list_media_watchlist_keys", wraps=database.list_media_watchlist_keys) as lookup:
            page = self.service.list_items("tmdb", "popular", "movie", 1, {})
        self.assertEqual(page.items[0].state, "watchlisted")
        lookup.assert_called_once()

    def test_watchlist_add_remove_identity_and_order_are_stable(self):
        first = MediaCard(provider="tmdb", external_id="10", media_type="movie", title="Movie", year="2026")
        second = MediaCard(provider="douban", external_id="10", media_type="movie", title="Douban Movie", year="2025")
        self.service.add_watchlist(first)
        self.service.add_watchlist(first)
        self.service.add_watchlist(second)
        listed = self.service.list_watchlist()
        self.assertEqual([(item["provider"], item["external_id"]) for item in listed], [("douban", "10"), ("tmdb", "10")])
        self.assertTrue(self.service.remove_watchlist("tmdb", "movie", "10"))
        self.assertFalse(self.service.remove_watchlist("tmdb", "movie", "10"))
        self.assertEqual(self.service.list_watchlist()[0]["provider"], "douban")

    def test_normalized_identifiers_are_used_for_registry_lookup(self):
        page = self.service.list_items(" TMDB ", " POPULAR ", " MOVIE ", 1, {})
        self.assertEqual(page.provider.name, "tmdb")

    def test_singleton_executor_can_shutdown_and_rebuild(self):
        shutdown_discovery_service()
        first = get_discovery_service()
        shutdown_discovery_service()
        second = get_discovery_service()
        self.assertIsNot(first, second)
        shutdown_discovery_service()


    def test_async_mapping_runs_in_discovery_bounded_executor(self):
        import asyncio
        import threading

        thread_names: list[str] = []
        scraper = SimpleNamespace(
            search_candidates=lambda *_args, **_kwargs: (
                thread_names.append(threading.current_thread().name) or []
            )
        )
        service = DiscoveryService(
            registry=self.registry,
            cache=self.cache,
            scraper_factory=lambda: scraper,
        )
        try:
            result = asyncio.run(
                service.map_to_tmdb_async("douban", "async-1", "movie", "Movie", "2026")
            )
        finally:
            service.shutdown()

        self.assertFalse(result["confirmed"])
        self.assertTrue(thread_names[0].startswith("discovery-refresh"))

    def test_confirmed_mapping_is_reused_without_search(self):
        database.upsert_media_external_id("douban", "7", "movie", "550", "Movie", "1999", 1.0, True)
        scraper = SimpleNamespace(search_candidates=lambda *args, **kwargs: self.fail("search should not run"))
        service = DiscoveryService(registry=self.registry, cache=self.cache, scraper_factory=lambda: scraper)
        result = service.map_to_tmdb("douban", "7", "movie", "Movie", "1999")
        self.assertEqual((result["tmdb_id"], result["confirmed"]), ("550", True))

    def test_legacy_unconfirmed_mapping_does_not_hide_new_candidates(self):
        database.upsert_media_external_id(
            "douban", "legacy-tentative", "movie", "550", "旧自动候选",
            "1999", 0.96, False,
        )
        scraper = SimpleNamespace(
            search_candidates=lambda *args, **kwargs: [SimpleNamespace(
                tmdb_id="551", title="可确认候选", year="2000",
                score=0.91, media_type="movie",
            )]
        )
        service = DiscoveryService(
            registry=self.registry, cache=self.cache, scraper_factory=lambda: scraper
        )

        result = service.lookup_tmdb_mapping(
            "douban", "legacy-tentative", "movie", "Movie", "1999"
        )

        self.assertFalse(result["confirmed"])
        self.assertEqual(result["tmdb_id"], "")
        self.assertEqual(result["candidates"][0]["tmdb_id"], "551")

    def test_explicit_confirmation_does_not_overwrite_existing_confirmed_mapping(self):
        database.upsert_media_external_id(
            "douban", "confirmed-7", "movie", "550", "Existing Movie",
            "1999", 1.0, True,
        )
        scraper = SimpleNamespace(
            search_candidates=lambda *args, **kwargs: self.fail("search should not run"),
            match_from_tmdb=lambda *args, **kwargs: self.fail("verify should not run"),
        )
        service = DiscoveryService(
            registry=self.registry, cache=self.cache, scraper_factory=lambda: scraper
        )

        result = service.map_to_tmdb(
            "douban", "confirmed-7", "movie", "Movie", "1999",
            confirmed_tmdb_id="551", confirmed_title="Other", confirmed_year="2000",
        )

        stored = database.get_media_external_id("douban", "confirmed-7", "movie")
        self.assertEqual((result["tmdb_id"], result["confirmed"]), ("550", True))
        self.assertEqual(
            (stored["tmdb_id"], stored["title"], stored["year"], stored["confirmed"]),
            ("550", "Existing Movie", "1999", 1),
        )

    def test_explicit_confirmation_corrects_existing_unconfirmed_mapping(self):
        database.upsert_media_external_id(
            "douban", "7", "movie", "550", "Wrong Movie", "1998", 0.96, False,
        )
        scraper = SimpleNamespace(
            search_candidates=lambda *args, **kwargs: self.fail("search should not run"),
            match_from_tmdb=lambda tmdb_id, media_type: SimpleNamespace(
                need_confirm=False, tmdb_id=tmdb_id, media_type=media_type,
                title="Correct Movie", year="2000",
            ),
        )
        service = DiscoveryService(registry=self.registry, cache=self.cache, scraper_factory=lambda: scraper)

        result = service.map_to_tmdb(
            "douban", "7", "movie", "Movie", "1999",
            confirmed_tmdb_id="551", confirmed_title="Correct Movie", confirmed_year="2000",
        )

        stored = database.get_media_external_id("douban", "7", "movie")
        self.assertEqual((result["tmdb_id"], result["confirmed"]), ("551", True))
        self.assertEqual(
            (stored["tmdb_id"], stored["title"], stored["year"], stored["confirmed"]),
            ("551", "Correct Movie", "2000", 1),
        )

    def test_mapping_candidate_lookup_never_persists_without_confirmation(self):
        high = SimpleNamespace(tmdb_id="550", title="Fight Club", year="1999", score=0.96, media_type="movie")
        low = SimpleNamespace(tmdb_id="551", title="Maybe", year="2000", score=0.72, media_type="movie")
        candidates = [high]
        scraper = SimpleNamespace(search_candidates=lambda *args, **kwargs: list(candidates))
        service = DiscoveryService(registry=self.registry, cache=self.cache, scraper_factory=lambda: scraper)
        result = service.map_to_tmdb("douban", "8", "movie", "Fight Club", "1999")
        self.assertEqual((result["tmdb_id"], result["confirmed"]), ("", False))
        self.assertEqual(result["candidates"][0]["tmdb_id"], "550")
        self.assertIsNone(database.get_media_external_id("douban", "8", "movie"))

        candidates[:] = [low]
        result = service.map_to_tmdb("douban", "9", "movie", "Maybe", "2000")
        self.assertEqual(result["tmdb_id"], "")
        self.assertEqual(result["candidates"][0]["tmdb_id"], "551")
        self.assertIsNone(database.get_media_external_id("douban", "9", "movie"))


if __name__ == "__main__":
    unittest.main()
