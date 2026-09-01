"""媒体反代职责收敛回归测试。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import importlib.util
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app import database as db
from tests.support import InitializedWebTestCase


class MediaServerProfileTests(unittest.TestCase):
    def test_profile_resolver_module_exists(self):
        self.assertIsNotNone(
            importlib.util.find_spec("app.modules.media_server_profiles"),
            "缺少服务端媒体服务器配置解析模块",
        )

    def test_configured_jellyfin_is_resolved_server_side(self):
        spec = importlib.util.find_spec("app.modules.media_server_profiles")
        self.assertIsNotNone(spec, "缺少服务端媒体服务器配置解析模块")
        profiles = importlib.import_module("app.modules.media_server_profiles")
        row = {
            "id": 7,
            "config_source": "configured:jellyfin",
            "server_type": "jellyfin",
            "upstream_url": "",
            "api_key": "",
            "enabled": 1,
        }
        values = {
            "JELLYFIN_ENABLED": "true",
            "JELLYFIN_URL": "http://127.0.0.1:8096",
            "JELLYFIN_API_KEY": "server-secret",
        }
        with (
            patch.object(
                profiles.config,
                "get",
                side_effect=lambda key, default="": values.get(key, default),
            ),
            patch.object(profiles.config, "get_bool", return_value=True),
        ):
            resolved = profiles.resolve_proxy_instance(row)
        self.assertEqual(resolved["upstream_url"], "http://127.0.0.1:8096")
        self.assertEqual(resolved["api_key"], "server-secret")
        self.assertEqual(resolved["server_type"], "jellyfin")
class MediaProxyProfileApiTests(InitializedWebTestCase):
    @staticmethod
    def _csrf_token(response) -> str:
        import re
        match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', response.text)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def test_profiles_and_configured_instance_do_not_expose_secret(self):
        from fastapi.testclient import TestClient

        from app.main import create_app
        from app.config import web_credentials
        import app.modules.media_server_profiles as profiles

        values = {
            "MEDIAFLUX_INITIALIZED": "1",
            "WEB_SECRET_KEY": "media-proxy-profile-test-secret",
            "ENV_WEB_PASSPORT": "admin",
            "ENV_WEB_PASSWORD": "123456",
            "JELLYFIN_ENABLED": "true",
            "JELLYFIN_URL": "http://127.0.0.1:8096",
            "JELLYFIN_API_KEY": "server-secret",
            "EMBY_ENABLED": "false",
            "EMBY_URL": "",
            "EMBY_TOKEN": "",
        }
        with tempfile.TemporaryDirectory() as root, \
             patch("app.database.DB_PATH", Path(root) / "profile-api.db"), \
             patch.object(profiles.config, "get", side_effect=lambda key, default="": values.get(key, default)), \
             patch.object(profiles.config, "get_bool", side_effect=lambda key, default=False: values.get(key, str(default)).lower() in {"1", "true", "yes", "on"}):
            with TestClient(create_app(), raise_server_exceptions=False) as client:
                login_page = client.get("/login")
                username, password = web_credentials()
                login = client.post(
                    "/login",
                    data={
                        "csrf_token": self._csrf_token(login_page),
                        "username": username,
                        "password": password,
                    },
                    follow_redirects=False,
                )
                self.assertEqual(login.status_code, 302)
                settings = client.get("/settings")
                headers = {"X-CSRF-Token": self._csrf_token(settings)}

                response = client.get("/api/media-proxy/profiles")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("credential", response.text)
                self.assertNotIn("server-secret", response.text)

                created = client.post(
                    "/api/media-proxy",
                    headers=headers,
                    json={
                        "name": "Configured Jellyfin",
                        "config_source": "configured:jellyfin",
                        "listen_host": "127.0.0.1",
                        "listen_port": 18098,
                        "enabled": False,
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                instance = created.json()["instance"]
                self.assertEqual(instance["config_source"], "configured:jellyfin")
                self.assertEqual(instance["upstream_url"], "http://127.0.0.1:8096")
                self.assertNotIn("server-secret", created.text)


class MediaProxyProbeApiTests(unittest.TestCase):
    def test_probe_endpoint_reuses_pinned_service_and_preserves_contract(self):
        import json
        from app.routes import media_proxy_api

        with (
            patch.object(media_proxy_api, "require_api_login", return_value=None),
            patch.object(media_proxy_api.db, "get_media_proxy_instance", return_value={"id": 7}),
            patch.object(
                media_proxy_api, "probe_media_proxy_instance",
                new=AsyncMock(return_value={"status_code": 204, "latency_ms": 37}),
            ) as probe,
        ):
            response = asyncio.run(media_proxy_api.test_instance(7, SimpleNamespace()))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.body),
            {"success": True, "status_code": 204, "elapsed_ms": 37},
        )
        probe.assert_awaited_once_with(7, timeout_seconds=10.0)

    def test_probe_endpoint_maps_oversize_upstream_to_stable_error(self):
        import json
        from app.routes import media_proxy_api

        with (
            patch.object(media_proxy_api, "require_api_login", return_value=None),
            patch.object(media_proxy_api.db, "get_media_proxy_instance", return_value={"id": 7}),
            patch.object(
                media_proxy_api,
                "probe_media_proxy_instance",
                new=AsyncMock(side_effect=media_proxy_api.ProxyUpstreamBodyTooLarge()),
            ),
        ):
            response = asyncio.run(media_proxy_api.test_instance(7, SimpleNamespace()))

        self.assertEqual(response.status_code, 502)
        self.assertEqual(json.loads(response.body), {"error": "上游响应过大"})


class MediaProxyLoggingBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_uvicorn_config_preserves_root_redaction_handlers(self):
        from app.modules import media_proxy

        row = {
            "id": 9,
            "listen_host": "127.0.0.1",
            "listen_port": 18099,
            "upstream_url": "http://127.0.0.1:8096",
        }
        fake_socket = MagicMock()
        fake_task = MagicMock()
        fake_task.done.return_value = False
        fake_server = MagicMock()
        with (
            patch.object(media_proxy, "resolve_proxy_instance", return_value=row),
            patch.object(media_proxy.socket, "socket", return_value=fake_socket),
            patch.object(media_proxy, "create_proxy_app", return_value=object()),
            patch.object(media_proxy.uvicorn, "Config", return_value=object()) as config,
            patch.object(media_proxy.uvicorn, "Server", return_value=fake_server),
            patch.object(media_proxy.asyncio, "create_task", return_value=fake_task),
            patch.object(media_proxy.asyncio, "sleep", new=AsyncMock()),
            patch.object(media_proxy.database, "update_media_proxy_instance"),
        ):
            await media_proxy.MediaProxyManager()._start_runtime(row)

        self.assertIsNone(config.call_args.kwargs["log_config"])
        self.assertFalse(config.call_args.kwargs["access_log"])
        self.assertEqual(config.call_args.kwargs["lifespan"], "on")
        self.assertFalse(config.call_args.kwargs["proxy_headers"])
        self.assertEqual(config.call_args.kwargs["forwarded_allow_ips"], [])
        self.assertEqual(
            config.call_args.kwargs["ws_max_size"],
            media_proxy._proxy_websocket_message_limit(),
        )


    async def test_runtime_enables_proxy_headers_only_for_instance_cidrs(self):
        from app.modules import media_proxy

        row = {
            "id": 10,
            "listen_host": "127.0.0.1",
            "listen_port": 18100,
            "upstream_url": "http://127.0.0.1:8096",
            "trust_forwarded_headers": 1,
            "trusted_proxy_cidrs_json": (
                '["172.18.0.1/32","192.168.88.110/32"]'
            ),
        }
        fake_socket = MagicMock()
        fake_task = MagicMock()
        fake_task.done.return_value = False
        fake_server = MagicMock()
        with (
            patch.object(media_proxy, "resolve_proxy_instance", return_value=row),
            patch.object(media_proxy.socket, "socket", return_value=fake_socket),
            patch.object(media_proxy, "create_proxy_app", return_value=object()),
            patch.object(media_proxy.uvicorn, "Config", return_value=object()) as config,
            patch.object(media_proxy.uvicorn, "Server", return_value=fake_server),
            patch.object(media_proxy.asyncio, "create_task", return_value=fake_task),
            patch.object(media_proxy.asyncio, "sleep", new=AsyncMock()),
            patch.object(media_proxy.database, "update_media_proxy_instance"),
        ):
            runtime = await media_proxy.MediaProxyManager()._start_runtime(row)

        self.assertTrue(config.call_args.kwargs["proxy_headers"])
        self.assertEqual(
            config.call_args.kwargs["forwarded_allow_ips"],
            ["172.18.0.1/32", "192.168.88.110/32"],
        )
        self.assertEqual(
            runtime.forwarding,
            (True, ("172.18.0.1/32", "192.168.88.110/32")),
        )


    async def test_runtime_start_rolls_back_when_status_persist_fails(self):
        from app.modules import media_proxy

        row = {
            "id": 11,
            "listen_host": "127.0.0.1",
            "listen_port": 18101,
            "upstream_url": "http://127.0.0.1:8096",
        }
        fake_socket = MagicMock()
        fake_task = MagicMock()
        fake_task.done.return_value = False
        fake_server = MagicMock()
        manager = media_proxy.MediaProxyManager()
        real_create_task = asyncio.create_task

        def create_task(coro, *, name=None):
            if name == "media-proxy-11":
                close = getattr(coro, "close", None)
                if callable(close):
                    close()
                return fake_task
            return real_create_task(coro, name=name)

        with (
            patch.object(media_proxy, "resolve_proxy_instance", return_value=row),
            patch.object(media_proxy.socket, "socket", return_value=fake_socket),
            patch.object(media_proxy, "create_proxy_app", return_value=object()),
            patch.object(media_proxy.uvicorn, "Config", return_value=object()),
            patch.object(media_proxy.uvicorn, "Server", return_value=fake_server),
            patch.object(media_proxy.asyncio, "create_task", side_effect=create_task),
            patch.object(media_proxy.asyncio, "sleep", new=AsyncMock()),
            patch.object(
                media_proxy.database,
                "update_media_proxy_instance",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            patch.object(manager, "_stop_runtime", new=AsyncMock()) as stop_runtime,
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                await manager._start_runtime(row)

        stop_runtime.assert_awaited_once()
        runtime = stop_runtime.await_args.args[0]
        self.assertEqual(runtime.instance_id, 11)
        self.assertIs(runtime.server, fake_server)
        self.assertIs(runtime.sock, fake_socket)
        self.assertIs(runtime.task, fake_task)
        self.assertEqual(manager._runtimes, {})


    async def test_runtime_start_cancellation_rolls_back_spawned_runtime(self):
        from app.modules import media_proxy

        row = {
            "id": 12,
            "listen_host": "127.0.0.1",
            "listen_port": 18102,
            "upstream_url": "http://127.0.0.1:8096",
        }
        fake_socket = MagicMock()
        fake_task = MagicMock()
        fake_task.done.return_value = False
        fake_server = MagicMock()
        manager = media_proxy.MediaProxyManager()
        real_create_task = asyncio.create_task

        def create_task(coro, *, name=None):
            if name == "media-proxy-12":
                close = getattr(coro, "close", None)
                if callable(close):
                    close()
                return fake_task
            return real_create_task(coro, name=name)

        with (
            patch.object(media_proxy, "resolve_proxy_instance", return_value=row),
            patch.object(media_proxy.socket, "socket", return_value=fake_socket),
            patch.object(media_proxy, "create_proxy_app", return_value=object()),
            patch.object(media_proxy.uvicorn, "Config", return_value=object()),
            patch.object(media_proxy.uvicorn, "Server", return_value=fake_server),
            patch.object(media_proxy.asyncio, "create_task", side_effect=create_task),
            patch.object(
                media_proxy.asyncio,
                "sleep",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            patch.object(
                media_proxy.database, "update_media_proxy_instance"
            ) as update_status,
            patch.object(manager, "_stop_runtime", new=AsyncMock()) as stop_runtime,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await manager._start_runtime(row)

        stop_runtime.assert_awaited_once()
        update_status.assert_not_called()
        runtime = stop_runtime.await_args.args[0]
        self.assertEqual(runtime.instance_id, 12)
        self.assertIs(runtime.task, fake_task)
        self.assertEqual(manager._runtimes, {})


class MediaProxyManagerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconcile_restarts_a_runtime_whose_task_exited(self):
        from app.modules import media_proxy

        async def crash():
            raise RuntimeError("simulated proxy crash")

        failed_task = asyncio.create_task(crash())
        await asyncio.sleep(0)
        row = {
            "id": 9,
            "listen_host": "127.0.0.1",
            "listen_port": 18099,
            "upstream_url": "http://127.0.0.1:8096",
            "enabled": 1,
        }
        previous = media_proxy.ProxyRuntime(
            instance_id=9,
            bind=("127.0.0.1", 18099),
            server=MagicMock(),
            task=failed_task,
            sock=MagicMock(),
            signed_urls=MagicMock(),
        )
        replacement = MagicMock()
        manager = media_proxy.MediaProxyManager()
        manager._runtimes[9] = previous
        with (
            patch.object(media_proxy.database, "list_media_proxy_instances", return_value=[row]),
            patch.object(media_proxy.database, "update_media_proxy_instance") as update,
            patch.object(media_proxy, "resolve_proxy_instance", return_value=row),
            patch.object(manager, "_stop_runtime", new=AsyncMock()) as stop,
            patch.object(manager, "_start_runtime", new=AsyncMock(return_value=replacement)) as start,
        ):
            result = await manager.reconcile()

        stop.assert_awaited_once_with(previous)
        start.assert_awaited_once_with(row)
        self.assertIs(manager._runtimes[9], replacement)
        self.assertEqual(result, {"started": [9], "stopped": [9], "failed": {}})
        self.assertTrue(any(
            call.args == (9, {"status": "error", "last_error": "媒体反代运行任务意外退出"})
            for call in update.call_args_list
        ))

    async def test_reconcile_restarts_same_bind_after_forwarding_config_changes(self):
        from app.modules import media_proxy

        row = {
            "id": 9,
            "listen_host": "127.0.0.1",
            "listen_port": 18099,
            "upstream_url": "http://127.0.0.1:8096",
            "enabled": 1,
            "trust_forwarded_headers": 1,
            "trusted_proxy_cidrs_json": '["172.18.0.1/32"]',
        }
        task = MagicMock()
        task.done.return_value = False
        previous = media_proxy.ProxyRuntime(
            instance_id=9,
            bind=("127.0.0.1", 18099),
            server=MagicMock(),
            task=task,
            sock=MagicMock(),
            signed_urls=MagicMock(),
        )
        replacement = MagicMock()
        manager = media_proxy.MediaProxyManager()
        manager._runtimes[9] = previous
        events: list[str] = []

        async def stop_runtime(runtime):
            self.assertIs(runtime, previous)
            events.append("stop")

        async def start_runtime(runtime_row):
            self.assertIs(runtime_row, row)
            events.append("start")
            return replacement

        with (
            patch.object(media_proxy.database, "list_media_proxy_instances", return_value=[row]),
            patch.object(media_proxy.database, "update_media_proxy_instance"),
            patch.object(media_proxy, "resolve_proxy_instance", return_value=row),
            patch.object(manager, "_stop_runtime", side_effect=stop_runtime),
            patch.object(manager, "_start_runtime", side_effect=start_runtime),
        ):
            result = await manager.reconcile()

        self.assertEqual(events, ["stop", "start"])
        self.assertIs(manager._runtimes[9], replacement)
        self.assertEqual(result, {"started": [9], "stopped": [9], "failed": {}})

    async def test_reconcile_stop_failure_retains_runtime_and_does_not_start_replacement(self):
        from app.modules import media_proxy

        row = {
            "id": 9,
            "listen_host": "127.0.0.1",
            "listen_port": 18100,
            "upstream_url": "http://127.0.0.1:8096",
            "enabled": 1,
        }
        task = MagicMock()
        task.done.return_value = False
        previous = media_proxy.ProxyRuntime(
            instance_id=9,
            bind=("127.0.0.1", 18099),
            server=MagicMock(),
            task=task,
            sock=MagicMock(),
            signed_urls=MagicMock(),
        )
        manager = media_proxy.MediaProxyManager()
        manager._runtimes[9] = previous

        with (
            patch.object(media_proxy.database, "list_media_proxy_instances", return_value=[row]),
            patch.object(media_proxy, "resolve_proxy_instance", return_value=row),
            patch.object(
                manager,
                "_stop_runtime",
                new=AsyncMock(side_effect=RuntimeError("client close failed")),
            ) as stop,
            patch.object(manager, "_start_runtime", new=AsyncMock()) as start,
        ):
            with self.assertRaisesRegex(RuntimeError, "client close failed"):
                await manager.reconcile()

        stop.assert_awaited_once_with(previous)
        start.assert_not_awaited()
        self.assertIs(manager._runtimes[9], previous)

    async def test_restart_stop_failure_retains_runtime_and_does_not_start_replacement(self):
        from app.modules import media_proxy

        row = {
            "id": 9,
            "listen_host": "127.0.0.1",
            "listen_port": 18099,
            "upstream_url": "http://127.0.0.1:8096",
            "enabled": 1,
        }
        task = MagicMock()
        task.done.return_value = False
        previous = media_proxy.ProxyRuntime(
            instance_id=9,
            bind=("127.0.0.1", 18099),
            server=MagicMock(),
            task=task,
            sock=MagicMock(),
            signed_urls=MagicMock(),
        )
        manager = media_proxy.MediaProxyManager()
        manager._runtimes[9] = previous

        with (
            patch.object(media_proxy.database, "get_media_proxy_instance", return_value=row),
            patch.object(media_proxy, "resolve_proxy_instance", return_value=row),
            patch.object(
                manager,
                "_stop_runtime",
                new=AsyncMock(side_effect=RuntimeError("client close failed")),
            ) as stop,
            patch.object(manager, "_start_runtime", new=AsyncMock()) as start,
        ):
            with self.assertRaisesRegex(RuntimeError, "client close failed"):
                await manager.restart_instance(9)

        stop.assert_awaited_once_with(previous)
        start.assert_not_awaited()
        self.assertIs(manager._runtimes[9], previous)

    async def test_reconcile_offloads_sqlite_work_from_event_loop(self):
        from app.modules import media_proxy

        loop_thread = threading.get_ident()
        worker_threads: list[int] = []
        ticker_ran = asyncio.Event()

        def slow_list_instances():
            worker_threads.append(threading.get_ident())
            time.sleep(0.05)
            return []

        async def ticker() -> None:
            await asyncio.sleep(0.005)
            ticker_ran.set()

        manager = media_proxy.MediaProxyManager()
        with patch.object(
            media_proxy.database,
            "list_media_proxy_instances",
            side_effect=slow_list_instances,
        ):
            result, _ = await asyncio.gather(manager.reconcile(), ticker())

        self.assertTrue(ticker_ran.is_set())
        self.assertEqual(result, {"started": [], "stopped": [], "failed": {}})
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], loop_thread)

    async def test_client_authorization_offloads_dns_resolution(self):
        from app.modules import media_proxy

        loop_thread = threading.get_ident()
        resolver_threads: list[int] = []
        ticker_ran = asyncio.Event()

        def slow_pin(_upstream, _path):
            resolver_threads.append(threading.get_ident())
            time.sleep(0.05)
            return SimpleNamespace(
                connect_url="http://127.0.0.1:8096/Users/Me",
                host_header="media.invalid:8096",
                sni_hostname="media.invalid",
            )

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def build_request(self, *_args, **_kwargs):
                return object()

            async def send(self, _request):
                return SimpleNamespace(status_code=200)

        async def ticker() -> None:
            await asyncio.sleep(0.005)
            ticker_ran.set()

        request = SimpleNamespace(
            headers={"X-Emby-Token": "user-token"},
            query_params={},
        )
        instance = {"id": 9, "upstream_url": "http://media.invalid:8096"}
        with (
            patch.object(media_proxy, "_pin_upstream_target", side_effect=slow_pin),
            patch.object(media_proxy.httpx, "AsyncClient", return_value=FakeClient()),
        ):
            authorized, _ = await asyncio.gather(
                media_proxy._client_is_authorized(instance, request),
                ticker(),
            )

        self.assertTrue(authorized)
        self.assertTrue(ticker_ran.is_set())
        self.assertEqual(len(resolver_threads), 1)
        self.assertNotEqual(resolver_threads[0], loop_thread)

    def test_request_reconcile_observes_threadsafe_future_failure(self):
        from app.modules import media_proxy

        manager = media_proxy.MediaProxyManager()
        manager._loop = MagicMock()
        manager._loop.is_closed.return_value = False
        manager._loop.is_running.return_value = True
        future = concurrent.futures.Future()

        def schedule(coroutine, _loop):
            coroutine.close()
            return future

        with patch.object(
            media_proxy.asyncio, "run_coroutine_threadsafe", side_effect=schedule
        ), patch.object(media_proxy.logger, "warning") as warning:
            manager.request_reconcile()
            future.set_exception(RuntimeError("simulated reconcile failure"))

        warning.assert_called_once_with(
            "媒体反代热重载失败 type=%s", "RuntimeError"
        )

    def test_threadsafe_submission_failure_closes_unscheduled_coroutines(self):
        from app.modules import media_proxy

        manager = media_proxy.MediaProxyManager()
        manager._loop = MagicMock()
        manager._loop.is_closed.return_value = False
        manager._loop.is_running.return_value = True
        reconcile = MagicMock()
        restart = MagicMock()

        with patch.object(
            manager, "reconcile", new=MagicMock(return_value=reconcile)
        ), patch.object(
            manager, "restart_instance", new=MagicMock(return_value=restart)
        ), patch.object(
            media_proxy.asyncio,
            "run_coroutine_threadsafe",
            side_effect=RuntimeError("Event loop is closed"),
        ):
            self.assertFalse(manager.request_reconcile())
            self.assertFalse(manager.request_restart(9))

        reconcile.close.assert_called_once_with()
        restart.close.assert_called_once_with()

    async def test_upstream_pool_retains_only_failed_clients_for_retry(self):
        from app.modules import media_proxy

        class Client:
            def __init__(self, *, fail_once: bool):
                self.fail_once = fail_once
                self.close_calls = 0

            async def aclose(self):
                self.close_calls += 1
                if self.fail_once and self.close_calls == 1:
                    raise RuntimeError("simulated close failure")

        pool = media_proxy._UpstreamClientPool()
        failed = Client(fail_once=True)
        closed = Client(fail_once=False)
        failed_key = ("http", "failed.invalid", 80)
        closed_key = ("http", "closed.invalid", 80)
        pool._clients = {failed_key: failed, closed_key: closed}

        with self.assertRaisesRegex(RuntimeError, "1 个客户端关闭失败"):
            await pool.aclose()
        self.assertEqual(pool._clients, {failed_key: failed})
        self.assertEqual(closed.close_calls, 1)

        await pool.aclose()
        self.assertEqual(pool._clients, {})
        self.assertEqual(failed.close_calls, 2)

    async def test_stop_runtime_retries_pool_retained_by_lifespan_failure(self):
        from app.modules import media_proxy

        class FailOnceClient:
            def __init__(self):
                self.close_calls = 0

            async def aclose(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("simulated close failure")

        pool = media_proxy._UpstreamClientPool()
        client = FailOnceClient()
        pool._clients[("http", "upstream.invalid", 80)] = client
        with self.assertRaises(RuntimeError):
            await pool.aclose()

        task = asyncio.create_task(asyncio.sleep(0))
        await task
        runtime = media_proxy.ProxyRuntime(
            instance_id=12,
            bind=("127.0.0.1", 18102),
            server=MagicMock(),
            task=task,
            sock=MagicMock(),
            signed_urls=MagicMock(),
            upstream_clients=pool,
        )
        with patch.object(media_proxy, "_release_signed_url_cache"):
            await media_proxy.MediaProxyManager._stop_runtime(runtime)

        self.assertEqual(client.close_calls, 2)
        self.assertEqual(pool._clients, {})


    async def test_stop_runtime_cancellation_still_closes_upstream_pool(self):
        from app.modules import media_proxy

        class ObservablePool:
            def __init__(self):
                self.closed = False

            async def aclose(self):
                await asyncio.sleep(0)
                self.closed = True

        pool = ObservablePool()
        runtime_task = asyncio.create_task(asyncio.Event().wait())
        runtime = media_proxy.ProxyRuntime(
            instance_id=14,
            bind=("127.0.0.1", 18104),
            server=MagicMock(),
            task=runtime_task,
            sock=MagicMock(),
            signed_urls=MagicMock(),
            upstream_clients=pool,
        )
        with patch.object(media_proxy, "_release_signed_url_cache"):
            stop_task = asyncio.create_task(
                media_proxy.MediaProxyManager._stop_runtime(runtime)
            )
            await asyncio.sleep(0)
            stop_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stop_task

        self.assertTrue(pool.closed)
        self.assertTrue(runtime_task.done())
        runtime.sock.close.assert_called_once_with()

    async def test_manager_stop_removes_runtime_whose_server_task_was_cancelled(self):
        from app.modules import media_proxy

        class ObservablePool:
            def __init__(self):
                self.closed = False

            async def aclose(self):
                self.closed = True

        manager = media_proxy.MediaProxyManager()
        pool = ObservablePool()
        runtime_task = asyncio.create_task(asyncio.Event().wait())
        runtime_task.cancel()
        await asyncio.gather(runtime_task, return_exceptions=True)
        runtime = media_proxy.ProxyRuntime(
            instance_id=16,
            bind=("127.0.0.1", 18106),
            server=MagicMock(),
            task=runtime_task,
            sock=MagicMock(),
            signed_urls=MagicMock(),
            upstream_clients=pool,
        )
        manager._runtimes = {16: runtime}

        with patch.object(media_proxy, "_release_signed_url_cache"):
            await manager.stop()

        self.assertTrue(pool.closed)
        self.assertEqual(manager._runtimes, {})
        runtime.sock.close.assert_called_once_with()

    async def test_manager_stop_retains_failed_runtime_for_retry(self):
        from app.modules import media_proxy

        manager = media_proxy.MediaProxyManager()
        runtime = media_proxy.ProxyRuntime(
            instance_id=13,
            bind=("127.0.0.1", 18103),
            server=MagicMock(),
            task=MagicMock(),
            sock=MagicMock(),
            signed_urls=MagicMock(),
        )
        manager._runtimes = {13: runtime}

        with patch.object(
            manager,
            "_stop_runtime",
            new=AsyncMock(side_effect=[RuntimeError("simulated close failure"), None]),
        ) as stop_runtime:
            with self.assertRaisesRegex(RuntimeError, "1 个媒体反代实例关闭失败"):
                await manager.stop()
            self.assertEqual(manager._runtimes, {13: runtime})

            await manager.stop()

        self.assertEqual(manager._runtimes, {})
        self.assertEqual(stop_runtime.await_count, 2)

    async def test_manager_stop_cancellation_cleans_all_snapshotted_runtimes(self):
        from app.modules import media_proxy

        manager = media_proxy.MediaProxyManager()
        started: set[int] = set()
        finished: set[int] = set()
        release = asyncio.Event()

        def runtime(instance_id: int):
            return media_proxy.ProxyRuntime(
                instance_id=instance_id,
                bind=("127.0.0.1", 18090 + instance_id),
                server=MagicMock(),
                task=MagicMock(),
                sock=MagicMock(),
                signed_urls=MagicMock(),
            )

        first, second = runtime(1), runtime(2)
        manager._runtimes = {1: first, 2: second}

        async def slow_stop(item):
            started.add(item.instance_id)
            try:
                await release.wait()
            finally:
                finished.add(item.instance_id)

        with patch.object(manager, "_stop_runtime", side_effect=slow_stop):
            stop_task = asyncio.create_task(manager.stop())
            for _ in range(20):
                if started == {1, 2}:
                    break
                await asyncio.sleep(0)
            self.assertEqual(started, {1, 2})
            stop_task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(stop_task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await stop_task

        self.assertEqual(finished, {1, 2})
        self.assertEqual(manager._runtimes, {})

    async def test_manager_stop_cancellation_retains_runtime_when_cleanup_fails(self):
        from app.modules import media_proxy

        class BlockingFailingPool:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def aclose(self):
                self.started.set()
                await self.release.wait()
                raise RuntimeError("simulated close failure")

        manager = media_proxy.MediaProxyManager()
        pool = BlockingFailingPool()
        runtime_task = asyncio.create_task(asyncio.sleep(0))
        await runtime_task
        runtime = media_proxy.ProxyRuntime(
            instance_id=15,
            bind=("127.0.0.1", 18105),
            server=MagicMock(),
            task=runtime_task,
            sock=MagicMock(),
            signed_urls=MagicMock(),
            upstream_clients=pool,
        )
        manager._runtimes = {15: runtime}

        with patch.object(media_proxy, "_release_signed_url_cache"):
            stop_task = asyncio.create_task(manager.stop())
            await pool.started.wait()
            stop_task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(stop_task.done())
            pool.release.set()
            with self.assertRaises(asyncio.CancelledError):
                await stop_task

        self.assertEqual(manager._runtimes, {15: runtime})

    async def test_shutdown_fences_concurrent_reconcile_and_clears_runtime(self):
        from app.modules import media_proxy

        manager = media_proxy.MediaProxyManager()
        manager._loop = asyncio.get_running_loop()
        runtime = media_proxy.ProxyRuntime(
            instance_id=7,
            bind=("127.0.0.1", 18097),
            server=MagicMock(),
            task=MagicMock(),
            sock=MagicMock(),
            signed_urls=MagicMock(),
        )
        manager._runtimes = {7: runtime}
        stop_started = asyncio.Event()
        release = asyncio.Event()

        async def slow_stop(_runtime):
            stop_started.set()
            await release.wait()

        with patch.object(manager, "_stop_runtime", side_effect=slow_stop), patch.object(
            media_proxy.database, "list_media_proxy_instances"
        ) as list_instances:
            stop_task = asyncio.create_task(manager.stop())
            await stop_started.wait()
            self.assertTrue(manager._stopping)
            self.assertFalse(manager.request_reconcile())
            reconcile_task = asyncio.create_task(manager.reconcile())
            await asyncio.sleep(0)
            self.assertFalse(reconcile_task.done())
            release.set()
            await stop_task
            result = await reconcile_task

        self.assertEqual(result, {"started": [], "stopped": [], "failed": {}})
        self.assertEqual(manager._runtimes, {})
        self.assertIsNone(manager._loop)
        list_instances.assert_not_called()

    async def test_stop_runtime_consumes_an_already_failed_task(self):
        from app.modules import media_proxy

        async def crash():
            raise RuntimeError("simulated proxy crash")

        failed_task = asyncio.create_task(crash())
        await asyncio.sleep(0)
        runtime = media_proxy.ProxyRuntime(
            instance_id=9,
            bind=("127.0.0.1", 18099),
            server=MagicMock(),
            task=failed_task,
            sock=MagicMock(),
            signed_urls=MagicMock(),
        )
        with patch.object(media_proxy, "_release_signed_url_cache"):
            await media_proxy.MediaProxyManager._stop_runtime(runtime)
        runtime.sock.close.assert_called_once_with()


class MediaProxyRuntimeProfileTests(unittest.TestCase):
    def test_runtime_resolves_configured_upstream(self):
        from app.modules import media_proxy
        import app.modules.media_server_profiles as profiles

        row = {
            "id": 9,
            "config_source": "configured:jellyfin",
            "server_type": "jellyfin",
            "upstream_url": "http://stale.invalid",
            "api_key": "",
            "listen_host": "127.0.0.1",
            "listen_port": 18099,
            "enabled": 1,
        }
        values = {
            "JELLYFIN_URL": "http://127.0.0.1:8096",
            "JELLYFIN_API_KEY": "current-key",
            "JELLYFIN_ENABLED": "true",
        }
        with patch.object(media_proxy.database, "get_media_proxy_instance", return_value=row), \
             patch.object(profiles.config, "get", side_effect=lambda key, default="": values.get(key, default)), \
             patch.object(profiles.config, "get_bool", return_value=True):
            resolved = media_proxy._resolved_instance(9)
        self.assertEqual(resolved["upstream_url"], "http://127.0.0.1:8096")
        self.assertEqual(resolved["api_key"], "current-key")

    def test_runtime_preserves_client_auth_boundary_instead_of_exposing_server_key(self):
        from app.modules import media_proxy

        request = type(
            "Request",
            (),
            {"headers": {}, "query_params": {}},
        )()
        self.assertEqual(media_proxy._upstream_request_headers(request), {})

        authenticated = type(
            "Request",
            (),
            {
                "headers": {"X-MediaBrowser-Token": "user-token"},
                "query_params": {},
            },
        )()
        self.assertEqual(
            media_proxy._upstream_request_headers(authenticated)["X-MediaBrowser-Token"],
            "user-token",
        )


class LocalPlaybackPassthroughTests(unittest.TestCase):
    def test_legacy_local_binding_does_not_rewrite_playback_info(self):
        import copy
        from app.modules import media_proxy

        source = {
            "Id": "local-source",
            "Path": "/media/Movies/example.mkv",
            "SupportsDirectPlay": False,
            "SupportsDirectStream": False,
            "SupportsTranscoding": True,
            "TranscodingUrl": "/Videos/item/master.m3u8",
        }
        payload = {"MediaSources": [copy.deepcopy(source)]}
        binding = {
            "id": 31,
            "instance_id": 7,
            "media_item_id": "item-local",
            "media_source_id": "local-source",
            "source_type": "local",
            "guangya_file_id": "",
            "local_relative_path": "Movies/example.mkv",
            "enabled": 1,
        }
        with patch.object(media_proxy.database, "get_media_proxy_binding", return_value=binding):
            rewritten, changed = media_proxy.rewrite_playback_info(payload, 7, "item-local")
        self.assertFalse(changed)
        self.assertEqual(rewritten["MediaSources"][0], source)

    def test_local_binding_creation_is_rejected(self):
        from app.routes.media_proxy_api import _validated_binding

        payload, error = _validated_binding(
            {
                "media_item_id": "item-local",
                "source_type": "local",
                "local_relative_path": "Movies/example.mkv",
            },
            {"local_root": "/media"},
        )
        self.assertIsNone(payload)
        self.assertEqual(error, "本地媒体由 Jellyfin/Emby 处理，不再支持本地绑定")

class MediaProxyTemplateTests(unittest.TestCase):
    def test_proxy_page_uses_configured_profiles_and_guangya_advanced_mapping(self):
        template = Path("app/templates/media_proxy.html").read_text(encoding="utf-8")
        self.assertIn("已配置媒体服务器", template)
        self.assertIn("高级光鸭播放映射", template)
        self.assertIn("本地视频继续由上游负责", template)
        self.assertIn("/api/media-proxy/profiles", template)
        self.assertNotIn("本地媒体根目录", template)
        self.assertNotIn("本地相对路径", template)
        self.assertNotIn("一期边界", template)
        self.assertNotIn("本地单段 Range/HEAD", template)

    def test_proxy_instance_modal_exposes_scoped_forwarded_header_trust(self):
        template = Path("app/templates/media_proxy.html").read_text(encoding="utf-8")
        css = Path("app/static/css/main.css").read_text(encoding="utf-8")

        self.assertIn('class="proxy-config-details proxy-custom-upstream"', template)
        self.assertIn('class="proxy-config-details proxy-forwarded-trust"', template)
        self.assertIn('id="proxyForwardedDetails"', template)
        self.assertIn('id="proxyTrustForwardedHeaders"', template)
        self.assertIn('id="proxyTrustedProxyCidrs"', template)
        self.assertIn("trust_forwarded_headers", template)
        self.assertIn("trusted_proxy_cidrs", template)
        self.assertIn(".proxy-config-details", css)
        self.assertIn(".proxy-config-details-body .form-group", css)
        self.assertIn(".proxy-trusted-cidrs-input", css)
        base = Path("app/templates/base.html").read_text(encoding="utf-8")
        self.assertIn("css/main.css') }}?v=20260831c", base)

    def test_proxy_playback_latency_is_split_by_stage_without_extra_columns(self):
        template = Path("app/templates/media_proxy.html").read_text(encoding="utf-8")
        css = Path("app/static/css/main.css").read_text(encoding="utf-8")

        self.assertIn("average_redirect_latency_ms", template)
        self.assertIn("average_playback_info_latency_ms", template)
        self.assertIn("internal_latency_ms", template)
        self.assertIn("302 平均", template)
        self.assertIn("上游阶段", template)
        self.assertIn("proxy-session-latency-detail", template)
        self.assertIn(".proxy-session-latency-detail", css)
        self.assertIn("min-height: 54px", css)

    def test_proxy_async_regions_reserve_stable_space(self):
        css = Path("app/static/css/main.css").read_text(encoding="utf-8")
        self.assertIn(".proxy-profile-grid", css)
        self.assertIn("min-height:", css[css.index(".proxy-profile-grid"):])
        self.assertIn(".proxy-instance-list", css)
        self.assertIn(".media-config-modal[hidden]", css)
