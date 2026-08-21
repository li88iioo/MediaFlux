"""混合 Emby/Jellyfin 302 自动识别回归测试。"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import socket
import threading
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi.testclient import TestClient

from app.modules import media_proxy


class _FakeUpstreamResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        body: bytes = b"upstream",
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        values = {"content-type": content_type}
        values.update(headers or {})
        self.headers = httpx.Headers(values)
        self.closed = False

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        self.closed = True

    async def aiter_raw(self):
        yield self._body

    async def aiter_bytes(self):
        yield self._body


class _FakeAsyncClient:
    responses: list[_FakeUpstreamResponse] = []
    requests: list[httpx.Request] = []
    init_kwargs: list[dict] = []
    instances: list["_FakeAsyncClient"] = []
    send_streams: list[bool] = []

    def __init__(self, *args, **kwargs) -> None:
        self.closed = False
        self.__class__.init_kwargs.append(dict(kwargs))
        self.__class__.instances.append(self)

    def build_request(self, method: str, url: str, **kwargs) -> httpx.Request:
        return httpx.Request(
            method,
            url,
            headers=kwargs.get("headers"),
            content=kwargs.get("content"),
            extensions=kwargs.get("extensions"),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.aclose()
        return False

    async def send(self, request: httpx.Request, stream: bool = False) -> _FakeUpstreamResponse:
        self.__class__.requests.append(request)
        self.__class__.send_streams.append(bool(stream))
        if not self.__class__.responses:
            raise AssertionError("测试未配置上游响应")
        return self.__class__.responses.pop(0)

    async def aclose(self) -> None:
        self.closed = True


class _FakeAuthAsyncClient:
    requests: list[httpx.Request] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def build_request(self, method: str, url: str, **kwargs) -> httpx.Request:
        return httpx.Request(
            method,
            url,
            headers=kwargs.get("headers"),
            extensions=kwargs.get("extensions"),
        )

    async def send(self, request: httpx.Request):
        self.__class__.requests.append(request)
        return SimpleNamespace(status_code=200)


class _FakeGuangYaClient:
    calls: list[str] = []
    call_options: list[dict] = []
    results: dict[str, list[str | None | Exception]] = {}

    def __init__(self) -> None:
        self.logged_in = True

    def get_download_url(self, file_id: str, **kwargs) -> str | None:
        self.__class__.calls.append(file_id)
        self.__class__.call_options.append(dict(kwargs))
        values = self.__class__.results.get(file_id, [])
        result = values.pop(0) if values else f"https://signed.invalid/{file_id}"
        if isinstance(result, Exception):
            raise result
        return result


class HybridMediaProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeAsyncClient.responses = []
        _FakeAsyncClient.requests = []
        _FakeAsyncClient.init_kwargs = []
        _FakeAsyncClient.instances = []
        _FakeAsyncClient.send_streams = []
        _FakeGuangYaClient.calls = []
        _FakeGuangYaClient.call_options = []
        _FakeGuangYaClient.results = {}
        media_proxy._dynamic_guangya_mappings.clear()
        media_proxy._item_level_binding_scopes.clear()
        if hasattr(media_proxy, "_playback_grants"):
            media_proxy._playback_grants.clear()

    @staticmethod
    def _instance() -> dict:
        return {
            "id": 7,
            "enabled": 1,
            "upstream_url": "http://127.0.0.1:8096",
            "local_root": "",
        }

    def test_saved_instance_probe_pins_host_sni_and_bounds_response(self):
        upstream = _FakeUpstreamResponse(
            body=b"{}", content_type="application/json"
        )
        _FakeAsyncClient.responses = [upstream]
        row = {
            "id": 7, "config_source": "custom", "server_type": "jellyfin",
            "upstream_url": "https://media.example:8920",
            "api_key": "server-token", "enabled": 1,
        }
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=row),
            patch("app.modules.media_proxy._resolve_upstream_addresses", return_value=("203.0.113.7",)),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
        ):
            result = asyncio.run(media_proxy.probe_media_proxy_instance(7))

        self.assertEqual(result["status_code"], 200)
        self.assertEqual(_FakeAsyncClient.send_streams, [True])
        request = _FakeAsyncClient.requests[0]
        self.assertEqual(str(request.url), "https://203.0.113.7:8920/System/Info/Public")
        self.assertEqual(request.headers["Host"], "media.example:8920")
        self.assertEqual(request.headers["X-Emby-Token"], "server-token")
        self.assertEqual(request.extensions["sni_hostname"], "media.example")
        self.assertTrue(upstream.closed)

    def test_saved_instance_probe_closes_oversize_response(self):
        upstream = _FakeUpstreamResponse(
            body=b"{}", content_type="application/json",
            headers={"content-length": "5"},
        )
        _FakeAsyncClient.responses = [upstream]
        row = {
            "id": 7, "config_source": "custom", "server_type": "jellyfin",
            "upstream_url": "http://127.0.0.1:8096", "api_key": "",
            "enabled": 1,
        }
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=row),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy._playback_info_response_limit", return_value=4),
        ):
            with self.assertRaises(media_proxy.ProxyUpstreamBodyTooLarge):
                asyncio.run(media_proxy.probe_media_proxy_instance(7))
        self.assertTrue(upstream.closed)

    def _client(self):
        app = media_proxy.create_proxy_app(7)
        stack = ExitStack()
        stack.enter_context(
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance())
        )
        stack.enter_context(patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient))
        stack.enter_context(
            patch("app.modules.media_proxy._client_is_authorized", new=AsyncMock(return_value=True))
        )
        return stack, TestClient(app, raise_server_exceptions=False)

    @staticmethod
    def _grant_file(token: str, item_id: str, source_id: str, file_id: str,
                    binding: dict | None = None) -> None:
        media_proxy._playback_grants.register(
            media_proxy._auth_scope_fingerprint(token),
            7,
            item_id,
            source_id,
            source_type="guangya" if binding else "dynamic",
            file_id=file_id,
            binding_signature=media_proxy._binding_signature(binding) if binding else "",
        )

    def test_manual_binding_playback_scope_isolated_between_two_valid_tokens(self):
        payload = {
            "MediaSources": [{"Id": "manual-source", "Path": "/mounted/manual.mkv"}]
        }
        binding = {
            "id": 70,
            "source_type": "guangya",
            "guangya_file_id": "manual-acl-file",
            "media_source_id": "manual-source",
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            _FakeUpstreamResponse(status_code=403, body=b"token-b-denied"),
        ]
        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=binding),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch("app.modules.media_proxy._client_is_authorized", new=AsyncMock(return_value=True)),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get("/Items/acl-manual/PlaybackInfo?api_key=token-a")
            stream_path = playback.json()["MediaSources"][0]["Path"]
            token_a = client.get(f"{stream_path}&api_key=token-a", follow_redirects=False)
            token_b = client.get(f"{stream_path}&api_key=token-b", follow_redirects=False)

        self.assertEqual(token_a.status_code, 302)
        self.assertEqual(token_a.headers["location"], "https://signed.invalid/manual-acl-file")
        self.assertEqual(token_b.status_code, 403)
        self.assertEqual(token_b.content, b"token-b-denied")
        self.assertEqual(_FakeGuangYaClient.calls, ["manual-acl-file"])

    def test_dynamic_playback_scope_isolated_between_two_valid_tokens(self):
        payload = {
            "MediaSources": [{
                "Id": "dynamic-source",
                "Path": "/playgy/dynamic-acl-file/e/100/Movie.mkv",
            }]
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            _FakeUpstreamResponse(status_code=403, body=b"other-user-denied"),
        ]
        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch("app.modules.media_proxy._client_is_authorized", new=AsyncMock(return_value=True)),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get("/Items/acl-dynamic/PlaybackInfo?api_key=token-a")
            stream_path = playback.json()["MediaSources"][0]["Path"]
            token_a = client.get(f"{stream_path}&api_key=token-a", follow_redirects=False)
            token_b = client.get(f"{stream_path}&api_key=token-b", follow_redirects=False)

        self.assertEqual(token_a.status_code, 302)
        self.assertEqual(token_a.headers["location"], "https://signed.invalid/dynamic-acl-file")
        self.assertEqual(token_b.status_code, 403)
        self.assertEqual(token_b.content, b"other-user-denied")
        self.assertEqual(_FakeGuangYaClient.calls, ["dynamic-acl-file"])

    def test_non_2xx_playback_info_does_not_grant_manual_or_dynamic_playgy_access(self):
        cases = (
            (
                "manual",
                {"MediaSources": [{"Id": "manual-source", "Path": "/mounted/manual.mkv"}]},
                {
                    "id": 71,
                    "source_type": "guangya",
                    "guangya_file_id": "manual-denied-file",
                    "media_source_id": "manual-source",
                },
                "manual-denied-file",
            ),
            (
                "dynamic",
                {
                    "MediaSources": [{
                        "Id": "dynamic-source",
                        "Path": "/playgy/dynamic-denied-file/e/100/Movie.mkv",
                    }]
                },
                None,
                "dynamic-denied-file",
            ),
        )

        for source_type, payload, binding, file_id in cases:
            with self.subTest(source_type=source_type):
                media_proxy._dynamic_guangya_mappings.clear()
                media_proxy._item_level_binding_scopes.clear()
                media_proxy._playback_grants.clear()
                _FakeGuangYaClient.calls = []
                _FakeAsyncClient.responses = [
                    _FakeUpstreamResponse(
                        status_code=403,
                        body=json.dumps(payload).encode("utf-8"),
                        content_type="application/json",
                    )
                ]
                app = media_proxy.create_proxy_app(7)
                with (
                    patch(
                        "app.modules.media_proxy.database.get_media_proxy_instance",
                        return_value=self._instance(),
                    ),
                    patch(
                        "app.modules.media_proxy.database.get_media_proxy_binding",
                        return_value=binding,
                    ),
                    patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
                    patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
                    TestClient(app, raise_server_exceptions=False) as client,
                ):
                    playback = client.get(
                        f"/Items/{source_type}-denied/PlaybackInfo?api_key=same-token"
                    )
                    direct = client.get(
                        f"/playgy/{file_id}?api_key=same-token",
                        follow_redirects=False,
                    )

                self.assertEqual(playback.status_code, 403)
                self.assertEqual(direct.status_code, 403)
                self.assertEqual(_FakeGuangYaClient.calls, [])

    def test_direct_playgy_rejects_valid_token_without_file_playback_scope(self):
        stack, client = self._client()
        with stack, patch(
            "app.modules.media_proxy.database.get_media_proxy_binding", return_value=None
        ), client:
            response = client.get(
                "/playgy/known-but-ungranted/e/1/Movie.mkv?api_key=valid-token",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(_FakeGuangYaClient.calls, [])


    def test_upstream_timeout_keeps_stream_reads_unbounded_but_bounds_connect_and_write(self):
        _FakeAsyncClient.responses = [_FakeUpstreamResponse(body=b"ok")]
        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/System/Info")

        self.assertEqual(response.status_code, 200)
        timeout = _FakeAsyncClient.init_kwargs[0]["timeout"]
        self.assertIsNone(timeout.read)
        self.assertEqual(timeout.connect, 10.0)
        self.assertEqual(timeout.write, 30.0)
        self.assertEqual(timeout.pool, 5.0)

    def test_upstream_client_is_reused_and_closed_with_proxy_lifespan(self):
        first_upstream = _FakeUpstreamResponse(body=b"first")
        second_upstream = _FakeUpstreamResponse(body=b"second")
        _FakeAsyncClient.responses = [first_upstream, second_upstream]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            first = client.get("/System/Info")
            second = client.get("/System/Info/Public")
            self.assertEqual(first.content, b"first")
            self.assertEqual(second.content, b"second")
            self.assertEqual(len(_FakeAsyncClient.instances), 1)
            self.assertFalse(_FakeAsyncClient.instances[0].closed)
            self.assertTrue(first_upstream.closed)
            self.assertTrue(second_upstream.closed)

        self.assertTrue(_FakeAsyncClient.instances[0].closed)

    def test_oversized_request_body_is_rejected_before_contacting_upstream(self):
        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy._proxy_request_body_limit", return_value=8),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post("/Items", content=b"123456789")

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"], "请求体过大")
        self.assertEqual(_FakeAsyncClient.init_kwargs, [])

    def test_runtime_payload_limits_are_clamped(self):
        with patch("app.modules.media_proxy.get", return_value="99999"):
            self.assertEqual(
                media_proxy._playback_info_response_limit(),
                64 * 1024 * 1024,
            )
            self.assertEqual(
                media_proxy._proxy_websocket_message_limit(),
                64 * 1024 * 1024,
            )

    def test_sensitive_query_tokens_are_removed_from_upstream_url_and_forwarded_as_header(self):
        _FakeAsyncClient.responses = [_FakeUpstreamResponse(body=b"upstream")]
        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/System/Info?api_key=query-secret&X-Emby-Token=second-secret"
                "&X-MediaBrowser-Token=third-secret&_mfps=internal-session&foo=visible"
            )

        self.assertEqual(response.status_code, 200)
        request = _FakeAsyncClient.requests[0]
        self.assertEqual(str(request.url), "http://127.0.0.1:8096/System/Info?foo=visible")
        self.assertEqual(request.headers["X-Emby-Token"], "query-secret")
        serialized = str(request.url)
        self.assertNotIn("query-secret", serialized)
        self.assertNotIn("second-secret", serialized)
        self.assertNotIn("third-secret", serialized)
        self.assertNotIn("internal-session", serialized)

    def test_websocket_target_removes_sensitive_query_tokens(self):
        websocket = SimpleNamespace(
            url=SimpleNamespace(
                query="api_key=socket-secret&X-Emby-Token=other-secret&device=visible"
            ),
            query_params=httpx.QueryParams(
                "api_key=socket-secret&X-Emby-Token=other-secret&device=visible"
            ),
        )
        target = media_proxy._websocket_upstream_url(
            "http://127.0.0.1:8096/emby",
            websocket,
            "emby/socket",
        )

        self.assertEqual(
            target,
            "ws://127.0.0.1:8096/emby/socket?device=visible",
        )
        self.assertNotIn("socket-secret", target)
        self.assertNotIn("other-secret", target)

    def test_logger_filter_redacts_media_server_tokens_in_headers_and_query(self):
        from app import logger as app_logger

        record = logging.LogRecord(
            "media-proxy-test",
            logging.INFO,
            __file__,
            1,
            "GET /x?api_key=query-secret&X-Emby-Token=query-emby "
            "X-MediaBrowser-Token=header-browser "
            'Authorization: MediaBrowser Token="authorization-secret"',
            (),
            None,
        )
        app_logger._RedactFilter().filter(record)
        message = record.getMessage()

        for secret in (
            "query-secret",
            "query-emby",
            "header-browser",
            "authorization-secret",
        ):
            self.assertNotIn(secret, message)
        self.assertIn("********", message)

    def test_logger_filter_redacts_quoted_mapping_keys_without_changing_plain_text(self):
        from app import logger as app_logger

        record = logging.LogRecord(
            "media-proxy-test",
            logging.INFO,
            __file__,
            1,
            "headers={'X-Emby-Token': 'header-secret'} "
            "payload={'api_key':'query-secret', 'name': 'visible'} "
            'json={"X-MediaBrowser-Token":"browser-secret","note":"ordinary text"} '
            "Authorization: Bearer bearer-secret "
            "GET /x?api_key=url-secret&foo=visible",
            (),
            None,
        )
        app_logger._RedactFilter().filter(record)

        self.assertEqual(
            record.getMessage(),
            "headers={'X-Emby-Token': '********'} "
            "payload={'api_key':'********', 'name': 'visible'} "
            'json={"X-MediaBrowser-Token":"********","note":"ordinary text"} '
            "Authorization: Bearer ******** "
            "GET /x?api_key=********&foo=visible",
        )

    def test_logger_filter_redacts_private_tracker_query_credentials(self):
        from app import logger as app_logger

        message = app_logger.redact_sensitive_text(
            "GET https://tracker.invalid/rss?passkey=pass-secret&authkey=auth-secret&rsskey=rss-secret&safe=visible"
        )
        for secret in ("pass-secret", "auth-secret", "rss-secret"):
            self.assertNotIn(secret, message)
        self.assertIn("safe=visible", message)

    def test_logger_filter_redacts_telegram_bot_url_and_suppresses_vendor_traceback(self):
        from app import logger as app_logger

        summary = logging.LogRecord(
            "TeleBot",
            logging.ERROR,
            __file__,
            1,
            "Infinity polling exception: ConnectTimeoutError GET https://api.telegram.org/bot123456:fakeSecret/getMe timed out",
            (),
            None,
        )
        log_filter = app_logger._RedactFilter()
        self.assertTrue(log_filter.filter(summary))
        self.assertEqual(
            summary.getMessage(),
            "Telegram Bot 连接超时（ConnectTimeout），将在后台自动重试",
        )
        self.assertNotIn("fakeSecret", summary.getMessage())
        self.assertNotIn("\n", summary.getMessage())

        cases = (
            (
                "Threaded polling exception: HTTPSConnectionPool: ReadTimeoutError(Read timed out)",
                "Telegram Bot 读取超时（ReadTimeout），将在后台自动重试",
            ),
            (
                "Polling exception: SSLError certificate_verify_failed",
                "Telegram Bot TLS/SSL 连接异常（SSL），将在后台自动重试",
            ),
            (
                "Infinity polling exception: NewConnectionError network is unreachable",
                "Telegram Bot 网络连接异常（ConnectionError），将在后台自动重试",
            ),
        )
        for raw_message, expected in cases:
            record = logging.LogRecord(
                "TeleBot", logging.ERROR, __file__, 1, raw_message, (), None,
            )
            self.assertTrue(log_filter.filter(record))
            self.assertEqual(record.getMessage(), expected)

        traceback_record = logging.LogRecord(
            "TeleBot",
            logging.ERROR,
            __file__,
            1,
            "Exception traceback:\nrequests.exceptions.ConnectTimeout: secret trace",
            (),
            None,
        )
        self.assertFalse(log_filter.filter(traceback_record))

        application_traceback = logging.LogRecord(
            "app.worker",
            logging.ERROR,
            __file__,
            1,
            "Exception traceback:\napplication failure",
            (),
            None,
        )
        self.assertTrue(log_filter.filter(application_traceback))
        self.assertIn("application failure", application_traceback.getMessage())

        disconnected = logging.LogRecord(
            "TeleBot",
            logging.ERROR,
            __file__,
            1,
            "Infinity polling exception: ('Connection aborted.', RemoteDisconnected('Remote end closed connection'))",
            (),
            None,
        )
        self.assertFalse(log_filter.filter(disconnected))
        self.assertIn("RemoteDisconnected", disconnected.getMessage())

    def test_configure_telebot_logging_removes_vendor_handler(self):
        from app import logger as app_logger

        vendor_logger = logging.getLogger("TeleBot")
        original_handlers = list(vendor_logger.handlers)
        original_propagate = vendor_logger.propagate
        original_level = vendor_logger.level
        try:
            vendor_logger.handlers[:] = [logging.StreamHandler()]
            vendor_logger.propagate = False
            vendor_logger.setLevel(logging.ERROR)

            app_logger.configure_telebot_logging()

            self.assertEqual(vendor_logger.handlers, [])
            self.assertTrue(vendor_logger.propagate)
            self.assertEqual(vendor_logger.level, logging.INFO)
        finally:
            vendor_logger.handlers[:] = original_handlers
            vendor_logger.propagate = original_propagate
            vendor_logger.setLevel(original_level)

    def test_manual_binding_direct_stream_url_carries_media_source_id(self):
        payload = {"MediaSources": [{"Id": "version A/1080p?", "Path": "/media/a.mkv"}]}
        binding = {
            "id": 31,
            "source_type": "guangya",
            "guangya_file_id": "manual-a",
            "media_source_id": "version A/1080p?",
        }
        with patch(
            "app.modules.media_proxy.database.get_media_proxy_binding",
            return_value=binding,
        ):
            rewritten, changed = media_proxy.rewrite_playback_info(payload, 7, "multi-item")

        self.assertTrue(changed)
        source = rewritten["MediaSources"][0]
        expected = "/Videos/multi-item/stream?MediaSourceId=version%20A%2F1080p%3F"
        self.assertEqual(source["Path"], expected)
        self.assertEqual(source["DirectStreamUrl"], expected)

    def test_multi_source_client_using_manual_binding_path_routes_exact_version(self):
        payload = {
            "MediaSources": [
                {"Id": "cloud version/4k", "Path": "/mounted/cloud.mkv"},
                {"Id": "local-version", "Path": "/media/local.mkv", "Protocol": "File"},
            ]
        }
        binding = {
            "id": 35,
            "source_type": "guangya",
            "guangya_file_id": "manual-path-file",
            "media_source_id": "cloud version/4k",
        }

        def binding_lookup(_instance_id, _item_id, source_id=""):
            return binding if source_id in {"cloud version/4k", ""} else None

        with patch(
            "app.modules.media_proxy.database.get_media_proxy_binding",
            side_effect=binding_lookup,
        ):
            rewritten, changed = media_proxy.rewrite_playback_info(
                payload,
                7,
                "path-item",
                auth_scope=media_proxy._auth_scope_fingerprint("client-token"),
            )

        self.assertTrue(changed)
        cloud, local = rewritten["MediaSources"]
        expected_path = "/Videos/path-item/stream?MediaSourceId=cloud%20version%2F4k"
        self.assertEqual(cloud["Path"], expected_path)
        self.assertEqual(cloud["DirectStreamUrl"], expected_path)
        self.assertEqual(local, payload["MediaSources"][1])

        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                side_effect=binding_lookup,
            ),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch("app.modules.media_proxy._client_is_authorized", new=AsyncMock(return_value=True)),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                f"{cloud['Path']}&api_key=client-token",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://signed.invalid/manual-path-file")
        self.assertEqual(_FakeGuangYaClient.calls, ["manual-path-file"])

    def test_item_level_binding_does_not_rewrite_mixed_media_sources(self):
        payload = {
            "MediaSources": [
                {"Id": "cloud-version", "Path": "/mounted/cloud.mkv"},
                {"Id": "local-version", "Path": "/media/local.mkv", "Protocol": "File"},
            ]
        }
        before = copy.deepcopy(payload)
        item_binding = {
            "id": 32,
            "source_type": "guangya",
            "guangya_file_id": "item-level-file",
            "media_source_id": "",
        }
        with patch(
            "app.modules.media_proxy.database.get_media_proxy_binding",
            return_value=item_binding,
        ):
            rewritten, changed = media_proxy.rewrite_playback_info(payload, 7, "mixed-item")

        self.assertFalse(changed)
        self.assertEqual(rewritten, before)

    def test_item_level_binding_cannot_hijack_explicit_mixed_source_stream(self):
        item_binding = {
            "id": 33,
            "source_type": "guangya",
            "guangya_file_id": "item-level-file",
            "media_source_id": "",
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(status_code=206, body=b"local-version")
        ]
        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=item_binding),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch("app.modules.media_proxy._client_is_authorized", new=AsyncMock(return_value=True)),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Videos/mixed-item/stream?MediaSourceId=local-version&api_key=client-token",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"local-version")
        self.assertEqual(_FakeGuangYaClient.calls, [])

    def test_item_level_binding_applies_to_single_source_and_routes_that_source(self):
        payload = {"MediaSources": [{"Id": "only-source", "Path": "/media/only.mkv"}]}
        item_binding = {
            "id": 34,
            "source_type": "guangya",
            "guangya_file_id": "only-file",
            "media_source_id": "",
        }
        with patch(
            "app.modules.media_proxy.database.get_media_proxy_binding",
            return_value=item_binding,
        ):
            rewritten, changed = media_proxy.rewrite_playback_info(
                payload,
                7,
                "single-item",
                auth_scope=media_proxy._auth_scope_fingerprint("client-token"),
            )

        self.assertTrue(changed)
        source = rewritten["MediaSources"][0]
        direct_url = source["DirectStreamUrl"]
        self.assertEqual(
            direct_url,
            "/Videos/single-item/stream?MediaSourceId=only-source",
        )
        self.assertEqual(source["Path"], direct_url)

        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=item_binding),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch("app.modules.media_proxy._client_is_authorized", new=AsyncMock(return_value=True)),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                f"{direct_url}&api_key=client-token",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://signed.invalid/only-file")

    def test_authorization_media_browser_token_is_verified_with_original_header(self):
        _FakeAuthAsyncClient.requests = []
        authorization = 'MediaBrowser Token="authorization-token", Client="MediaFlux"'
        request = SimpleNamespace(
            headers=httpx.Headers({"Authorization": authorization}),
            query_params={"redirect": "http://attacker.invalid"},
        )
        instance = {**self._instance(), "upstream_url": "http://127.0.0.1:8096/emby"}
        with patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAuthAsyncClient):
            allowed = asyncio.run(media_proxy._client_is_authorized(instance, request))

        self.assertTrue(allowed)
        upstream_request = _FakeAuthAsyncClient.requests[0]
        self.assertEqual(str(upstream_request.url), "http://127.0.0.1:8096/emby/Users/Me")
        self.assertEqual(upstream_request.headers["Authorization"], authorization)
        self.assertEqual(upstream_request.headers["X-Emby-Token"], "authorization-token")
        self.assertEqual(upstream_request.headers["Host"], "127.0.0.1:8096")

    def test_x_emby_authorization_token_is_verified_with_original_header(self):
        _FakeAuthAsyncClient.requests = []
        authorization = 'MediaBrowser Client="Emby", Token="emby-auth-token"'
        request = SimpleNamespace(
            headers=httpx.Headers({"X-Emby-Authorization": authorization}),
            query_params={},
        )
        with patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAuthAsyncClient):
            allowed = asyncio.run(media_proxy._client_is_authorized(self._instance(), request))

        self.assertTrue(allowed)
        upstream_request = _FakeAuthAsyncClient.requests[0]
        self.assertEqual(str(upstream_request.url), "http://127.0.0.1:8096/Users/Me")
        self.assertEqual(upstream_request.headers["X-Emby-Authorization"], authorization)
        self.assertEqual(upstream_request.headers["X-Emby-Token"], "emby-auth-token")
        self.assertEqual(upstream_request.headers["Host"], "127.0.0.1:8096")

    def test_validate_upstream_rejects_metadata_and_unsafe_ip_classes_but_allows_lan(self):
        allowed = [
            "http://127.0.0.1:8096",
            "http://192.168.1.20:8096",
            "http://10.10.0.5:8096/emby",
        ]
        for url in allowed:
            with self.subTest(allowed=url):
                self.assertEqual(media_proxy.validate_upstream_url(url), url)

        rejected = [
            "http://0.0.0.0:8096",
            "http://169.254.169.254/latest/meta-data",
            "http://224.0.0.1:8096",
            "http://[::]:8096",
            "http://[ff02::1]:8096",
            "http://[fd00:ec2::254]/latest/meta-data",
            "http://[::ffff:169.254.169.254]/latest/meta-data",
            "http://[::ffff:100.100.100.200]/latest/meta-data",
            "http://100.100.100.200/latest/meta-data",
            "http://metadata.google.internal/computeMetadata/v1/",
        ]
        for url in rejected:
            with self.subTest(rejected=url):
                with self.assertRaises(ValueError):
                    media_proxy.validate_upstream_url(url)

    def test_validate_upstream_rejects_domain_when_any_resolved_ip_is_unsafe(self):
        resolved = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 8096)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:169.254.169.254", 8096, 0, 0)),
        ]
        with patch("app.modules.media_proxy.socket.getaddrinfo", return_value=resolved) as resolver:
            with self.assertRaisesRegex(ValueError, "云元数据"):
                media_proxy.validate_upstream_url("http://media.internal.example:8096/emby")

        resolver.assert_called_once()

    def test_runtime_request_pins_verified_dns_snapshot_and_preserves_host_and_sni(self):
        resolved = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 8096)),
        ]
        instance = {**self._instance(), "upstream_url": "http://media.internal.example:8096/emby"}
        _FakeAsyncClient.responses = [_FakeUpstreamResponse(body=b"system-info")]
        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=instance),
            patch("app.modules.media_proxy.socket.getaddrinfo", return_value=resolved) as resolver,
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/emby/System/Info?api_key=client-token")

        self.assertEqual(response.status_code, 200)
        resolver.assert_called_once()
        upstream_request = _FakeAsyncClient.requests[0]
        self.assertEqual(
            str(upstream_request.url),
            "http://192.168.1.20:8096/emby/System/Info",
        )
        self.assertEqual(upstream_request.headers["Host"], "media.internal.example:8096")
        self.assertEqual(
            upstream_request.extensions["sni_hostname"],
            "media.internal.example",
        )

    def test_validate_upstream_dns_failure_keeps_offline_hostname_configurable(self):
        url = "http://offline-media.invalid:8096/emby"
        with patch(
            "app.modules.media_proxy.socket.getaddrinfo",
            side_effect=socket.gaierror(socket.EAI_NONAME, "name not known"),
        ) as resolver:
            self.assertEqual(media_proxy.validate_upstream_url(url), url)

        resolver.assert_called_once()

    def test_upstream_with_emby_base_path_is_not_duplicated(self):
        instance = {**self._instance(), "upstream_url": "http://127.0.0.1:8096/emby"}
        _FakeAsyncClient.responses = [_FakeUpstreamResponse(body=b"system-info")]
        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=instance),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/emby/System/Info?api_key=client-token")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"system-info")
        self.assertEqual(
            str(_FakeAsyncClient.requests[0].url),
            "http://127.0.0.1:8096/emby/System/Info",
        )
        self.assertEqual(_FakeAsyncClient.requests[0].headers["X-Emby-Token"], "client-token")
        websocket = SimpleNamespace(url=SimpleNamespace(query="api_key=client-token"))
        self.assertEqual(
            media_proxy._websocket_upstream_url(
                "http://127.0.0.1:8096/emby",
                websocket,
                "emby/socket",
            ),
            "ws://127.0.0.1:8096/emby/socket?api_key=client-token",
        )

    def test_playback_info_drops_stale_content_encoding_and_length_after_aread(self):
        payload = {"MediaSources": [{"Id": "local", "Path": "/media/local.mkv"}]}
        raw = json.dumps(payload).encode("utf-8")
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=raw,
                content_type="application/json",
                headers={"content-encoding": "gzip", "content-length": "9999"},
            )
        ]
        app = media_proxy.create_proxy_app(7)
        route = next(
            route for route in app.routes
            if getattr(route, "path", None) == "/{path:path}" and getattr(route, "methods", None)
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/Items/local-item/PlaybackInfo",
            "raw_path": b"/Items/local-item/PlaybackInfo",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        from starlette.requests import Request
        request = Request(scope, receive)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
        ):
            response = asyncio.run(route.endpoint(request, path="Items/local-item/PlaybackInfo"))

        self.assertNotIn("content-encoding", response.headers)
        self.assertNotEqual(response.headers.get("content-length"), "9999")
        self.assertEqual(response.body, raw)

    def test_playback_info_rejects_oversize_streamed_response_and_closes_upstream(self):
        upstream = _FakeUpstreamResponse(
            body=b"12345",
            content_type="application/json",
        )
        _FakeAsyncClient.responses = [upstream]
        app = media_proxy.create_proxy_app(7)
        route = next(
            route for route in app.routes
            if getattr(route, "path", None) == "/{path:path}" and getattr(route, "methods", None)
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/Items/local-item/PlaybackInfo",
            "raw_path": b"/Items/local-item/PlaybackInfo",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        from starlette.requests import Request
        request = Request(scope, receive)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy._playback_info_response_limit", return_value=4),
        ):
            response = asyncio.run(route.endpoint(request, path="Items/local-item/PlaybackInfo"))

        self.assertEqual(response.status_code, 502)
        self.assertEqual(json.loads(response.body), {"error": "上游响应过大"})
        self.assertTrue(upstream.closed)

    def test_playback_info_rejects_oversize_declared_content_length(self):
        upstream = _FakeUpstreamResponse(
            body=b"{}",
            content_type="application/json",
            headers={"content-length": "5"},
        )
        with self.assertRaises(media_proxy.ProxyUpstreamBodyTooLarge):
            asyncio.run(media_proxy._read_bounded_upstream_body(upstream, 4))

    def test_guangya_redirect_disables_caching_and_referrers(self):
        self._grant_file("client-token", "header-item", "header-source", "private-file")
        stack, client = self._client()
        with stack, patch(
            "app.modules.media_proxy.database.get_media_proxy_binding", return_value=None
        ), client:
            response = client.get(
                "/playgy/private-file/e/1/a.mkv?api_key=client-token",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        cache_control = response.headers.get("cache-control", "")
        self.assertIn("private", cache_control)
        self.assertIn("no-store", cache_control)
        self.assertIn("no-cache", cache_control)
        self.assertEqual(response.headers.get("pragma"), "no-cache")
        self.assertEqual(response.headers.get("referrer-policy"), "no-referrer")

    def test_dynamic_mapping_uses_bounded_lru_eviction(self):
        try:
            mappings = media_proxy.DynamicGuangYaMappings(max_entries=2)
        except TypeError as exc:
            self.fail(f"动态映射缺少容量上限: {exc}")
        mappings.register(7, "item-a", "source", "file-a")
        mappings.register(7, "item-b", "source", "file-b")
        self.assertEqual(mappings.get(7, "item-a", "source"), "file-a")
        mappings.register(7, "item-c", "source", "file-c")

        self.assertEqual(mappings.get(7, "item-a", "source"), "file-a")
        self.assertIsNone(mappings.get(7, "item-b", "source"))
        self.assertEqual(mappings.get(7, "item-c", "source"), "file-c")
        self.assertEqual(mappings.entry_count, 2)

    def test_signed_url_cache_uses_bounded_lru_eviction(self):
        try:
            cache = media_proxy.SignedUrlCache(ttl_seconds=60, max_entries=2)
        except TypeError as exc:
            self.fail(f"签名 URL 缓存缺少容量上限: {exc}")
        calls: list[str] = []

        async def fetch(file_id: str, suffix: str = "") -> str:
            calls.append(file_id)
            return f"https://signed.invalid/{file_id}{suffix}"

        async def scenario():
            await cache.get_or_fetch("file-a", lambda: fetch("file-a"))
            await cache.get_or_fetch("file-b", lambda: fetch("file-b"))
            await cache.get_or_fetch("file-a", lambda: fetch("file-a", "-unexpected"))
            await cache.get_or_fetch("file-c", lambda: fetch("file-c"))
            return await cache.get_or_fetch("file-b", lambda: fetch("file-b", "-refetched"))

        refetched = asyncio.run(scenario())
        self.assertEqual(refetched, "https://signed.invalid/file-b-refetched")
        self.assertEqual(calls, ["file-a", "file-b", "file-c", "file-b"])
        self.assertEqual(cache.entry_count, 2)

    def test_rewrite_auto_detects_absolute_and_relative_playgy_and_preserves_other_sources(self):
        local_source = {
            "Id": "local-source",
            "Path": "/srv/media/movie.mkv",
            "SupportsDirectPlay": False,
            "Protocol": "File",
        }
        unknown_source = {
            "Id": "unknown-source",
            "Path": "https://other.invalid/video.mkv",
            "SupportsTranscoding": True,
        }
        payload = {
            "MediaSources": [
                {
                    "Id": "cloud-absolute",
                    "Path": "https://media.invalid/playgy/file-a/etag/100/Movie.mkv",
                    "SupportsDirectPlay": False,
                    "SupportsTranscoding": True,
                    "TranscodingUrl": "/Videos/item/master.m3u8",
                },
                {
                    "Id": "cloud-relative",
                    "Path": "/playgy/file-b/etag/200/Episode.mkv",
                    "SupportsDirectStream": False,
                },
                local_source,
                unknown_source,
            ]
        }
        local_before = copy.deepcopy(local_source)
        unknown_before = copy.deepcopy(unknown_source)

        with patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None):
            rewritten, changed = media_proxy.rewrite_playback_info(payload, 7, "item-1")

        self.assertTrue(changed)
        absolute, relative, local_after, unknown_after = rewritten["MediaSources"]
        self.assertEqual(
            absolute["DirectStreamUrl"],
            "/Videos/item-1/stream?MediaSourceId=cloud-absolute",
        )
        self.assertEqual(absolute["Path"], absolute["DirectStreamUrl"])
        self.assertTrue(absolute["SupportsDirectPlay"])
        self.assertTrue(absolute["SupportsDirectStream"])
        self.assertFalse(absolute["SupportsTranscoding"])
        self.assertNotIn("TranscodingUrl", absolute)
        self.assertEqual(
            relative["DirectStreamUrl"],
            "/Videos/item-1/stream?MediaSourceId=cloud-relative",
        )
        self.assertEqual(local_after, local_before)
        self.assertEqual(unknown_after, unknown_before)
        self.assertEqual(
            media_proxy._dynamic_guangya_mappings.get(7, "item-1", "cloud-absolute"),
            "file-a",
        )
        self.assertEqual(
            media_proxy._dynamic_guangya_mappings.get(7, "item-1", "cloud-relative"),
            "file-b",
        )

    def test_emby_playback_info_path_is_rewritten_with_emby_stream_prefix(self):
        payload = {
            "MediaSources": [
                {"Id": "cloud", "Path": "/playgy/file-emby/e/1/a.mkv"},
                {"Id": "local", "Path": "/media/local.mkv"},
            ]
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/emby/Items/item-emby/PlaybackInfo?api_key=client-token")

        self.assertEqual(response.status_code, 200)
        sources = response.json()["MediaSources"]
        direct_stream = urlsplit(sources[0]["DirectStreamUrl"])
        self.assertEqual(direct_stream.path, "/emby/Videos/item-emby/stream")
        direct_query = parse_qs(direct_stream.query)
        self.assertEqual(direct_query["MediaSourceId"], ["cloud"])
        self.assertEqual(len(direct_query["_mfps"]), 1)
        self.assertGreaterEqual(len(direct_query["_mfps"][0]), 24)
        self.assertEqual(sources[1], payload["MediaSources"][1])
        self.assertEqual(_FakeAsyncClient.requests[0].url.path, "/emby/Items/item-emby/PlaybackInfo")

    def test_playback_info_and_rewritten_stream_share_one_recording_session(self):
        class Raw:
            token = "provider-secret"

        class GuangYa:
            logged_in = True
            raw = Raw()

            def get_download_url(self, _file_id, **_kwargs):
                return "https://signed.invalid/file?Expires=4102444800"

        payload = {
            "PlaySessionId": "emby-session-1",
            "MediaSources": [
                {"Id": "cloud", "Path": "/playgy/file-session/e/1/a.mkv"}
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        captured = []
        recorder = media_proxy.PlaybackRecordWriter(
            write_record=lambda record: captured.append(dict(record))
        )
        app = media_proxy.create_proxy_app(7, playback_record_writer=recorder)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy._client_is_authorized", new=AsyncMock(return_value=True)),
            patch("app.modules.media_proxy.GuangYaClient", GuangYa),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/emby/Items/item-session/PlaybackInfo?api_key=client-token"
            )
            direct_url = playback.json()["MediaSources"][0]["DirectStreamUrl"]
            stream = client.get(
                direct_url,
                headers={"X-Emby-Token": "client-token"},
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(stream.status_code, 302)
        self.assertEqual(len(captured), 2)
        self.assertEqual(
            {record["playback_session_key"] for record in captured},
            {captured[0]["playback_session_key"]},
        )
        self.assertTrue(captured[0]["playback_session_key"])
        self.assertTrue(all(record["media_item_id"] == "item-session" for record in captured))
        self.assertEqual(captured[-1]["media_source_id"], "cloud")
        self.assertEqual(captured[-1]["guangya_file_id"], "file-session")

    def test_manual_binding_has_priority_over_dynamic_mapping_on_emby_stream(self):
        media_proxy._dynamic_guangya_mappings.register(7, "item-2", "source-2", "dynamic-file")
        manual = {
            "source_type": "guangya",
            "guangya_file_id": "manual-file",
            "media_source_id": "source-2",
        }
        self._grant_file("client-token", "item-2", "source-2", "manual-file", manual)
        stack, client = self._client()
        with stack, patch(
            "app.modules.media_proxy.database.get_media_proxy_binding", return_value=manual
        ), client:
            response = client.get(
                "/emby/Videos/item-2/stream?MediaSourceId=source-2&api_key=client-token",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://signed.invalid/manual-file")
        self.assertEqual(_FakeGuangYaClient.calls, ["manual-file"])

    def test_dynamic_mapping_redirects_and_signed_url_is_short_cached(self):
        media_proxy._dynamic_guangya_mappings.register(7, "item-3", "source-3", "dynamic-file")
        self._grant_file("client-token", "item-3", "source-3", "dynamic-file")
        stack, client = self._client()
        with stack, patch(
            "app.modules.media_proxy.database.get_media_proxy_binding", return_value=None
        ), client:
            first = client.get(
                "/Videos/item-3/stream?MediaSourceId=source-3&api_key=client-token",
                follow_redirects=False,
            )
            second = client.get(
                "/Videos/item-3/stream?MediaSourceId=source-3&api_key=client-token",
                follow_redirects=False,
            )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(first.headers["location"], "https://signed.invalid/dynamic-file")
        self.assertEqual(_FakeGuangYaClient.calls, ["dynamic-file"])

    def test_proxy_port_handles_playgy_directly_and_does_not_cache_failures(self):
        _FakeGuangYaClient.results = {
            "direct-file": [None, "https://signed.invalid/direct-file-second"]
        }
        self._grant_file("client-token", "direct-item", "direct-source", "direct-file")
        stack, client = self._client()
        with stack, patch(
            "app.modules.media_proxy.database.get_media_proxy_binding", return_value=None
        ), client:
            failed = client.get(
                "/playgy/direct-file/etag/123/Movie.mkv?api_key=client-token",
                follow_redirects=False,
            )
            succeeded = client.get(
                "/playgy/direct-file/etag/123/Movie.mkv?api_key=client-token",
                follow_redirects=False,
            )

        self.assertEqual(failed.status_code, 502)
        self.assertEqual(succeeded.status_code, 302)
        self.assertEqual(
            succeeded.headers["location"],
            "https://signed.invalid/direct-file-second",
        )
        self.assertEqual(_FakeGuangYaClient.calls, ["direct-file", "direct-file"])

    def test_proxy_playgy_timeout_returns_504_and_is_not_cached(self):
        _FakeGuangYaClient.results = {
            "timeout-file": [TimeoutError("provider secret"), TimeoutError("provider secret")]
        }
        self._grant_file(
            "client-token", "timeout-item", "timeout-source", "timeout-file"
        )
        stack, client = self._client()
        with stack, client:
            first = client.get(
                "/playgy/timeout-file/etag/123/Movie.mkv?api_key=client-token",
                follow_redirects=False,
            )
            second = client.get(
                "/playgy/timeout-file/etag/123/Movie.mkv?api_key=client-token",
                follow_redirects=False,
            )

        self.assertEqual(first.status_code, 504)
        self.assertEqual(second.status_code, 504)
        self.assertEqual(first.json(), {"error": "光鸭播放地址获取超时"})
        self.assertEqual(_FakeGuangYaClient.calls, ["timeout-file", "timeout-file"])
        self.assertTrue(all(
            options == {
                "timeout": media_proxy.PLAYGY_SIGNED_URL_TIMEOUT_SECONDS,
                "raise_timeout": True,
            }
            for options in _FakeGuangYaClient.call_options
        ))

    def test_direct_playgy_uses_existing_client_token_authorization(self):
        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=False),
            ) as authorized,
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/playgy/private-file/e/1/a.mkv",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 401)
        authorized.assert_awaited_once()
        self.assertEqual(_FakeGuangYaClient.calls, [])

    def test_unbound_emby_stream_is_forwarded_unchanged_to_upstream(self):
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(status_code=206, body=b"local-video")
        ]
        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/emby/Videos/local-item/stream?MediaSourceId=local-source&api_key=client-token"
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"local-video")
        self.assertEqual(
            str(_FakeAsyncClient.requests[0].url),
            "http://127.0.0.1:8096/emby/Videos/local-item/stream?MediaSourceId=local-source",
        )
        self.assertEqual(_FakeAsyncClient.requests[0].headers["X-Emby-Token"], "client-token")

    def test_fallback_manual_binding_does_not_rewrite_a_different_local_source(self):
        payload = {
            "MediaSources": [
                {"Id": "cloud", "Path": "/playgy/cloud-file/e/1/cloud.mkv"},
                {"Id": "local", "Path": "/media/local.mkv", "Protocol": "File"},
            ]
        }
        local_before = copy.deepcopy(payload["MediaSources"][1])
        fallback_binding = {
            "source_type": "guangya",
            "guangya_file_id": "manual-cloud",
            "media_source_id": "cloud",
        }

        with patch(
            "app.modules.media_proxy.database.get_media_proxy_binding",
            return_value=fallback_binding,
        ):
            rewritten, changed = media_proxy.rewrite_playback_info(payload, 7, "mixed-item")

        self.assertTrue(changed)
        self.assertEqual(rewritten["MediaSources"][1], local_before)

    def test_fallback_manual_binding_for_other_source_does_not_hijack_stream(self):
        fallback_binding = {
            "source_type": "guangya",
            "guangya_file_id": "manual-cloud",
            "media_source_id": "cloud-source",
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(status_code=206, body=b"local-video")
        ]
        app = media_proxy.create_proxy_app(7)
        with (
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance()),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=fallback_binding,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Videos/mixed-item/stream?MediaSourceId=local-source&api_key=client-token",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"local-video")
        self.assertEqual(_FakeGuangYaClient.calls, [])

    def test_signed_url_cache_prunes_expired_entries_on_later_fetch(self):
        now = [100.0]
        cache = media_proxy.SignedUrlCache(ttl_seconds=10, clock=lambda: now[0])

        async def scenario():
            await cache.get_or_fetch("old-file", lambda: _async_value("https://signed.invalid/old"))
            now[0] = 111.0
            await cache.get_or_fetch("new-file", lambda: _async_value("https://signed.invalid/new"))

        async def _async_value(value: str) -> str:
            return value

        asyncio.run(scenario())
        self.assertEqual(cache.entry_count, 1)

    def test_signed_url_cache_uses_bounded_lock_stripes(self):
        cache = media_proxy.SignedUrlCache(ttl_seconds=10)

        async def scenario():
            for index in range(200):
                file_id = f"file-{index}"

                async def fetch(value=file_id):
                    return f"https://signed.invalid/{value}"

                await cache.get_or_fetch(file_id, fetch)

        asyncio.run(scenario())
        self.assertLessEqual(cache.lock_count, 64)

    def test_dynamic_mapping_prunes_expired_entries_during_registration(self):
        now = [100.0]
        mappings = media_proxy.DynamicGuangYaMappings(
            ttl_seconds=10,
            clock=lambda: now[0],
        )
        mappings.register(7, "old-item", "source", "old-file")
        now[0] = 111.0
        mappings.register(7, "new-item", "source", "new-file")
        self.assertEqual(mappings.entry_count, 1)
        self.assertEqual(mappings.get(7, "new-item", "source"), "new-file")

    def test_dynamic_mapping_expires(self):
        now = [100.0]
        mappings = media_proxy.DynamicGuangYaMappings(
            ttl_seconds=10,
            clock=lambda: now[0],
        )
        mappings.register(7, "item", "source", "file")
        self.assertEqual(mappings.get(7, "item", "source"), "file")
        now[0] = 111.0
        self.assertIsNone(mappings.get(7, "item", "source"))

    def test_signed_url_cache_expires_and_coalesces_concurrent_fetches(self):
        now = [100.0]
        cache = media_proxy.SignedUrlCache(ttl_seconds=10, clock=lambda: now[0])
        calls = []

        async def fetch() -> str:
            calls.append("fetch")
            await asyncio.sleep(0)
            return "https://signed.invalid/file"

        async def scenario():
            first, second = await asyncio.gather(
                cache.get_or_fetch("file", fetch),
                cache.get_or_fetch("file", fetch),
            )
            now[0] = 111.0
            third = await cache.get_or_fetch("file", fetch)
            return first, second, third

        first, second, third = asyncio.run(scenario())
        self.assertEqual(first, "https://signed.invalid/file")
        self.assertEqual(second, first)
        self.assertEqual(third, first)
        self.assertEqual(calls, ["fetch", "fetch"])


    def test_sync_signed_url_cache_singleflights_same_key(self):
        cache = media_proxy.SignedUrlCache(ttl_seconds=60)
        started = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()
        results: list[str | None] = []

        def fetch() -> str:
            nonlocal calls
            with calls_lock:
                calls += 1
            started.set()
            self.assertTrue(release.wait(2))
            return "https://signed.invalid/sync-file"

        def resolve() -> None:
            results.append(
                cache.get_or_fetch_sync_result("sync-file", fetch).url
            )

        first = threading.Thread(target=resolve)
        second = threading.Thread(target=resolve)
        first.start()
        self.assertTrue(started.wait(1))
        second.start()
        release.set()
        first.join(2)
        second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(calls, 1)
        self.assertEqual(results, [
            "https://signed.invalid/sync-file",
            "https://signed.invalid/sync-file",
        ])
        self.assertEqual(cache.metrics()["entries"], 1)

    def test_range_diagnostic_only_exposes_shape(self):
        self.assertEqual(media_proxy._range_diagnostic(""), "none")
        self.assertEqual(media_proxy._range_diagnostic("bytes=0-1023"), "single")
        self.assertEqual(media_proxy._range_diagnostic("bytes=-4096"), "single")
        self.assertEqual(media_proxy._range_diagnostic("bytes=0-1,4-5"), "invalid")

    def test_proxy_diagnostic_logs_safe_range_if_range_and_ua_flags(self):
        _FakeAsyncClient.responses = [_FakeUpstreamResponse(body=b"video")]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.database.record_media_proxy_playback_attempt"),
            patch.object(media_proxy.logger, "debug") as debug,
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Videos/diagnostic-item/stream.mkv?api_key=secret-query-token",
                headers={
                    "Range": "bytes=123-456",
                    "If-Range": "secret-if-range-value",
                    "User-Agent": "secret-player-agent",
                },
            )

        self.assertEqual(response.status_code, 200)
        message, *args = debug.call_args.args
        rendered = message % tuple(args)
        self.assertIn("route=stream", rendered)
        self.assertIn("action=upstream_stream", rendered)
        self.assertIn("range=single", rendered)
        self.assertIn("if_range=1", rendered)
        self.assertIn("ua=1", rendered)
        self.assertNotIn("123-456", rendered)
        self.assertNotIn("secret-if-range-value", rendered)
        self.assertNotIn("secret-player-agent", rendered)
        self.assertNotIn("secret-query-token", rendered)


if __name__ == "__main__":
    unittest.main()
