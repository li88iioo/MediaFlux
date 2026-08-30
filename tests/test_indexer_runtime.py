from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.indexers import runtime


class FakeRegistry:
    def ids(self):
        return ("nyaa", "sukebei", "mikan", "btbtla", "1lou", "tpb")

    def enabled_ids(self):
        return ("nyaa",)


class IndexerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        runtime.unbind_indexer_event_loop()
        runtime._service = None

    def test_build_service_adds_sukebei_only_once_when_enabled(self):
        registry = FakeRegistry()

        def get_value(key, default=""):
            if key == "INDEXER_ENABLED_SITES":
                return "nyaa"
            return default

        def get_bool(key, default=False):
            if key == "INDEXER_SUKEBEI_ENABLED":
                return True
            return default

        with patch("app.indexers.runtime.config.get", side_effect=get_value), patch(
            "app.indexers.runtime.config.get_int",
            side_effect=lambda _key, default: default,
        ), patch(
            "app.indexers.runtime.config.get_bool", side_effect=get_bool,
        ), patch(
            "app.indexers.runtime.build_default_registry", return_value=registry,
        ):
            service = runtime._build_service()

        self.assertEqual(service.enabled_site_ids, frozenset({"nyaa", "sukebei"}))

    def test_build_service_passes_configured_user_agent_to_registry(self):
        registry = FakeRegistry()

        def get_value(key, default=""):
            if key == "INDEXER_USER_AGENT":
                return "MediaFlux/Test"
            return default

        with patch("app.indexers.runtime.config.get", side_effect=get_value), patch(
            "app.indexers.runtime.config.get_int",
            side_effect=lambda _key, default: default,
        ), patch(
            "app.indexers.runtime.config.get_bool",
            side_effect=lambda _key, default: default,
        ), patch(
            "app.indexers.runtime.build_default_registry",
            return_value=registry,
        ) as build:
            service = runtime._build_service()

        build.assert_called_once_with(
            user_agent="MediaFlux/Test",
            nyaa_endpoint_timeout_seconds=4.0,
            btbtla_min_interval_seconds=5,
            onelou_min_interval_seconds=5,
            onelou_endpoint_timeout_seconds=3.0,
            onelou_google_enabled=True,
        )
        self.assertIs(service.registry, registry)

    def test_build_service_enables_env_example_default_sites(self):
        registry = FakeRegistry()

        with patch("app.indexers.runtime.config.get", side_effect=lambda _key, default="": default), patch(
            "app.indexers.runtime.config.get_int",
            side_effect=lambda _key, default: default,
        ), patch(
            "app.indexers.runtime.config.get_bool",
            return_value=False,
        ), patch(
            "app.indexers.runtime.build_default_registry",
            return_value=registry,
        ):
            service = runtime._build_service()

        self.assertEqual(
            service.enabled_site_ids,
            frozenset({"nyaa", "mikan", "btbtla", "1lou", "tpb"}),
        )

    def test_build_service_ignores_retired_site_in_persisted_selection(self):
        registry = FakeRegistry()

        def get_value(key, default=""):
            if key == "INDEXER_ENABLED_SITES":
                return "nyaa,animetosho"
            return default

        with patch("app.indexers.runtime.config.get", side_effect=get_value), patch(
            "app.indexers.runtime.config.get_int",
            side_effect=lambda _key, default: default,
        ), patch(
            "app.indexers.runtime.config.get_bool",
            return_value=False,
        ), patch(
            "app.indexers.runtime.build_default_registry",
            return_value=registry,
        ):
            service = runtime._build_service()

        self.assertEqual(service.enabled_site_ids, frozenset({"nyaa"}))

    async def test_sync_bridge_without_binding_reuses_and_closes_standalone_loop(self):
        observed: list[asyncio.AbstractEventLoop] = []
        closed_on: list[asyncio.AbstractEventLoop] = []

        async def capture_loop():
            observed.append(asyncio.get_running_loop())
            return "ok"

        first = await asyncio.to_thread(
            runtime.run_indexer_awaitable_sync, capture_loop(), timeout_seconds=1.0,
        )
        second = await asyncio.to_thread(
            runtime.run_indexer_awaitable_sync, capture_loop(), timeout_seconds=1.0,
        )
        service = Mock()

        async def close():
            closed_on.append(asyncio.get_running_loop())

        service.aclose = close
        runtime._service = service
        stopped = await asyncio.to_thread(runtime._stop_standalone_runtime, 1.0)

        self.assertEqual((first, second), ("ok", "ok"))
        self.assertEqual(len(observed), 2)
        self.assertIs(observed[0], observed[1])
        self.assertEqual(closed_on, [observed[0]])
        self.assertTrue(stopped)
        self.assertIsNone(runtime._runtime_loop)

    async def test_standalone_shutdown_failure_keeps_runtime_handles_for_retry(self):
        async def capture_loop():
            return asyncio.get_running_loop()

        owner_loop = await asyncio.to_thread(
            runtime.run_indexer_awaitable_sync,
            capture_loop(),
            timeout_seconds=1.0,
        )
        thread = runtime._standalone_thread
        service = Mock()
        service.aclose = AsyncMock(
            side_effect=[RuntimeError("close failed once"), None]
        )
        runtime._service = service

        first = await asyncio.to_thread(runtime._stop_standalone_runtime, 0.2)
        self.assertFalse(first)
        self.assertIs(runtime._runtime_loop, owner_loop)
        self.assertIs(runtime._standalone_loop, owner_loop)
        self.assertIs(runtime._standalone_thread, thread)
        self.assertTrue(runtime._runtime_stopping)
        self.assertTrue(thread.is_alive())

        second = await asyncio.to_thread(runtime._stop_standalone_runtime, 1.0)
        self.assertTrue(second)
        self.assertEqual(service.aclose.await_count, 2)
        self.assertIsNone(runtime._runtime_loop)
        self.assertIsNone(runtime._standalone_loop)
        self.assertIsNone(runtime._standalone_thread)
        self.assertFalse(runtime._runtime_stopping)

    async def test_async_bridge_without_binding_uses_standalone_loop(self):
        caller_loop = asyncio.get_running_loop()

        async def capture_loop():
            return asyncio.get_running_loop()

        observed = await runtime.run_indexer_awaitable(
            capture_loop(), timeout_seconds=1.0,
        )

        self.assertIsNot(observed, caller_loop)
        self.assertIs(observed, runtime._standalone_loop)
        self.assertTrue(await asyncio.to_thread(runtime._stop_standalone_runtime, 1.0))

    async def test_sync_bridge_runs_on_bound_lifespan_loop(self):
        owner_loop = asyncio.get_running_loop()
        runtime.bind_indexer_event_loop(owner_loop)
        observed: list[asyncio.AbstractEventLoop] = []

        async def capture_loop():
            observed.append(asyncio.get_running_loop())
            return "ok"

        result = await asyncio.to_thread(
            runtime.run_indexer_awaitable_sync,
            capture_loop(),
            timeout_seconds=1.0,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(observed, [owner_loop])

    async def test_sync_bridge_closes_service_on_same_bound_loop(self):
        owner_loop = asyncio.get_running_loop()
        runtime.bind_indexer_event_loop(owner_loop)
        closed_on: list[asyncio.AbstractEventLoop] = []
        service = Mock()

        async def close():
            closed_on.append(asyncio.get_running_loop())

        service.aclose = close
        runtime._service = service

        await asyncio.to_thread(
            runtime.run_indexer_awaitable_sync,
            runtime.shutdown_indexer_service(),
            timeout_seconds=1.0,
        )

        self.assertEqual(closed_on, [owner_loop])
        self.assertIsNone(runtime._service)

    async def test_async_bridge_from_worker_loop_runs_on_bound_lifespan_loop(self):
        owner_loop = asyncio.get_running_loop()
        runtime.bind_indexer_event_loop(owner_loop)
        observed: list[asyncio.AbstractEventLoop] = []

        async def capture_loop():
            observed.append(asyncio.get_running_loop())
            return "ok"

        def run_from_worker_loop():
            return asyncio.run(
                runtime.run_indexer_awaitable(
                    capture_loop(), timeout_seconds=1.0
                )
            )

        result = await asyncio.to_thread(run_from_worker_loop)

        self.assertEqual(result, "ok")
        self.assertEqual(observed, [owner_loop])

    async def test_async_bridge_accepts_generic_awaitable_on_bound_loop(self):
        owner_loop = asyncio.get_running_loop()
        runtime.bind_indexer_event_loop(owner_loop)
        observed: list[asyncio.AbstractEventLoop] = []

        class GenericAwaitable:
            def __await__(self):
                async def capture():
                    observed.append(asyncio.get_running_loop())
                    return "ok"

                return capture().__await__()

        def run_from_worker_loop():
            return asyncio.run(runtime.run_indexer_awaitable(GenericAwaitable()))

        result = await asyncio.to_thread(run_from_worker_loop)

        self.assertEqual(result, "ok")
        self.assertEqual(observed, [owner_loop])

    async def test_shutdown_gate_cancels_pending_bridge_and_rejects_new_work(self):
        owner_loop = asyncio.get_running_loop()
        runtime.bind_indexer_event_loop(owner_loop)
        entered = asyncio.Event()
        release = asyncio.Event()
        worker_result: list[BaseException] = []

        async def gated_call():
            entered.set()
            await release.wait()

        def worker_target():
            try:
                asyncio.run(runtime.run_indexer_awaitable(gated_call()))
            except BaseException as exc:
                worker_result.append(exc)

        worker = threading.Thread(target=worker_target, daemon=True)
        worker.start()
        await asyncio.wait_for(entered.wait(), timeout=1.0)

        self.assertEqual(runtime.begin_indexer_shutdown(owner_loop), 1)
        await asyncio.to_thread(worker.join, 1.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(worker_result)

        async def rejected_call():
            return "unexpected"

        with self.assertRaisesRegex(RuntimeError, "正在关闭"):
            await runtime.run_indexer_awaitable(rejected_call())

    async def test_shutdown_wait_offloaded_from_owner_loop_allows_worker_bridge_to_finish(self):
        owner_loop = asyncio.get_running_loop()
        runtime.bind_indexer_event_loop(owner_loop)
        entered = asyncio.Event()
        release = asyncio.Event()
        worker_done = threading.Event()
        stop_called_on: list[int] = []

        async def gated_call():
            entered.set()
            await release.wait()
            return "ok"

        def worker_target():
            try:
                asyncio.run(runtime.run_indexer_awaitable(gated_call()))
            finally:
                worker_done.set()

        worker = threading.Thread(target=worker_target, daemon=True)
        worker.start()
        await asyncio.wait_for(entered.wait(), timeout=1.0)

        def wait_for_worker():
            stop_called_on.append(threading.get_ident())
            worker.join(timeout=1.0)
            return not worker.is_alive()

        async def release_soon():
            await asyncio.sleep(0.02)
            release.set()

        release_task = asyncio.create_task(release_soon())
        started = time.monotonic()
        stopped = await asyncio.to_thread(wait_for_worker)
        elapsed = time.monotonic() - started
        await release_task

        self.assertTrue(stopped)
        self.assertTrue(worker_done.is_set())
        self.assertLess(elapsed, 0.5)
        self.assertEqual(len(stop_called_on), 1)
        self.assertNotEqual(stop_called_on[0], threading.get_ident())

    async def test_async_bridge_on_owner_loop_awaits_directly(self):
        owner_loop = asyncio.get_running_loop()
        runtime.bind_indexer_event_loop(owner_loop)

        async def capture_loop():
            return asyncio.get_running_loop()

        observed = await runtime.run_indexer_awaitable(capture_loop())

        self.assertIs(observed, owner_loop)

    async def test_lifespan_restores_exception_handler_when_indexer_bind_fails(self):
        from app.main import create_app

        owner_loop = asyncio.get_running_loop()
        previous = owner_loop.get_exception_handler()
        delegated: list[dict] = []

        def original_handler(_loop, context):
            delegated.append(context)

        def bind_with_failure(loop):
            handler = loop.get_exception_handler()
            self.assertIsNotNone(handler)
            handler(loop, {"message": "unsilenced runtime failure"})
            raise RuntimeError("bind failed")

        owner_loop.set_exception_handler(original_handler)
        proxy = Mock()
        proxy.start = AsyncMock()
        proxy.stop = AsyncMock()
        try:
            with patch(
                "app.main.database.init_db"
            ), patch(
                "app.modules.recognition_knowledge.ensure_seed_knowledge"
            ), patch(
                "app.modules.media_proxy.get_media_proxy_manager", return_value=proxy
            ), patch(
                "app.indexers.runtime.bind_indexer_event_loop",
                side_effect=bind_with_failure,
            ) as bind, patch(
                "app.indexers.runtime.begin_indexer_shutdown"
            ) as begin, patch(
                "app.indexers.runtime.unbind_indexer_event_loop"
            ) as unbind, patch(
                "app.discovery.service.shutdown_discovery_service"
            ) as shutdown_discovery, patch(
                "app.discovery.search.shutdown_discovery_search_service"
            ) as shutdown_search, patch(
                "app.modules.directory_scrape.close_directory_scrape_service"
            ) as close_directory_scrape, patch(
                "app.modules.local_media_service.close_local_media_service"
            ) as close_local_media, patch(
                "app.routes.discovery_image.close_poster_session"
            ) as close_poster, patch(
                "app.indexers.runtime.shutdown_indexer_service", new=AsyncMock()
            ) as shutdown_indexer:
                app = create_app(start_background=False)
                with self.assertRaisesRegex(RuntimeError, "bind failed"):
                    async with app.router.lifespan_context(app):
                        self.fail("lifespan must not start after bind failure")

            bind.assert_called_once_with(owner_loop)
            begin.assert_not_called()
            unbind.assert_not_called()
            shutdown_discovery.assert_not_called()
            shutdown_search.assert_not_called()
            close_directory_scrape.assert_not_called()
            close_local_media.assert_not_called()
            close_poster.assert_called_once_with()
            shutdown_indexer.assert_not_awaited()
            self.assertEqual(delegated, [{"message": "unsilenced runtime failure"}])
            self.assertIs(owner_loop.get_exception_handler(), original_handler)
        finally:
            owner_loop.set_exception_handler(previous)

    async def test_application_lifespan_offloads_background_shutdown(self):
        from app.main import create_app

        owner_loop = asyncio.get_running_loop()
        owner_thread = threading.get_ident()
        stop_threads: list[int] = []
        owner_progress = asyncio.Event()
        proxy = Mock()
        proxy.start = AsyncMock()
        proxy.stop = AsyncMock()

        async def mark_owner_progress():
            owner_progress.set()

        def stop_background():
            stop_threads.append(threading.get_ident())
            future = asyncio.run_coroutine_threadsafe(mark_owner_progress(), owner_loop)
            future.result(timeout=1.0)
            return True

        with patch.object(owner_loop, "slow_callback_duration", 2.0), patch(
            "app.main.database.init_db"
        ), patch(
            "app.modules.recognition_knowledge.ensure_seed_knowledge"
        ), patch(
            "app.modules.media_proxy.get_media_proxy_manager", return_value=proxy
        ), patch(
            "app.modules.media_proxy.start_signed_media_probe_runtime"
        ) as start_probe_runtime, patch(
            "app.modules.media_proxy.shutdown_signed_media_probe_runtime",
            return_value=True,
        ) as stop_probe_runtime, patch(
            "app.indexers.runtime.bind_indexer_event_loop"
        ), patch(
            "app.indexers.runtime.unbind_indexer_event_loop"
        ), patch(
            "app.main.start_background_services"
        ), patch(
            "app.main.stop_background_services", side_effect=stop_background
        ), patch(
            "app.discovery.service.shutdown_discovery_service"
        ), patch(
            "app.discovery.search.shutdown_discovery_search_service"
        ), patch(
            "app.modules.directory_scrape.close_directory_scrape_service"
        ) as close_directory_scrape, patch(
            "app.indexers.runtime.shutdown_indexer_service", new=AsyncMock()
        ):
            app = create_app(start_background=True)
            async with app.router.lifespan_context(app):
                self.assertTrue(app.state.ready)

        self.assertTrue(owner_progress.is_set())
        self.assertEqual(len(stop_threads), 1)
        self.assertNotEqual(stop_threads[0], owner_thread)
        proxy.start.assert_awaited_once_with()
        proxy.stop.assert_awaited_once_with()
        close_directory_scrape.assert_called_once_with()
        start_probe_runtime.assert_called_once_with()
        stop_probe_runtime.assert_called_once_with(5.0)

    async def test_application_lifespan_keeps_shutdown_gate_when_workers_do_not_stop(self):
        from app.main import create_app

        proxy = Mock()
        proxy.start = AsyncMock()
        proxy.stop = AsyncMock()
        begin = Mock()
        unbind = Mock()
        shutdown = AsyncMock()

        with patch(
            "app.main.database.init_db"
        ), patch(
            "app.modules.recognition_knowledge.ensure_seed_knowledge"
        ), patch(
            "app.modules.media_proxy.get_media_proxy_manager", return_value=proxy
        ), patch(
            "app.indexers.runtime.bind_indexer_event_loop"
        ), patch(
            "app.indexers.runtime.begin_indexer_shutdown", begin
        ), patch(
            "app.indexers.runtime.unbind_indexer_event_loop", unbind
        ), patch(
            "app.main.start_background_services"
        ), patch(
            "app.main.stop_background_services", return_value=False
        ), patch(
            "app.discovery.service.shutdown_discovery_service"
        ) as shutdown_discovery, patch(
            "app.discovery.search.shutdown_discovery_search_service"
        ) as shutdown_search, patch(
            "app.modules.directory_scrape.close_directory_scrape_service"
        ) as close_directory_scrape, patch(
            "app.indexers.runtime.shutdown_indexer_service", new=shutdown
        ):
            app = create_app(start_background=True)
            async with app.router.lifespan_context(app):
                self.assertTrue(app.state.ready)

        begin.assert_called_once()
        unbind.assert_not_called()
        shutdown.assert_not_awaited()
        shutdown_discovery.assert_not_called()
        shutdown_search.assert_not_called()
        close_directory_scrape.assert_not_called()
        proxy.stop.assert_awaited_once_with()


    async def test_shutdown_failure_keeps_service_handle_for_retry(self):
        service = Mock()
        service.aclose = AsyncMock(
            side_effect=[RuntimeError("registry still in use"), None]
        )
        runtime._service = service

        with self.assertRaisesRegex(RuntimeError, "registry still in use"):
            await runtime.shutdown_indexer_service()

        self.assertIs(runtime._service, service)
        await runtime.shutdown_indexer_service()
        self.assertIsNone(runtime._service)
        self.assertEqual(service.aclose.await_count, 2)


    async def test_shutdown_uses_service_lifecycle_hook(self):
        service = Mock()
        service.aclose = AsyncMock()
        service.registry = Mock()
        service.registry.aclose = AsyncMock()
        runtime._service = service

        await runtime.shutdown_indexer_service()

        service.aclose.assert_awaited_once_with()
        service.registry.aclose.assert_not_awaited()
        self.assertIsNone(runtime._service)


if __name__ == "__main__":
    unittest.main()
