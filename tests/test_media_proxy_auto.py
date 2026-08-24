"""混合 Emby/Jellyfin 302 自动识别回归测试。"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import socket
import threading
import time
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.websockets import WebSocketDisconnect

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


class _FakeDecodingErrorResponse(_FakeUpstreamResponse):
    async def aiter_bytes(self):
        if False:  # pragma: no cover - 让本函数保持 async iterator 形态
            yield b""
        raise httpx.DecodingError("invalid compressed body")


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
    init_kwargs: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        self.__class__.init_kwargs.append(dict(kwargs))

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
        _FakeAuthAsyncClient.requests = []
        _FakeAuthAsyncClient.init_kwargs = []
        _FakeGuangYaClient.calls = []
        _FakeGuangYaClient.call_options = []
        _FakeGuangYaClient.results = {}
        media_proxy._dynamic_guangya_mappings.clear()
        media_proxy._item_level_binding_scopes.clear()
        if hasattr(media_proxy, "_playback_grants"):
            media_proxy._playback_grants.clear()
        if hasattr(media_proxy, "_playback_sessions"):
            media_proxy._playback_sessions.clear()

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
        self.assertEqual(str(request.url), "https://203.0.113.7:8920/System/Info")
        self.assertEqual(request.headers["Host"], "media.example:8920")
        self.assertEqual(
            request.headers["Authorization"],
            'MediaBrowser Token="server-token"',
        )
        self.assertNotIn("X-Emby-Token", request.headers)
        self.assertEqual(request.extensions["sni_hostname"], "media.example")
        self.assertFalse(_FakeAsyncClient.init_kwargs[0]["trust_env"])
        self.assertTrue(upstream.closed)

    def test_saved_emby_instance_probe_keeps_legacy_token_header(self):
        upstream = _FakeUpstreamResponse(
            body=b"{}", content_type="application/json"
        )
        _FakeAsyncClient.responses = [upstream]
        row = {
            "id": 7,
            "config_source": "custom",
            "server_type": "emby",
            "upstream_url": "http://127.0.0.1:8096/emby",
            "api_key": "emby-token",
            "enabled": 1,
        }
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=row,
            ),
            patch(
                "app.modules.media_proxy.httpx.AsyncClient",
                _FakeAsyncClient,
            ),
        ):
            result = asyncio.run(media_proxy.probe_media_proxy_instance(7))

        self.assertEqual(result["status_code"], 200)
        request = _FakeAsyncClient.requests[0]
        self.assertEqual(
            str(request.url),
            "http://127.0.0.1:8096/emby/System/Info",
        )
        self.assertEqual(request.headers["X-Emby-Token"], "emby-token")
        self.assertNotIn("Authorization", request.headers)

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

    def _client(self, *, base_url: str = "http://testserver"):
        app = media_proxy.create_proxy_app(7)
        stack = ExitStack()
        stack.enter_context(
            patch("app.modules.media_proxy.database.get_media_proxy_instance", return_value=self._instance())
        )
        stack.enter_context(patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient))
        stack.enter_context(
            patch("app.modules.media_proxy._client_is_authorized", new=AsyncMock(return_value=True))
        )
        return stack, TestClient(
            app,
            base_url=base_url,
            raise_server_exceptions=False,
        )

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

    @staticmethod
    def _signed_target(value: str) -> media_proxy._PinnedUpstreamTarget:
        logical = httpx.URL(value)
        connect = logical.copy_with(host="203.0.113.10")
        return media_proxy._PinnedUpstreamTarget(
            logical_url=str(logical),
            connect_url=str(connect),
            host_header=logical.netloc.decode("ascii"),
            sni_hostname=str(logical.host),
            addresses=("203.0.113.10",),
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
        ]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
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
        self.assertEqual(token_b.status_code, 401)
        self.assertEqual(_FakeGuangYaClient.calls, ["manual-acl-file"])
        self.assertEqual(len(_FakeAsyncClient.requests), 1)

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
        self.assertEqual(token_b.status_code, 401)
        self.assertEqual(_FakeGuangYaClient.calls, ["dynamic-acl-file"])
        self.assertEqual(len(_FakeAsyncClient.requests), 1)

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
        self.assertFalse(_FakeAsyncClient.init_kwargs[0]["trust_env"])

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
                "/System/Info?api_key=query-secret&X-Emby-Token=query-secret"
                "&X-MediaBrowser-Token=query-secret&foo=visible"
            )

        self.assertEqual(response.status_code, 200)
        request = _FakeAsyncClient.requests[0]
        self.assertEqual(str(request.url), "http://127.0.0.1:8096/System/Info?foo=visible")
        self.assertEqual(request.headers["X-Emby-Token"], "query-secret")
        serialized = str(request.url)
        self.assertNotIn("query-secret", serialized)

    def test_camel_case_api_key_is_recognized_and_removed_from_upstream_url(self):
        _FakeAsyncClient.responses = [_FakeUpstreamResponse(body=b"upstream")]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/System/Info?ApiKey=android-secret&foo=visible"
            )

        self.assertEqual(response.status_code, 200)
        request = _FakeAsyncClient.requests[0]
        self.assertEqual(
            str(request.url),
            "http://127.0.0.1:8096/System/Info?foo=visible",
        )
        self.assertEqual(request.headers["X-Emby-Token"], "android-secret")
        self.assertNotIn("android-secret", str(request.url))

    def test_playback_info_defaults_direct_play_and_stream_upstream(self):
        payload = {"MediaSources": []}
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
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
                return_value=None,
            ),
            patch(
                "app.modules.media_proxy.httpx.AsyncClient",
                _FakeAsyncClient,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Items/direct-item/PlaybackInfo"
                "?api_key=client-token&foo=visible"
            )

        self.assertEqual(response.status_code, 200)
        upstream = _FakeAsyncClient.requests[0]
        query = parse_qs(urlsplit(str(upstream.url)).query)
        self.assertEqual(query["EnableDirectPlay"], ["true"])
        self.assertEqual(query["EnableDirectStream"], ["true"])
        self.assertEqual(query["foo"], ["visible"])
        self.assertNotIn("api_key", query)

    def test_playback_info_preserves_explicit_direct_fallback_upstream(self):
        payload = {"MediaSources": []}
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
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
                return_value=None,
            ),
            patch(
                "app.modules.media_proxy.httpx.AsyncClient",
                _FakeAsyncClient,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Items/direct-item/PlaybackInfo"
                "?api_key=client-token&EnableDirectPlay=false"
                "&enabledirectstream=false&foo=visible"
            )

        self.assertEqual(response.status_code, 200)
        upstream = _FakeAsyncClient.requests[0]
        query = parse_qs(urlsplit(str(upstream.url)).query)
        self.assertEqual(query["EnableDirectPlay"], ["false"])
        self.assertEqual(query["enabledirectstream"], ["false"])
        self.assertEqual(query["foo"], ["visible"])
        self.assertNotIn("api_key", query)
        self.assertNotIn("EnableDirectStream", query)

    def test_conflicting_media_server_credentials_are_rejected_before_upstream(self):
        app = media_proxy.create_proxy_app(7)
        instance = {**self._instance(), "server_type": "jellyfin"}
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy.httpx.AsyncClient",
                _FakeAsyncClient,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/System/Info?api_key=query-token",
                headers={
                    "Authorization": 'MediaBrowser Token="header-token"',
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"error": "媒体服务器凭据参数冲突"},
        )
        self.assertEqual(_FakeAsyncClient.requests, [])

    def test_duplicate_conflicting_auth_headers_are_rejected(self):
        app = media_proxy.create_proxy_app(7)
        instance = {**self._instance(), "server_type": "jellyfin"}
        header_sets = (
            [
                ("X-Emby-Token", "first-token"),
                ("X-Emby-Token", "second-token"),
            ],
            [
                ("Authorization", 'MediaBrowser Token="first-token"'),
                ("Authorization", 'MediaBrowser Token="second-token"'),
            ],
            [
                (
                    "Authorization",
                    'MediaBrowser Token="first-token", Token="second-token"',
                ),
            ],
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy.httpx.AsyncClient",
                _FakeAsyncClient,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            for headers in header_sets:
                with self.subTest(headers=headers):
                    response = client.get("/System/Info", headers=headers)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(
                        response.json(),
                        {"error": "媒体服务器凭据参数冲突"},
                    )

        self.assertEqual(_FakeAsyncClient.requests, [])

    def test_conflicting_credentials_do_not_touch_playback_sessions(self):
        app = media_proxy.create_proxy_app(7)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/playgy/file-id/etag/1/video.mkv?api_key=query-token",
                headers={
                    "Authorization": 'MediaBrowser Token="header-token"',
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(media_proxy._playback_sessions._entries, {})
        self.assertEqual(media_proxy._playback_sessions._capability_index, {})

    def test_jellyfin_rebuilds_duplicate_physical_authorization_headers(self):
        _FakeAsyncClient.responses = [_FakeUpstreamResponse(body=b"upstream")]
        app = media_proxy.create_proxy_app(7)
        instance = {**self._instance(), "server_type": "jellyfin"}
        duplicate_headers = [
            (
                "Authorization",
                'MediaBrowser Token="same-token", Client="first"',
            ),
            (
                "Authorization",
                'MediaBrowser Token="same-token", Client="second"',
            ),
        ]
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy.httpx.AsyncClient",
                _FakeAsyncClient,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/System/Info", headers=duplicate_headers)

        self.assertEqual(response.status_code, 200)
        upstream = _FakeAsyncClient.requests[0]
        self.assertEqual(
            upstream.headers["Authorization"],
            'MediaBrowser Token="same-token"',
        )
        self.assertNotIn("Client=", upstream.headers["Authorization"])

    def test_jellyfin_rebuilds_repeated_identical_authorization_token(self):
        request = SimpleNamespace(
            headers=httpx.Headers({
                "Authorization": (
                    'MediaBrowser Token="same-token", Token="same-token", '
                    'Client="Jellyfin"'
                ),
            }),
            query_params=httpx.QueryParams(),
        )

        headers = media_proxy._upstream_request_headers(request, "jellyfin")

        self.assertEqual(
            headers["Authorization"],
            'MediaBrowser Token="same-token"',
        )
        self.assertEqual(headers["Authorization"].count("Token="), 1)

    def test_upstream_headers_strip_mediaflux_session_cookie_only(self):
        request = SimpleNamespace(
            headers=httpx.Headers({
                "Cookie": (
                    "session=mediaflux-admin; connect.sid=jellyfin-session; "
                    "theme=dark"
                ),
            }),
            query_params=httpx.QueryParams(),
            client=("203.0.113.8", 43123),
            url=SimpleNamespace(scheme="https"),
        )

        headers = media_proxy._upstream_request_headers(request, "jellyfin")

        self.assertNotIn("mediaflux-admin", headers.get("cookie", ""))
        self.assertEqual(
            headers["cookie"],
            "connect.sid=jellyfin-session; theme=dark",
        )

    def test_generic_response_headers_drop_upstream_set_cookie(self):
        headers = media_proxy._response_headers(httpx.Headers({
            "Content-Type": "application/json",
            "Set-Cookie": "session=upstream; Path=/",
        }))

        self.assertEqual(headers["content-type"], "application/json")
        self.assertNotIn("set-cookie", headers)

    def test_jellyfin_forwarded_headers_replace_untrusted_client_chain(self):
        request = SimpleNamespace(
            headers=httpx.Headers({
                "Authorization": 'MediaBrowser Token="same-token"',
                "Host": "media.example:18096",
                "Forwarded": "for=10.0.0.8;proto=http;host=evil.invalid",
                "X-Forwarded-For": "10.0.0.8, 198.51.100.9",
                "X-Forwarded-Host": "evil.invalid",
                "X-Forwarded-Proto": "http",
                "X-Real-IP": "10.0.0.8",
            }),
            query_params=httpx.QueryParams(),
            client=("::ffff:203.0.113.77", 43123),
            url=SimpleNamespace(scheme="https"),
        )

        headers = media_proxy._upstream_request_headers(request, "jellyfin")

        self.assertEqual(headers["X-Forwarded-For"], "203.0.113.77")
        self.assertEqual(headers["X-Real-IP"], "203.0.113.77")
        self.assertEqual(headers["X-Forwarded-Proto"], "https")
        self.assertNotIn("X-Forwarded-Host", headers)
        self.assertNotIn("Forwarded", headers)
        self.assertNotIn("10.0.0.8", str(headers))
        self.assertNotIn("evil.invalid", str(headers))

    def test_jellyfin_forwarded_headers_drop_spoofed_chain_without_peer_ip(self):
        request = SimpleNamespace(
            headers=httpx.Headers({
                "Authorization": 'MediaBrowser Token="same-token"',
                "X-Forwarded-For": "10.0.0.8",
                "X-Real-IP": "10.0.0.8",
            }),
            query_params=httpx.QueryParams(),
            client=("testclient", 43123),
            url=SimpleNamespace(scheme="http"),
        )

        headers = media_proxy._upstream_request_headers(request, "jellyfin")

        self.assertNotIn("X-Forwarded-For", headers)
        self.assertNotIn("X-Real-IP", headers)
        self.assertEqual(headers["X-Forwarded-Proto"], "http")

    def test_jellyfin_replaces_unrelated_authorization_with_canonical_token(self):
        _FakeAsyncClient.responses = [_FakeUpstreamResponse(body=b"upstream")]
        app = media_proxy.create_proxy_app(7)
        instance = {**self._instance(), "server_type": "jellyfin"}
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy.httpx.AsyncClient",
                _FakeAsyncClient,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/System/Info?api_key=valid-token",
                headers={
                    "Authorization": (
                        'Bearer unrelated, MediaBrowser Token="valid-token", '
                        'Client="Jellyfin"'
                    ),
                },
            )

        self.assertEqual(response.status_code, 200)
        upstream = _FakeAsyncClient.requests[0]
        self.assertEqual(
            upstream.headers["Authorization"],
            'MediaBrowser Token="valid-token"',
        )
        self.assertNotIn("Bearer unrelated", str(upstream.headers))
        self.assertNotIn("X-Emby-Token", upstream.headers)

    def test_duplicate_same_media_server_credential_is_canonicalized(self):
        _FakeAsyncClient.responses = [_FakeUpstreamResponse(body=b"upstream")]
        app = media_proxy.create_proxy_app(7)
        instance = {**self._instance(), "server_type": "jellyfin"}
        authorization = 'MediaBrowser Token="same-token", Client="Jellyfin"'
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy.httpx.AsyncClient",
                _FakeAsyncClient,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/System/Info?api_key=same-token",
                headers={
                    "Authorization": authorization,
                    "X-Emby-Token": "same-token",
                },
            )

        self.assertEqual(response.status_code, 200)
        upstream = _FakeAsyncClient.requests[0]
        self.assertEqual(upstream.headers["Authorization"], authorization)
        self.assertNotIn("X-Emby-Token", upstream.headers)
        self.assertNotIn("same-token", str(upstream.url))

    def test_jellyfin_sensitive_query_token_uses_media_browser_authorization(self):
        _FakeAsyncClient.responses = [_FakeUpstreamResponse(body=b"upstream")]
        instance = {**self._instance(), "server_type": "jellyfin"}
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/System/Info?api_key=jellyfin-token&device=visible"
            )

        self.assertEqual(response.status_code, 200)
        request = _FakeAsyncClient.requests[0]
        self.assertEqual(
            str(request.url),
            "http://127.0.0.1:8096/System/Info?device=visible",
        )
        self.assertEqual(
            request.headers["Authorization"],
            'MediaBrowser Token="jellyfin-token"',
        )
        self.assertNotIn("X-Emby-Token", request.headers)
        self.assertNotIn("jellyfin-token", str(request.url))

    def test_jellyfin_hls_keeps_current_user_token_in_upstream_query(self):
        manifest = b"#EXTM3U\nmain.m3u8?MediaSourceId=source&api_key=user-token\n"
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=manifest,
                content_type="application/vnd.apple.mpegurl",
            )
        ]
        instance = {
            **self._instance(),
            "server_type": "jellyfin",
            "api_key": "instance-admin-token",
        }
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Videos/hls-item/master.m3u8"
                "?MediaSourceId=source&api_key=user-token"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, manifest)
        upstream = _FakeAsyncClient.requests[0]
        query = parse_qs(urlsplit(str(upstream.url)).query)
        self.assertEqual(query["MediaSourceId"], ["source"])
        self.assertEqual(query["api_key"], ["user-token"])
        self.assertEqual(
            upstream.headers["Authorization"],
            'MediaBrowser Token="user-token"',
        )
        self.assertNotIn("instance-admin-token", str(upstream.url))
        self.assertNotIn("instance-admin-token", str(upstream.headers))

    def test_jellyfin_hls_header_only_token_stays_out_of_upstream_query(self):
        manifest = b"#EXTM3U\nmain.m3u8?MediaSourceId=source\n"
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=manifest,
                content_type="application/vnd.apple.mpegurl",
            )
        ]
        instance = {
            **self._instance(),
            "server_type": "jellyfin",
            "api_key": "instance-admin-token",
        }
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Videos/hls-item/master.m3u8?MediaSourceId=source",
                headers={
                    "Authorization": (
                        'MediaBrowser Client="Jellyfin Web", '
                        'Token="header-token"'
                    ),
                },
            )

        self.assertEqual(response.status_code, 200)
        upstream = _FakeAsyncClient.requests[0]
        query = parse_qs(urlsplit(str(upstream.url)).query)
        self.assertEqual(query["MediaSourceId"], ["source"])
        self.assertNotIn("api_key", query)
        self.assertEqual(
            upstream.headers["Authorization"],
            'MediaBrowser Client="Jellyfin Web", Token="header-token"',
        )
        self.assertNotIn("header-token", str(upstream.url))

    def test_jellyfin_hls1_segment_keeps_query_token(self):
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=b"segment",
                content_type="video/mp4",
            )
        ]
        instance = {
            **self._instance(),
            "server_type": "jellyfin",
            "api_key": "instance-admin-token",
        }
        app = media_proxy.create_proxy_app(7)
        path = "/Videos/hls-item/hls1/playlist/0.mp4"
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                f"{path}?MediaSourceId=source&api_key=user-token"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(media_proxy.classify_proxy_route(path), "upstream_hls")
        upstream = _FakeAsyncClient.requests[0]
        query = parse_qs(urlsplit(str(upstream.url)).query)
        self.assertEqual(query["MediaSourceId"], ["source"])
        self.assertEqual(query["api_key"], ["user-token"])
        self.assertEqual(
            upstream.headers["Authorization"],
            'MediaBrowser Token="user-token"',
        )
        self.assertNotIn("instance-admin-token", str(upstream.url))
        self.assertNotIn("instance-admin-token", str(upstream.headers))

    def test_emby_hls_does_not_readd_query_token(self):
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=b"#EXTM3U\n",
                content_type="application/vnd.apple.mpegurl",
            )
        ]
        instance = {
            **self._instance(),
            "server_type": "emby",
            "api_key": "instance-admin-token",
        }
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Videos/hls-item/master.m3u8"
                "?MediaSourceId=source&api_key=user-token"
            )

        self.assertEqual(response.status_code, 200)
        upstream = _FakeAsyncClient.requests[0]
        query = parse_qs(urlsplit(str(upstream.url)).query)
        self.assertEqual(query["MediaSourceId"], ["source"])
        self.assertNotIn("api_key", query)
        self.assertEqual(upstream.headers["X-Emby-Token"], "user-token")

    def test_jellyfin_hls_never_uses_instance_key_for_anonymous_child(self):
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                status_code=401,
                body=b"unauthorized",
                content_type="text/plain",
            )
        ]
        instance = {
            **self._instance(),
            "server_type": "jellyfin",
            "api_key": "instance-admin-token",
        }
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Videos/hls-item/hls1/playlist/0.mp4?MediaSourceId=source"
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            media_proxy.classify_proxy_route(
                "/Videos/hls-item/hls1/playlist/0.mp4"
            ),
            "upstream_hls",
        )
        upstream = _FakeAsyncClient.requests[0]
        query = parse_qs(urlsplit(str(upstream.url)).query)
        self.assertNotIn("api_key", query)
        self.assertNotIn("Authorization", upstream.headers)
        self.assertNotIn("instance-admin-token", str(upstream.url))
        self.assertNotIn("instance-admin-token", str(upstream.headers))

    def test_exoplayer_cross_protocol_signed_url_relays_https_unchanged(self):
        media_body = b"cross-protocol-media"
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                status_code=206,
                body=media_body,
                content_type="video/x-matroska",
                headers={
                    "accept-ranges": "bytes",
                    "content-length": str(len(media_body)),
                    "content-range": "bytes 0-19/100",
                },
            )
        ]
        _FakeGuangYaClient.results["cross-protocol-file"] = [
            "https://signed.invalid/cross-protocol-file"
        ]
        self._grant_file(
            "client-token",
            "cross-protocol-item",
            "cross-protocol-source",
            "cross-protocol-file",
        )
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/playgy/cross-protocol-file/e/1/video.mkv"
                "?api_key=client-token",
                headers={
                    "Range": "bytes=0-19",
                    "User-Agent": "ExoPlayerLib/1.8.0",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, media_body)
        self.assertNotIn("location", response.headers)
        relay_request = _FakeAsyncClient.requests[0]
        self.assertEqual(relay_request.url.scheme, "https")
        self.assertEqual(relay_request.url.host, "203.0.113.10")
        self.assertEqual(relay_request.headers["Host"], "signed.invalid")
        self.assertEqual(relay_request.headers["Range"], "bytes=0-19")
        self.assertFalse(_FakeAsyncClient.init_kwargs[0]["trust_env"])

    def test_working_third_party_clients_keep_cross_protocol_https_redirect(self):
        cases = (
            ("Yamby", "Yamby/2.0.5.5 ExoPlayerLib/1.8.0", "yamby-file"),
            ("Moonfin", "Moonfin/0.10 Media3/1.8.0", "moonfin-file"),
        )
        for _client_name, _user_agent, file_id in cases:
            _FakeGuangYaClient.results[file_id] = [
                f"https://signed.invalid/{file_id}"
            ]
            self._grant_file(
                "client-token",
                f"{file_id}-item",
                f"{file_id}-source",
                file_id,
            )

        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            for client_name, user_agent, file_id in cases:
                with self.subTest(client=client_name):
                    response = client.get(
                        f"/playgy/{file_id}/e/1/video.mkv"
                        "?api_key=client-token",
                        headers={
                            "X-Emby-Client": client_name,
                            "User-Agent": user_agent,
                        },
                        follow_redirects=False,
                    )
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(
                        response.headers["location"],
                        f"https://signed.invalid/{file_id}",
                    )

        self.assertEqual(_FakeAsyncClient.requests, [])

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
        self.assertEqual(source["Protocol"], "File")

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
        self.assertFalse(_FakeAuthAsyncClient.init_kwargs[0]["trust_env"])

    def test_jellyfin_client_authorization_uses_media_browser_header(self):
        _FakeAuthAsyncClient.requests = []
        request = SimpleNamespace(
            headers=httpx.Headers({}),
            query_params={"api_key": "jellyfin-user-token"},
        )
        instance = {**self._instance(), "server_type": "jellyfin"}
        with patch(
            "app.modules.media_proxy.httpx.AsyncClient",
            _FakeAuthAsyncClient,
        ):
            allowed = asyncio.run(
                media_proxy._client_is_authorized(instance, request)
            )

        self.assertTrue(allowed)
        upstream_request = _FakeAuthAsyncClient.requests[0]
        self.assertEqual(
            upstream_request.headers["Authorization"],
            'MediaBrowser Token="jellyfin-user-token"',
        )
        self.assertNotIn("X-Emby-Token", upstream_request.headers)

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

    def test_generic_same_origin_redirect_is_rewritten_to_proxy_origin(self):
        resolved = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 8096)),
        ]
        instance = {
            **self._instance(),
            "upstream_url": "http://media.internal.example:8096/emby",
        }
        upstream = _FakeUpstreamResponse(
            status_code=302,
            headers={
                "Location": "http://media.internal.example:8096/emby/web/index.html?x=1#dashboard",
                "Set-Cookie": "session=upstream; Path=/",
            },
        )
        _FakeAsyncClient.responses = [upstream]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy.socket.getaddrinfo",
                return_value=resolved,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/emby/redirect", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["location"],
            "/emby/web/index.html?x=1#dashboard",
        )
        self.assertNotIn("set-cookie", response.headers)
        self.assertTrue(upstream.closed)

    def test_generic_same_origin_redirect_rejects_encoded_path_traversal(self):
        instance = {
            **self._instance(),
            "upstream_url": "http://media.internal.example:8096/emby",
        }
        for location in (
            "http://media.internal.example:8096/emby/../System/Info",
            "http://media.internal.example:8096/emby/%2e%2e/System/Info",
            "http://media.internal.example:8096/emby/%252e%252e/System/Info",
            "http://media.internal.example:8096/emby/%2f%2fattacker.invalid/path",
        ):
            with self.subTest(location=location):
                upstream = _FakeUpstreamResponse(
                    status_code=302,
                    headers={"Location": location},
                )
                _FakeAsyncClient.responses = [upstream]
                app = media_proxy.create_proxy_app(7)
                with (
                    patch(
                        "app.modules.media_proxy.database.get_media_proxy_instance",
                        return_value=instance,
                    ),
                    patch(
                        "app.modules.media_proxy.socket.getaddrinfo",
                        return_value=[
                            (
                                socket.AF_INET,
                                socket.SOCK_STREAM,
                                6,
                                "",
                                ("192.168.1.20", 8096),
                            ),
                        ],
                    ),
                    patch(
                        "app.modules.media_proxy.httpx.AsyncClient",
                        _FakeAsyncClient,
                    ),
                    TestClient(app, raise_server_exceptions=False) as client,
                ):
                    response = client.get(
                        "/emby/redirect",
                        follow_redirects=False,
                    )

                self.assertEqual(response.status_code, 502)
                self.assertNotIn("location", response.headers)
                self.assertTrue(upstream.closed)

    def test_generic_same_origin_network_path_redirect_is_rejected(self):
        instance = {
            **self._instance(),
            "upstream_url": "http://media.internal.example:8096/emby",
        }
        upstream = _FakeUpstreamResponse(
            status_code=302,
            headers={
                "Location": (
                    "http://media.internal.example:8096//attacker.example/path"
                ),
            },
        )
        _FakeAsyncClient.responses = [upstream]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy.socket.getaddrinfo",
                return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 8096)),
                ],
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/emby/redirect", follow_redirects=False)

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("location", response.headers)
        self.assertTrue(upstream.closed)

    def test_generic_redirect_cannot_escape_configured_upstream_base_path(self):
        instance = {
            **self._instance(),
            "upstream_url": "http://media.internal.example:8096/emby",
        }
        upstream = _FakeUpstreamResponse(
            status_code=302,
            headers={
                "Location": "http://media.internal.example:8096/System/Info",
            },
        )
        _FakeAsyncClient.responses = [upstream]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy.socket.getaddrinfo",
                return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 8096)),
                ],
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/emby/redirect", follow_redirects=False)

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("location", response.headers)
        self.assertTrue(upstream.closed)

    def test_generic_cross_origin_redirect_is_rejected(self):
        instance = self._instance()
        upstream = _FakeUpstreamResponse(
            status_code=302,
            headers={"Location": "https://cdn.example.invalid/media?api_key=secret"},
        )
        _FakeAsyncClient.responses = [upstream]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/System/Redirect", follow_redirects=False)

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("location", response.headers)
        self.assertNotIn("secret", response.text)
        self.assertTrue(upstream.closed)

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

    def test_websocket_rejects_conflicting_credentials_before_upstream(self):
        app = media_proxy.create_proxy_app(7)
        with TestClient(app, raise_server_exceptions=False) as client:
            with self.assertRaises(WebSocketDisconnect) as closed:
                with client.websocket_connect(
                    "/socket?api_key=query-token",
                    headers={
                        "Authorization": (
                            'MediaBrowser Token="header-token"'
                        ),
                    },
                ):
                    pass

        self.assertEqual(closed.exception.code, 1008)

    def test_websocket_duplicate_credentials_are_rejected_before_resolution(self):
        class DuplicateCredentialWebSocket:
            def __init__(self, headers) -> None:
                self.headers = httpx.Headers(headers)
                self.query_params = httpx.QueryParams()
                self.url = SimpleNamespace(query="")
                self.scope = {"subprotocols": []}
                self.closed: dict[str, object] = {}

            async def close(self, **kwargs) -> None:
                self.closed = dict(kwargs)

        app = media_proxy.create_proxy_app(7)
        websocket_route = next(
            route
            for route in app.routes
            if getattr(route, "path", None) == "/{path:path}"
            and getattr(route, "methods", None) is None
        )
        header_sets = (
            [
                ("X-Emby-Token", "first-token"),
                ("X-Emby-Token", "second-token"),
            ],
            [
                (
                    "Authorization",
                    'MediaBrowser Token="first-token", Token="second-token"',
                ),
            ],
        )
        with patch("app.modules.media_proxy._pin_upstream_target") as pin:
            for headers in header_sets:
                with self.subTest(headers=headers):
                    websocket = DuplicateCredentialWebSocket(headers)
                    asyncio.run(websocket_route.endpoint(websocket, "socket"))
                    self.assertEqual(websocket.closed.get("code"), 1008)

        pin.assert_not_called()

    def test_websocket_duplicate_same_authorization_is_canonicalized(self):
        instance = {
            **self._instance(),
            "server_type": "jellyfin",
            "upstream_url": "http://media.example:8096",
        }
        pinned = media_proxy._PinnedUpstreamTarget(
            logical_url="http://media.example:8096/socket",
            connect_url="http://203.0.113.10:8096/socket",
            host_header="media.example:8096",
            sni_hostname="media.example",
            addresses=("203.0.113.10",),
        )
        captured: dict[str, object] = {}

        class RejectingWebSocketSession:
            def __init__(self, *, connector, trace_configs=None) -> None:
                self.closed = False

            async def ws_connect(self, target: str, **kwargs):
                captured["target"] = target
                captured["kwargs"] = kwargs
                raise media_proxy.WSServerHandshakeError(
                    None,
                    (),
                    status=403,
                    message="Invalid response status",
                )

            async def close(self) -> None:
                self.closed = True

        class DuplicateAuthorizationWebSocket:
            def __init__(self) -> None:
                self.headers = Headers(raw=[
                    (
                        b"authorization",
                        b'MediaBrowser Token="same-token", Client="first"',
                    ),
                    (
                        b"authorization",
                        b'MediaBrowser Token="same-token", Client="second"',
                    ),
                ])
                self.query_params = httpx.QueryParams()
                self.url = SimpleNamespace(query="")
                self.scope = {"subprotocols": []}
                self.closed: dict[str, object] = {}

            async def close(self, **kwargs) -> None:
                self.closed = dict(kwargs)

        app = media_proxy.create_proxy_app(7)
        websocket_route = next(
            route
            for route in app.routes
            if getattr(route, "path", None) == "/{path:path}"
            and getattr(route, "methods", None) is None
        )
        websocket = DuplicateAuthorizationWebSocket()
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy._pin_upstream_target",
                return_value=pinned,
            ),
            patch("app.modules.media_proxy.TCPConnector", return_value=object()),
            patch(
                "app.modules.media_proxy.ClientSession",
                RejectingWebSocketSession,
            ),
        ):
            asyncio.run(websocket_route.endpoint(websocket, "socket"))

        headers = httpx.Headers(captured["kwargs"]["headers"])
        self.assertEqual(
            headers["Authorization"],
            'MediaBrowser Token="same-token"',
        )
        self.assertNotIn("Client=", headers["Authorization"])
        self.assertEqual(websocket.closed.get("code"), 1011)

    def test_jellyfin_websocket_translates_token_and_reports_handshake_status(self):
        instance = {
            **self._instance(),
            "server_type": "jellyfin",
            "upstream_url": "http://media.example:8096",
        }
        pinned = media_proxy._PinnedUpstreamTarget(
            logical_url="http://media.example:8096/socket",
            connect_url="http://203.0.113.10:8096/socket",
            host_header="media.example:8096",
            sni_hostname="media.example",
            addresses=("203.0.113.10",),
        )
        captured: dict[str, object] = {}

        class RejectingWebSocketSession:
            def __init__(self, *, connector, trace_configs=None) -> None:
                captured["connector"] = connector
                captured["trace_configs"] = trace_configs
                self.closed = False
                captured["session"] = self

            async def ws_connect(self, target: str, **kwargs):
                captured["target"] = target
                captured["kwargs"] = kwargs
                raise media_proxy.WSServerHandshakeError(
                    None,
                    (),
                    status=403,
                    message="Invalid response status",
                )

            async def close(self) -> None:
                self.closed = True

        app = media_proxy.create_proxy_app(7)
        connector = object()
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy._pin_upstream_target",
                return_value=pinned,
            ),
            patch("app.modules.media_proxy.TCPConnector", return_value=connector),
            patch(
                "app.modules.media_proxy.ClientSession",
                RejectingWebSocketSession,
            ),
            patch("app.modules.media_proxy.log_throttled") as throttled,
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            with self.assertRaises(WebSocketDisconnect) as closed:
                with client.websocket_connect(
                    "/socket?api_key=socket-secret&device=visible",
                    headers={
                        "Origin": "http://mediaflux.test",
                        "Forwarded": "for=10.0.0.8;proto=https",
                        "X-Forwarded-For": "10.0.0.8",
                        "X-Forwarded-Host": "evil.invalid",
                        "X-Forwarded-Proto": "https",
                        "X-Real-IP": "10.0.0.8",
                        "Authorization": (
                            'Bearer unrelated, MediaBrowser Token="socket-secret", '
                            'Client="Jellyfin"'
                        ),
                    },
                    subprotocols=["jellyfin"],
                ):
                    pass

        self.assertEqual(closed.exception.code, 1011)
        self.assertEqual(
            captured["target"],
            "ws://media.example:8096/socket?device=visible",
        )
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        headers = httpx.Headers(kwargs["headers"])
        self.assertEqual(
            headers["Authorization"],
            'MediaBrowser Token="socket-secret"',
        )
        self.assertEqual(headers["Origin"], "http://mediaflux.test")
        self.assertNotIn("Forwarded", headers)
        self.assertNotIn("X-Forwarded-For", headers)
        self.assertNotIn("X-Forwarded-Host", headers)
        self.assertNotIn("X-Real-IP", headers)
        self.assertNotIn("10.0.0.8", str(headers))
        self.assertNotIn("X-Emby-Token", headers)
        trace_configs = captured["trace_configs"]
        self.assertEqual(len(trace_configs), 1)
        self.assertEqual(len(trace_configs[0].on_request_redirect), 1)
        self.assertFalse(
            any(key.lower().startswith("sec-websocket-") for key in headers)
        )
        self.assertEqual(kwargs["protocols"], ["jellyfin"])
        self.assertIs(captured["connector"], connector)
        self.assertTrue(captured["session"].closed)
        throttled.assert_called_once()
        self.assertEqual(
            throttled.call_args.args[2],
            "media-proxy-ws-handshake:7:403",
        )
        self.assertEqual(throttled.call_args.args[5], 403)
        self.assertEqual(len(throttled.call_args.args), 6)

    def test_anonymous_websocket_403_is_debug_only(self):
        instance = {
            **self._instance(),
            "server_type": "jellyfin",
            "upstream_url": "http://media.example:8096",
        }
        pinned = media_proxy._PinnedUpstreamTarget(
            logical_url="http://media.example:8096/socket",
            connect_url="http://203.0.113.10:8096/socket",
            host_header="media.example:8096",
            sni_hostname="media.example",
            addresses=("203.0.113.10",),
        )

        class RejectingWebSocketSession:
            def __init__(self, *, connector, trace_configs=None) -> None:
                self.closed = False

            async def ws_connect(self, target: str, **kwargs):
                raise media_proxy.WSServerHandshakeError(
                    None,
                    (),
                    status=403,
                    message="Invalid response status",
                )

            async def close(self) -> None:
                self.closed = True

        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy._pin_upstream_target",
                return_value=pinned,
            ),
            patch("app.modules.media_proxy.TCPConnector", return_value=object()),
            patch(
                "app.modules.media_proxy.ClientSession",
                RejectingWebSocketSession,
            ),
            patch("app.modules.media_proxy.log_throttled") as throttled,
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            with self.assertRaises(WebSocketDisconnect) as closed:
                with client.websocket_connect(
                    "/socket",
                    headers={"Origin": "http://mediaflux.test"},
                ):
                    pass

        self.assertEqual(closed.exception.code, 1011)
        throttled.assert_called_once()
        self.assertEqual(throttled.call_args.args[1], logging.DEBUG)
        self.assertEqual(
            throttled.call_args.args[2],
            "media-proxy-ws-anonymous:7:403",
        )
        self.assertEqual(throttled.call_args.args[5], 403)

    def test_websocket_cookie_counts_as_auth_signal(self):
        request = SimpleNamespace(
            headers=Headers({"Cookie": "connect.sid=session-cookie"}),
            query_params={},
        )

        self.assertTrue(media_proxy._request_has_websocket_auth_signal(request))

        request.headers = Headers({})
        self.assertFalse(media_proxy._request_has_websocket_auth_signal(request))

    def test_websocket_anonymous_rejection_log_matrix(self):
        anonymous = SimpleNamespace(headers=Headers({}), query_params={})
        self.assertTrue(
            media_proxy._websocket_handshake_is_anonymous_rejection(
                anonymous,
                401,
            )
        )
        self.assertTrue(
            media_proxy._websocket_handshake_is_anonymous_rejection(
                anonymous,
                403,
            )
        )
        self.assertFalse(
            media_proxy._websocket_handshake_is_anonymous_rejection(
                anonymous,
                500,
            )
        )

        authenticated_requests = (
            SimpleNamespace(
                headers=Headers({"Authorization": "Bearer expired-token"}),
                query_params={},
            ),
            SimpleNamespace(
                headers=Headers({"X-Emby-Authorization": "malformed"}),
                query_params={},
            ),
            SimpleNamespace(
                headers=Headers({"Cookie": "connect.sid=session-cookie"}),
                query_params={},
            ),
            SimpleNamespace(
                headers=Headers({}),
                query_params={"api_key": "expired-token"},
            ),
        )
        for request in authenticated_requests:
            with self.subTest(headers=dict(request.headers), query=request.query_params):
                self.assertFalse(
                    media_proxy._websocket_handshake_is_anonymous_rejection(
                        request,
                        403,
                    )
                )

    def test_websocket_upstream_redirect_is_rejected_before_second_hop(self):
        instance = {
            **self._instance(),
            "server_type": "jellyfin",
            "upstream_url": "http://media.example:8096",
        }
        pinned = media_proxy._PinnedUpstreamTarget(
            logical_url="http://media.example:8096/socket",
            connect_url="http://203.0.113.10:8096/socket",
            host_header="media.example:8096",
            sni_hostname="media.example",
            addresses=("203.0.113.10",),
        )
        captured: dict[str, object] = {}

        class RedirectingWebSocketSession:
            def __init__(self, *, connector, trace_configs=None) -> None:
                captured["trace_configs"] = trace_configs
                self.closed = False
                captured["session"] = self

            async def ws_connect(self, _target: str, **_kwargs):
                trace = captured["trace_configs"][0]
                callback = trace.on_request_redirect[0]
                await callback(None, None, None)
                raise AssertionError("redirect callback must abort the second hop")

            async def close(self) -> None:
                self.closed = True

        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy._pin_upstream_target",
                return_value=pinned,
            ),
            patch("app.modules.media_proxy.TCPConnector", return_value=object()),
            patch(
                "app.modules.media_proxy.ClientSession",
                RedirectingWebSocketSession,
            ),
            patch("app.modules.media_proxy.log_throttled") as throttled,
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            with self.assertRaises(WebSocketDisconnect) as closed:
                with client.websocket_connect("/socket"):
                    pass

        self.assertEqual(closed.exception.code, 1011)
        self.assertTrue(captured["session"].closed)
        self.assertEqual(
            throttled.call_args.args[2],
            "media-proxy-ws-redirect:7",
        )

    def test_websocket_accept_failure_closes_connected_upstream_resources(self):
        instance = {
            **self._instance(),
            "server_type": "jellyfin",
            "upstream_url": "http://media.example:8096",
        }
        pinned = media_proxy._PinnedUpstreamTarget(
            logical_url="http://media.example:8096/socket",
            connect_url="http://203.0.113.10:8096/socket",
            host_header="media.example:8096",
            sni_hostname="media.example",
            addresses=("203.0.113.10",),
        )

        class ConnectedUpstreamWebSocket:
            protocol = "jellyfin"

            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        upstream = ConnectedUpstreamWebSocket()
        captured_session: dict[str, object] = {}

        class ConnectedSession:
            def __init__(self, *, connector, trace_configs=None) -> None:
                self.connector = connector
                self.closed = False
                captured_session["value"] = self

            async def ws_connect(self, _target: str, **_kwargs):
                return upstream

            async def close(self) -> None:
                self.closed = True

        class DisconnectingWebSocket:
            def __init__(self) -> None:
                self.headers = httpx.Headers({
                    "Authorization": 'MediaBrowser Token="client-token"',
                })
                self.query_params = httpx.QueryParams()
                self.url = SimpleNamespace(query="")
                self.scope = {"subprotocols": ["jellyfin"]}
                self.closed = False

            async def accept(self, **_kwargs) -> None:
                raise RuntimeError("downstream disconnected")

            async def close(self, **_kwargs) -> None:
                self.closed = True

        downstream = DisconnectingWebSocket()
        app = media_proxy.create_proxy_app(7)
        websocket_route = next(
            route
            for route in app.routes
            if getattr(route, "path", None) == "/{path:path}"
            and getattr(route, "methods", None) is None
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=instance,
            ),
            patch(
                "app.modules.media_proxy._pin_upstream_target",
                return_value=pinned,
            ),
            patch("app.modules.media_proxy.TCPConnector", return_value=object()),
            patch(
                "app.modules.media_proxy.ClientSession",
                ConnectedSession,
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "downstream disconnected",
            ):
                asyncio.run(websocket_route.endpoint(downstream, "socket"))

        self.assertTrue(upstream.closed)
        self.assertTrue(captured_session["value"].closed)
        self.assertTrue(downstream.closed)

    def test_replace_header_removes_all_case_insensitive_duplicates(self):
        headers = {
            "accept-encoding": "br",
            "ACCEPT-ENCODING": "gzip",
            "X-Test": "kept",
        }

        media_proxy._replace_header(headers, "Accept-Encoding", "identity")

        encoding_items = [
            (key, value)
            for key, value in headers.items()
            if key.casefold() == "accept-encoding"
        ]
        self.assertEqual(encoding_items, [("Accept-Encoding", "identity")])
        self.assertEqual(headers["X-Test"], "kept")

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

    def test_playback_info_invalid_encoded_body_fails_closed(self):
        upstream = _FakeUpstreamResponse(
            body=b"\x8b\xc3\x05\x00compressed-json",
            content_type="application/json",
            headers={"content-encoding": "br"},
        )
        _FakeAsyncClient.responses = [upstream]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Items/invalid-encoding-item/PlaybackInfo",
                headers={"Accept-Encoding": "br, gzip, deflate"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"error": "上游播放信息无效"})
        self.assertEqual(
            _FakeAsyncClient.requests[0].headers["Accept-Encoding"],
            "identity",
        )
        self.assertTrue(upstream.closed)

    def test_playback_info_decoding_error_fails_closed(self):
        upstream = _FakeDecodingErrorResponse(
            body=b"broken-compressed-json",
            content_type="application/json",
            headers={"content-encoding": "br"},
        )
        _FakeAsyncClient.responses = [upstream]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Items/decoding-error-item/PlaybackInfo",
                headers={"Accept-Encoding": "br, gzip, deflate"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"error": "上游播放信息无效"})
        self.assertEqual(
            _FakeAsyncClient.requests[0].headers["Accept-Encoding"],
            "identity",
        )
        self.assertTrue(upstream.closed)

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

    def test_dynamic_mapping_and_grant_accept_equivalent_jellyfin_uuid_formats(self):
        hyphenated = "b3851937-35e9-8d81-287b-2b7ccc84fe42"
        compact = "b385193735e98d81287b2b7ccc84fe42"
        mappings = media_proxy.DynamicGuangYaMappings()
        grants = media_proxy.PlaybackGrantRegistry()

        mappings.register(7, hyphenated, hyphenated, "uuid-file")
        grants.register(
            "scope",
            7,
            hyphenated,
            hyphenated,
            source_type="dynamic",
            file_id="uuid-file",
        )

        self.assertEqual(mappings.get(7, compact, compact), "uuid-file")
        self.assertTrue(
            grants.matches(
                "scope", 7, compact, compact, file_id="uuid-file"
            )
        )

    def test_uuid_normalization_rejects_noncanonical_dash_layouts_and_isolates_scopes(self):
        canonical = "b3851937-35e9-8d81-287b-2b7ccc84fe42"
        compact = "b385193735e98d81287b2b7ccc84fe42"
        malformed = "b385-193735e98d81287b2b7ccc84fe42"
        mappings = media_proxy.DynamicGuangYaMappings()
        grants = media_proxy.PlaybackGrantRegistry()
        scopes = media_proxy.ItemLevelBindingScopes()

        mappings.register(7, malformed, malformed, "malformed-file")
        grants.register(
            "scope-a",
            7,
            canonical,
            canonical,
            source_type="dynamic",
            file_id="uuid-file",
        )
        scopes.register(7, canonical, canonical, "binding", "scope-a")

        self.assertEqual(
            media_proxy._normalized_media_identifier(malformed), malformed
        )
        self.assertIsNone(mappings.get(7, compact, compact))
        self.assertTrue(
            grants.matches(
                "scope-a", 7, compact, compact, file_id="uuid-file"
            )
        )
        self.assertFalse(
            grants.matches(
                "scope-b", 7, compact, compact, file_id="uuid-file"
            )
        )
        self.assertTrue(
            scopes.matches(7, compact, compact, "binding", "scope-a")
        )
        self.assertFalse(
            scopes.matches(7, compact, compact, "binding", "scope-b")
        )

    def test_manual_binding_lookup_accepts_equivalent_uuid_database_formats(self):
        canonical = "b3851937-35e9-8d81-287b-2b7ccc84fe42"
        compact = "b385193735e98d81287b2b7ccc84fe42"
        stored_canonical = canonical.upper()
        binding = {
            "id": 9,
            "source_type": "guangya",
            "guangya_file_id": "manual-uuid-file",
            "media_item_id": stored_canonical,
            "media_source_id": stored_canonical,
        }
        calls = []

        def lookup(instance_id, item_id, source_id=""):
            calls.append((instance_id, item_id, source_id))
            if item_id == stored_canonical and source_id == stored_canonical:
                return binding
            return None

        with patch(
            "app.modules.media_proxy.database.get_media_proxy_binding",
            side_effect=lookup,
        ):
            resolved = media_proxy._media_proxy_binding(7, compact, compact)

        self.assertIs(resolved, binding)
        self.assertIn((7, stored_canonical, stored_canonical), calls)

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

    def test_media_name_extraction_drops_authority_credentials_without_path(self):
        payload = {
            "MediaSources": [
                {
                    "Id": "cloud",
                    "Path": (
                        "https://user:password@media.invalid"
                        "?api_key=secret#private"
                    ),
                }
            ]
        }

        self.assertEqual(media_proxy._playback_media_name(payload), "")

    def test_media_name_extraction_sanitizes_opaque_and_encoded_urls(self):
        cases = (
            ({"Name": "https:Movie.mkv?api_key=secret"}, ""),
            ({"ItemName": "magnet:?xt=urn:btih:abc&token=secret"}, ""),
            ({"Title": "mailto:alice:password@example.invalid"}, ""),
            ({"Name": "custom:token=secret"}, ""),
            ({"Name": "web+foo:user:password@example.invalid"}, ""),
            ({"Name": "urn:secret:supersecret"}, ""),
            ({"Name": "Mission: Impossible"}, "Mission: Impossible"),
            (
                {
                    "Title": (
                        "https%3A%2F%2Fuser%3Apassword%40media.invalid%2F"
                        "private%2FEncoded%20Movie.mkv%3Fapi_key%3Dsecret%23frag"
                    )
                },
                "Encoded Movie.mkv",
            ),
        )

        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(media_proxy._playback_media_name(payload), expected)

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
                    "TranscodingContainer": "ts",
                    "TranscodingSubProtocol": "hls",
                    "TranscodingInfo": {"Protocol": "hls"},
                },
                {
                    "Id": "cloud-relative",
                    "Path": "/playgy/file-b/etag/200/Episode.mkv",
                    "SupportsDirectStream": False,
                    "SupportsTranscoding": True,
                    "TranscodingSubProtocol": "hls",
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
        for stale_field in (
            "TranscodingUrl",
            "TranscodingContainer",
            "TranscodingInfo",
        ):
            self.assertNotIn(stale_field, absolute)
        self.assertEqual(absolute["TranscodingSubProtocol"], "hls")
        self.assertEqual(
            relative["DirectStreamUrl"],
            "/Videos/item-1/stream?MediaSourceId=cloud-relative",
        )
        self.assertFalse(relative["SupportsTranscoding"])
        self.assertEqual(relative["TranscodingSubProtocol"], "hls")
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
        self.assertEqual(sources[0]["Protocol"], "File")
        self.assertEqual(len(direct_query["_mfps"]), 1)
        self.assertGreaterEqual(len(direct_query["_mfps"][0]), 24)
        self.assertEqual(len(direct_query["_mfss"]), 1)
        self.assertEqual(len(direct_query["_mfss"][0]), 48)
        self.assertEqual(sources[1], payload["MediaSources"][1])
        self.assertEqual(_FakeAsyncClient.requests[0].url.path, "/emby/Items/item-emby/PlaybackInfo")

    def test_jellyfin_web_preserves_transcoding_decision_and_relays_direct_stream(self):
        payload = {
            "MediaSources": [
                {
                    "Id": "cloud",
                    "Path": "/playgy/browser-file/e/1/Movie.mkv",
                    "SupportsDirectPlay": False,
                    "SupportsDirectStream": False,
                    "SupportsTranscoding": True,
                    "TranscodingUrl": "/Videos/browser-item/main.m3u8?api_key=client-token",
                    "TranscodingContainer": "ts",
                    "TranscodingSubProtocol": "hls",
                    "TranscodingInfo": {"Protocol": "hls"},
                }
            ]
        }
        media_body = b"test"
        media_response = _FakeUpstreamResponse(
            status_code=206,
            body=media_body,
            content_type="video/mp4",
            headers={
                "accept-ranges": "bytes",
                "content-length": str(len(media_body)),
                "content-range": "bytes 0-3/100",
                "etag": '"media-etag"',
                "set-cookie": "provider-secret=leak",
                "location": "https://untrusted.invalid/leak",
            },
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            media_response,
        ]
        app = media_proxy.create_proxy_app(7)
        authorization = (
            'MediaBrowser Client="Jellyfin Web", Device="Chrome", '
            'Token="client-token"'
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/browser-item/PlaybackInfo",
                headers={"X-Emby-Authorization": authorization},
            )
            source = playback.json()["MediaSources"][0]
            stream = client.get(
                source["DirectStreamUrl"],
                headers={
                    "Accept": "video/*",
                    "Cookie": "session=must-not-leak",
                    "If-Range": '"media-etag"',
                    "Origin": "http://testserver",
                    "Range": "bytes=0-3",
                    "Sec-Fetch-Dest": "video",
                    "Sec-Fetch-Mode": "cors",
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                    "X-Emby-Token": "client-token",
                },
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertFalse(source["SupportsDirectPlay"])
        self.assertFalse(source["SupportsDirectStream"])
        self.assertTrue(source["SupportsTranscoding"])
        self.assertEqual(
            source["TranscodingUrl"],
            "/Videos/browser-item/main.m3u8?api_key=client-token",
        )
        self.assertEqual(source["TranscodingContainer"], "ts")
        self.assertEqual(source["TranscodingInfo"], {"Protocol": "hls"})
        self.assertEqual(source["TranscodingSubProtocol"], "hls")
        self.assertEqual(stream.status_code, 206)
        self.assertEqual(stream.content, media_body)
        self.assertEqual(stream.headers["accept-ranges"], "bytes")
        self.assertEqual(stream.headers["content-range"], "bytes 0-3/100")
        self.assertEqual(stream.headers["content-length"], "4")
        self.assertEqual(stream.headers["content-type"], "video/mp4")
        self.assertNotIn("location", stream.headers)
        self.assertNotIn("set-cookie", stream.headers)
        self.assertTrue(media_response.closed)
        self.assertTrue(all(client.closed for client in _FakeAsyncClient.instances))

        relay_request = _FakeAsyncClient.requests[1]
        self.assertEqual(str(relay_request.url), "https://203.0.113.10/browser-file")
        self.assertEqual(relay_request.headers["Host"], "signed.invalid")
        self.assertEqual(relay_request.headers["Range"], "bytes=0-3")
        self.assertEqual(relay_request.headers["If-Range"], '"media-etag"')
        self.assertEqual(relay_request.headers["Accept-Encoding"], "identity")
        for secret_header in (
            "authorization",
            "cookie",
            "origin",
            "x-emby-token",
        ):
            self.assertNotIn(secret_header, relay_request.headers)

    def test_jellyfin_web_true_302_is_bound_to_upstream_direct_play_source(self):
        payload = {
            "PlaySessionId": "web-direct-session",
            "MediaSources": [
                {
                    "Id": "direct-source",
                    "Path": "/playgy/web-direct-file/e/1/direct.mp4",
                    "Protocol": "File",
                    "Container": "mp4",
                    "SupportsDirectPlay": True,
                    "SupportsDirectStream": True,
                    "SupportsTranscoding": True,
                    "TranscodingUrl": "/Videos/web-direct-item/master.m3u8",
                    "TranscodingContainer": "ts",
                    "TranscodingSubProtocol": "hls",
                },
                {
                    "Id": "transcode-source",
                    "Path": "/playgy/web-transcode-file/e/1/transcode.mkv",
                    "Protocol": "File",
                    "Container": "mkv",
                    "SupportsDirectPlay": False,
                    "SupportsDirectStream": False,
                    "SupportsTranscoding": True,
                    "TranscodingUrl": "/Videos/web-direct-item/master.m3u8",
                    "TranscodingContainer": "ts",
                    "TranscodingSubProtocol": "hls",
                },
            ],
        }
        relay_response = _FakeUpstreamResponse(
            status_code=206,
            body=b"relay",
            content_type="video/x-matroska",
            headers={
                "content-length": "5",
                "content-range": "bytes 0-4/100",
            },
        )
        manifest_response = _FakeUpstreamResponse(
            body=b"#EXTM3U\n",
            content_type="application/vnd.apple.mpegurl",
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            relay_response,
            manifest_response,
        ]
        app = media_proxy.create_proxy_app(7)
        authorization = (
            'MediaBrowser Client="Jellyfin Web", Device="Chrome", '
            'Token="client-token"'
        )
        browser_headers = {
            "Origin": "http://testserver",
            "Range": "bytes=0-4",
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "no-cors",
            "User-Agent": "Mozilla/5.0 Jellyfin Web",
        }
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ) as pin_signed_target,
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/web-direct-item/PlaybackInfo",
                headers={"X-Emby-Authorization": authorization},
            )
            direct_source, transcode_source = playback.json()["MediaSources"]
            direct = client.get(
                direct_source["Path"],
                headers=browser_headers,
                follow_redirects=False,
            )
            direct_audio = client.get(
                direct_source["Path"],
                headers={
                    **browser_headers,
                    "Sec-Fetch-Dest": "audio",
                },
                follow_redirects=False,
            )
            relayed = client.get(
                transcode_source["DirectStreamUrl"],
                headers=browser_headers,
                follow_redirects=False,
            )
            hls = client.get(
                direct_source["TranscodingUrl"],
                headers={"X-Emby-Authorization": authorization},
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertTrue(direct_source["SupportsDirectPlay"])
        self.assertTrue(direct_source["SupportsTranscoding"])
        self.assertTrue(direct_source["IsRemote"])
        self.assertEqual(direct_source["Protocol"], "Http")
        self.assertEqual(direct_source["RequiredHttpHeaders"], {})
        self.assertNotIn("TranscodingSubProtocol", direct_source)
        self.assertEqual(
            direct_source["TranscodingUrl"],
            "/Videos/web-direct-item/master.m3u8",
        )
        self.assertFalse(transcode_source["IsRemote"])
        self.assertEqual(transcode_source["Protocol"], "File")
        self.assertEqual(transcode_source["TranscodingSubProtocol"], "hls")
        self.assertEqual(direct.status_code, 302)
        self.assertEqual(
            direct.headers["location"],
            "https://signed.invalid/web-direct-file",
        )
        self.assertEqual(direct.headers["cache-control"], "private, no-store, no-cache, max-age=0")
        self.assertEqual(direct.headers["pragma"], "no-cache")
        self.assertEqual(direct.headers["referrer-policy"], "no-referrer")
        self.assertEqual(direct_audio.status_code, 302)
        self.assertEqual(
            direct_audio.headers["location"],
            "https://signed.invalid/web-direct-file",
        )
        self.assertFalse(transcode_source["SupportsDirectPlay"])
        self.assertTrue(transcode_source["SupportsTranscoding"])
        self.assertEqual(relayed.status_code, 206)
        self.assertEqual(relayed.content, b"relay")
        self.assertNotIn("location", relayed.headers)
        self.assertEqual(hls.status_code, 200)
        self.assertEqual(hls.content, b"#EXTM3U\n")
        self.assertNotIn("location", hls.headers)
        self.assertEqual(
            _FakeGuangYaClient.calls,
            ["web-direct-file", "web-transcode-file"],
        )
        self.assertEqual(len(_FakeAsyncClient.requests), 3)
        self.assertEqual(
            [item.args[0] for item in pin_signed_target.call_args_list],
            [
                "https://signed.invalid/web-direct-file",
                "https://signed.invalid/web-direct-file",
                "https://signed.invalid/web-transcode-file",
            ],
        )
        self.assertTrue(relay_response.closed)
        self.assertTrue(manifest_response.closed)
        direct_query = parse_qs(urlsplit(direct_source["Path"]).query)
        transcode_query = parse_qs(
            urlsplit(transcode_source["DirectStreamUrl"]).query
        )
        self.assertTrue(
            media_proxy._playback_sessions.capability_allows_browser_direct_redirect(
                7,
                direct_query["_mfps"][0],
                direct_query["_mfss"][0],
            )
        )
        self.assertFalse(
            media_proxy._playback_sessions.capability_allows_browser_direct_redirect(
                7,
                transcode_query["_mfps"][0],
                transcode_query["_mfss"][0],
            )
        )

    def test_jellyfin_web_direct_redirect_rejects_private_signed_target(self):
        payload = {
            "PlaySessionId": "web-private-session",
            "MediaSources": [
                {
                    "Id": "web-private-source",
                    "Path": "/playgy/web-private-file/e/1/direct.mp4",
                    "Protocol": "File",
                    "Container": "mp4",
                    "SupportsDirectPlay": True,
                    "SupportsDirectStream": True,
                    "SupportsTranscoding": False,
                }
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        _FakeGuangYaClient.results["web-private-file"] = [
            "http://127.0.0.1/private-video.mp4"
        ]
        app = media_proxy.create_proxy_app(7)
        authorization = (
            'MediaBrowser Client="Jellyfin Web", Device="Chrome", '
            'Token="client-token"'
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/web-private-item/PlaybackInfo",
                headers={"X-Emby-Authorization": authorization},
            )
            source = playback.json()["MediaSources"][0]
            stream = client.get(
                source["Path"],
                headers={
                    "Origin": "http://testserver",
                    "Sec-Fetch-Dest": "video",
                    "Sec-Fetch-Mode": "no-cors",
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                },
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(stream.status_code, 502)
        self.assertEqual(stream.json(), {"error": "媒体直链地址无效"})
        self.assertNotIn("location", stream.headers)
        self.assertEqual(_FakeGuangYaClient.calls, ["web-private-file"])

    def test_jellyfin_web_http_signed_target_always_relays(self):
        payload = {
            "PlaySessionId": "web-downgrade-session",
            "MediaSources": [
                {
                    "Id": "web-downgrade-source",
                    "Path": "/playgy/web-downgrade-file/e/1/direct.mp4",
                    "Protocol": "File",
                    "Container": "mp4",
                    "SupportsDirectPlay": True,
                    "SupportsDirectStream": True,
                    "SupportsTranscoding": False,
                }
            ],
        }
        relay_response = _FakeUpstreamResponse(
            status_code=206,
            body=b"secure-relay",
            content_type="video/mp4",
            headers={
                "content-length": "12",
                "content-range": "bytes 0-11/100",
            },
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            relay_response,
        ]
        _FakeGuangYaClient.results["web-downgrade-file"] = [
            "http://signed.invalid/web-downgrade-file"
        ]
        app = media_proxy.create_proxy_app(7)
        authorization = (
            'MediaBrowser Client="Jellyfin Web", Device="Chrome", '
            'Token="client-token"'
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/web-downgrade-item/PlaybackInfo",
                headers={"X-Emby-Authorization": authorization},
            )
            source = playback.json()["MediaSources"][0]
            stream = client.get(
                source["Path"],
                headers={
                    "Origin": "http://testserver",
                    "Range": "bytes=0-11",
                    "Sec-Fetch-Dest": "video",
                    "Sec-Fetch-Mode": "no-cors",
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                },
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(stream.status_code, 206)
        self.assertEqual(stream.content, b"secure-relay")
        self.assertNotIn("location", stream.headers)
        relay_request = _FakeAsyncClient.requests[1]
        self.assertEqual(
            str(relay_request.url),
            "http://203.0.113.10/web-downgrade-file",
        )
        self.assertEqual(relay_request.headers["Host"], "signed.invalid")
        self.assertEqual(relay_request.headers["Accept-Encoding"], "identity")
        self.assertTrue(relay_response.closed)

    def test_jellyfin_web_hls_xhr_relays_even_with_direct_play_capability(self):
        payload = {
            "PlaySessionId": "web-xhr-session",
            "MediaSources": [
                {
                    "Id": "web-xhr-source",
                    "Path": "/playgy/web-xhr-file/e/1/direct.mp4",
                    "Protocol": "File",
                    "Container": "mp4",
                    "SupportsDirectPlay": True,
                    "SupportsDirectStream": True,
                    "SupportsTranscoding": True,
                    "TranscodingUrl": "/Videos/web-xhr-item/master.m3u8",
                    "TranscodingContainer": "ts",
                    "TranscodingSubProtocol": "hls",
                }
            ],
        }
        media_response = _FakeUpstreamResponse(
            status_code=206,
            body=b"xhr-relay",
            content_type="video/mp4",
            headers={
                "accept-ranges": "bytes",
                "content-length": "9",
                "content-range": "bytes 0-8/100",
            },
        )
        cross_origin_media_response = _FakeUpstreamResponse(
            status_code=206,
            body=b"cors-video",
            content_type="video/mp4",
            headers={
                "accept-ranges": "bytes",
                "content-length": "10",
                "content-range": "bytes 9-18/100",
            },
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            media_response,
            cross_origin_media_response,
        ]
        app = media_proxy.create_proxy_app(7)
        authorization = (
            'MediaBrowser Client="Jellyfin Web", Device="Chrome", '
            'Token="client-token"'
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/web-xhr-item/PlaybackInfo",
                headers={"X-Emby-Authorization": authorization},
            )
            source = playback.json()["MediaSources"][0]
            stream = client.get(
                source["Path"],
                headers={
                    "Accept": "video/*",
                    "Accept-Encoding": "br, gzip, deflate",
                    "Cookie": "session=must-not-leak",
                    "Origin": "http://testserver",
                    "Range": "bytes=0-8",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "User-Agent": "Mozilla/5.0 Jellyfin Web hls.js",
                    "X-Emby-Token": "client-token",
                },
                follow_redirects=False,
            )
            cross_origin_video = client.get(
                source["Path"],
                headers={
                    "Accept": "video/*",
                    "Accept-Encoding": "br, gzip, deflate",
                    "Cookie": "session=must-not-leak",
                    "Origin": "http://testserver",
                    "Range": "bytes=9-18",
                    "Sec-Fetch-Dest": "video",
                    "Sec-Fetch-Mode": "cors",
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                    "X-Emby-Token": "client-token",
                },
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertTrue(source["SupportsDirectPlay"])
        self.assertTrue(source["IsRemote"])
        self.assertEqual(source["Protocol"], "Http")
        self.assertNotIn("TranscodingSubProtocol", source)
        self.assertEqual(stream.status_code, 206)
        self.assertEqual(stream.content, b"xhr-relay")
        self.assertNotIn("location", stream.headers)
        self.assertEqual(stream.headers["content-range"], "bytes 0-8/100")
        self.assertEqual(cross_origin_video.status_code, 206)
        self.assertEqual(cross_origin_video.content, b"cors-video")
        self.assertNotIn("location", cross_origin_video.headers)
        self.assertEqual(
            cross_origin_video.headers["content-range"],
            "bytes 9-18/100",
        )
        self.assertTrue(media_response.closed)
        self.assertTrue(cross_origin_media_response.closed)
        self.assertEqual(_FakeGuangYaClient.calls, ["web-xhr-file"])

        relay_request = _FakeAsyncClient.requests[1]
        self.assertEqual(str(relay_request.url), "https://203.0.113.10/web-xhr-file")
        self.assertEqual(relay_request.headers["Host"], "signed.invalid")
        self.assertEqual(relay_request.headers["Range"], "bytes=0-8")
        self.assertEqual(relay_request.headers["Accept-Encoding"], "identity")
        for secret_header in (
            "authorization",
            "cookie",
            "origin",
            "x-emby-token",
        ):
            self.assertNotIn(secret_header, relay_request.headers)

        cross_origin_relay_request = _FakeAsyncClient.requests[2]
        self.assertEqual(
            str(cross_origin_relay_request.url),
            "https://203.0.113.10/web-xhr-file",
        )
        self.assertEqual(cross_origin_relay_request.headers["Host"], "signed.invalid")
        self.assertEqual(
            cross_origin_relay_request.headers["Accept-Encoding"],
            "identity",
        )
        self.assertEqual(cross_origin_relay_request.headers["Range"], "bytes=9-18")
        for secret_header in (
            "authorization",
            "cookie",
            "origin",
            "x-emby-token",
        ):
            self.assertNotIn(secret_header, cross_origin_relay_request.headers)

    def test_web_direct_redirect_permissions_are_scoped_per_capability(self):
        now = [100.0]
        sessions = media_proxy.PlaybackSessionRegistry(
            ttl_seconds=3600,
            capability_ttl_seconds=10,
            capability_max_ttl_seconds=60,
            clock=lambda: now[0],
        )
        first_signature = "a" * 48
        second_signature = "b" * 48

        first = sessions.finalize_capability(
            "scope",
            7,
            "capability-a",
            item_id="item",
            upstream_session_token="shared-upstream-session",
            browser_relay=True,
            browser_direct_redirect_signatures=(first_signature,),
        )
        second = sessions.finalize_capability(
            "scope",
            7,
            "capability-b",
            item_id="item",
            upstream_session_token="shared-upstream-session",
            browser_relay=True,
            browser_direct_redirect_signatures=(second_signature,),
        )

        self.assertIs(first, second)
        self.assertTrue(
            sessions.capability_allows_browser_direct_redirect(
                7, "capability-a", first_signature
            )
        )
        self.assertTrue(
            sessions.capability_allows_browser_direct_redirect(
                7, "capability-b", second_signature
            )
        )
        self.assertFalse(
            sessions.capability_allows_browser_direct_redirect(
                7, "capability-a", second_signature
            )
        )
        self.assertFalse(
            sessions.capability_allows_browser_direct_redirect(
                7, "capability-b", first_signature
            )
        )
        now[0] = 111.0
        self.assertFalse(
            sessions.capability_allows_browser_direct_redirect(
                7, "capability-a", first_signature
            )
        )
        self.assertFalse(
            sessions.capability_allows_browser_direct_redirect(
                7, "capability-b", second_signature
            )
        )

    def test_web_direct_redirect_rejects_ambiguous_media_source_identity(self):
        for source_ids in (("", ""), ("duplicate", "duplicate")):
            with self.subTest(source_ids=source_ids):
                payload = {
                    "MediaSources": [
                        {
                            "Id": source_ids[0],
                            "Path": "/playgy/ambiguous-direct/e/1/direct.mp4",
                            "SupportsDirectPlay": True,
                        },
                        {
                            "Id": source_ids[1],
                            "Path": "/playgy/ambiguous-transcode/e/1/transcode.mkv",
                            "SupportsDirectPlay": False,
                            "SupportsTranscoding": True,
                        },
                    ]
                }
                allowed_signatures: set[str] = set()
                with patch(
                    "app.modules.media_proxy.database.get_media_proxy_binding",
                    return_value=None,
                ):
                    _rewritten, changed = media_proxy.rewrite_playback_info(
                        payload,
                        7,
                        "ambiguous-item",
                        playback_session_token="ambiguous-capability",
                        native_client_mode="web",
                        browser_direct_redirect_signatures=allowed_signatures,
                    )

                self.assertTrue(changed)
                self.assertEqual(allowed_signatures, set())

    def test_ambiguous_web_direct_source_relays_instead_of_returning_302(self):
        payload = {
            "MediaSources": [
                {
                    "Id": "",
                    "Path": "/playgy/ambiguous-direct/e/1/direct.mp4",
                    "SupportsDirectPlay": True,
                },
                {
                    "Id": "",
                    "Path": "/playgy/ambiguous-transcode/e/1/transcode.mkv",
                    "SupportsDirectPlay": False,
                    "SupportsTranscoding": True,
                },
            ]
        }
        relay_response = _FakeUpstreamResponse(
            status_code=206,
            body=b"ambiguous-relay",
            content_type="video/x-matroska",
            headers={
                "content-length": "15",
                "content-range": "bytes 0-14/100",
            },
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            relay_response,
        ]
        app = media_proxy.create_proxy_app(7)
        authorization = (
            'MediaBrowser Client="Jellyfin Web", Device="Chrome", '
            'Token="client-token"'
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/ambiguous-web-item/PlaybackInfo",
                headers={"X-Emby-Authorization": authorization},
            )
            direct_source = playback.json()["MediaSources"][0]
            stream = client.get(
                direct_source["Path"],
                headers={
                    "Origin": "http://testserver",
                    "Range": "bytes=0-14",
                    "Sec-Fetch-Dest": "video",
                    "Sec-Fetch-Mode": "no-cors",
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                },
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(stream.status_code, 206)
        self.assertEqual(stream.content, b"ambiguous-relay")
        self.assertNotIn("location", stream.headers)
        self.assertEqual(_FakeGuangYaClient.calls, ["ambiguous-transcode"])
        self.assertTrue(relay_response.closed)

    def test_legacy_item_binding_web_direct_path_keeps_true_302(self):
        payload = {
            "MediaSources": [
                {
                    "Id": "legacy-source",
                    "Path": "/mounted/legacy.mp4",
                    "Container": "mp4",
                    "SupportsDirectPlay": True,
                    "SupportsDirectStream": True,
                    "SupportsTranscoding": True,
                }
            ]
        }
        legacy_binding = {
            "id": 91,
            "source_type": "guangya",
            "guangya_file_id": "legacy-web-file",
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        app = media_proxy.create_proxy_app(7)
        authorization = (
            'MediaBrowser Client="Jellyfin Web", Device="Chrome", '
            'Token="client-token"'
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=legacy_binding,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/legacy-web-item/PlaybackInfo",
                headers={"X-Emby-Authorization": authorization},
            )
            source = playback.json()["MediaSources"][0]
            stream = client.get(
                source["Path"],
                headers={
                    "Origin": "http://testserver",
                    "Range": "bytes=0-1",
                    "Sec-Fetch-Dest": "video",
                    "Sec-Fetch-Mode": "no-cors",
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                },
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        path_query = parse_qs(urlsplit(source["Path"]).query)
        direct_query = parse_qs(urlsplit(source["DirectStreamUrl"]).query)
        self.assertNotIn("MediaSourceId", path_query)
        self.assertEqual(
            direct_query["MediaSourceId"], ["legacy-source"]
        )
        self.assertTrue(
            media_proxy._playback_sessions.capability_allows_browser_direct_redirect(
                7, path_query["_mfps"][0], path_query["_mfss"][0]
            )
        )
        self.assertEqual(stream.status_code, 302)
        self.assertEqual(
            stream.headers["location"],
            "https://signed.invalid/legacy-web-file",
        )

    def test_native_https_client_keeps_zero_bandwidth_302_redirect(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "native-item", "native-source", "native-file"
        )
        self._grant_file(
            "client-token", "native-item", "native-source", "native-file"
        )
        stack, client = self._client(base_url="https://testserver")
        with stack, patch(
            "app.modules.media_proxy.database.get_media_proxy_binding",
            return_value=None,
        ), client:
            response = client.get(
                "/Videos/native-item/stream"
                "?MediaSourceId=native-source&api_key=client-token",
                headers={
                    "User-Agent": "Jellyfin-Android/2.6.3",
                    "X-Emby-Client": "Jellyfin Android",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["location"],
            "https://signed.invalid/native-file",
        )

    def test_native_https_head_probe_then_get_keeps_zero_bandwidth_302(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "native-probe-item", "native-probe-source", "native-probe-file"
        )
        self._grant_file(
            "client-token",
            "native-probe-item",
            "native-probe-source",
            "native-probe-file",
        )
        upstream = _FakeUpstreamResponse(
            status_code=206,
            body=b"must-not-be-returned",
            content_type="video/mp4",
            headers={
                "accept-ranges": "bytes",
                "content-length": "1024",
                "content-range": "bytes 0-1023/10000",
            },
        )
        _FakeAsyncClient.responses = [upstream]
        app = media_proxy.create_proxy_app(7)
        headers = {
            "Accept": "application/json",
            "If-Range": '"native-etag"',
            "Range": "bytes=0-0",
            "User-Agent": "Jellyfin-Android/2.6.3",
            "X-Emby-Client": "Jellyfin Android",
        }
        target = (
            "/Videos/native-probe-item/stream"
            "?MediaSourceId=native-probe-source&api_key=client-token"
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(
                app,
                base_url="https://testserver",
                raise_server_exceptions=False,
            ) as client,
        ):
            probe = client.head(target, headers=headers, follow_redirects=False)
            stream = client.get(target, headers=headers, follow_redirects=False)

        self.assertEqual(probe.status_code, 206)
        self.assertEqual(probe.content, b"")
        self.assertEqual(probe.headers["content-range"], "bytes 0-0/10000")
        self.assertEqual(probe.headers["content-length"], "1")
        self.assertNotIn("location", probe.headers)
        self.assertTrue(upstream.closed)

        relay_request = _FakeAsyncClient.requests[0]
        self.assertEqual(relay_request.method, "GET")
        self.assertEqual(
            str(relay_request.url),
            "https://203.0.113.10/native-probe-file",
        )
        self.assertEqual(relay_request.headers["Host"], "signed.invalid")
        self.assertEqual(relay_request.headers["Range"], "bytes=0-1023")
        self.assertEqual(relay_request.headers["If-Range"], '"native-etag"')
        self.assertEqual(
            relay_request.headers["User-Agent"],
            "Jellyfin-Android/2.6.3",
        )
        self.assertEqual(relay_request.headers["Accept"], "*/*")
        self.assertEqual(relay_request.headers["Accept-Encoding"], "identity")

        self.assertEqual(stream.status_code, 302)
        self.assertEqual(
            stream.headers["location"],
            "https://signed.invalid/native-probe-file",
        )
        self.assertEqual(_FakeGuangYaClient.calls, ["native-probe-file"])

    def test_native_head_probe_preserves_range_conditions_and_avoids_cdn_406(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "probe-accept-item", "probe-accept-source", "probe-accept-file"
        )
        self._grant_file(
            "client-token",
            "probe-accept-item",
            "probe-accept-source",
            "probe-accept-file",
        )
        upstream = _FakeUpstreamResponse(
            status_code=206,
            content_type="video/mp4",
            headers={
                "accept-ranges": "bytes",
                "content-length": "1000",
                "content-range": "bytes 0-999/1000",
            },
        )
        _FakeAsyncClient.responses = [upstream]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.head(
                "/Videos/probe-accept-item/stream"
                "?MediaSourceId=probe-accept-source&api_key=client-token",
                headers={
                    "Accept": "video/*",
                    "If-Range": '"stale-etag"',
                    "Range": "bytes=100-199",
                    "User-Agent": "Jellyfin-Android/2.6.3",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["content-length"], "100")
        self.assertEqual(response.headers["content-range"], "bytes 100-199/1000")
        self.assertTrue(upstream.closed)
        self.assertEqual(len(_FakeAsyncClient.requests), 1)
        probe = _FakeAsyncClient.requests[0]
        self.assertEqual(probe.method, "GET")
        self.assertEqual(probe.headers["Accept"], "*/*")
        self.assertEqual(probe.headers["Range"], "bytes=0-1023")
        self.assertEqual(probe.headers["If-Range"], '"stale-etag"')
        self.assertEqual(_FakeAsyncClient.init_kwargs[0]["timeout"].read, 10.0)

    def test_native_head_if_range_miss_keeps_full_response_semantics(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "if-range-item", "if-range-source", "if-range-file"
        )
        self._grant_file(
            "client-token", "if-range-item", "if-range-source", "if-range-file"
        )
        upstream = _FakeUpstreamResponse(
            status_code=200,
            body=b"must-not-be-returned",
            content_type="video/mp4",
            headers={
                "accept-ranges": "bytes",
                "content-length": "10000",
                "etag": '"new-etag"',
            },
        )
        _FakeAsyncClient.responses = [upstream]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.head(
                "/Videos/if-range-item/stream"
                "?MediaSourceId=if-range-source&api_key=client-token",
                headers={
                    "If-Range": '"stale-etag"',
                    "Range": "bytes=100-199",
                    "User-Agent": "Jellyfin-Android/2.6.3",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["content-length"], "10000")
        self.assertNotIn("content-range", response.headers)
        self.assertTrue(upstream.closed)
        probe = _FakeAsyncClient.requests[0]
        self.assertEqual(probe.method, "GET")
        self.assertEqual(probe.headers["Range"], "bytes=0-1023")
        self.assertEqual(probe.headers["If-Range"], '"stale-etag"')

    def test_native_head_fallback_preserves_full_redirect_budget(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "redirect-budget-item", "redirect-budget-source", "redirect-budget-file"
        )
        self._grant_file(
            "client-token",
            "redirect-budget-item",
            "redirect-budget-source",
            "redirect-budget-file",
        )
        responses = []
        for index in range(media_proxy._SIGNED_MEDIA_MAX_REDIRECTS):
            responses.append(
                _FakeUpstreamResponse(
                    status_code=302,
                    headers={
                        "location": f"https://edge-{index}.invalid/next"
                    },
                )
            )
        responses.append(
            _FakeUpstreamResponse(
                status_code=206,
                headers={
                    "content-length": "1024",
                    "content-range": "bytes 0-1023/10000",
                },
            )
        )
        _FakeAsyncClient.responses = responses.copy()
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.head(
                "/Videos/redirect-budget-item/stream"
                "?MediaSourceId=redirect-budget-source&api_key=client-token",
                headers={
                    "If-Range": '"stale-etag"',
                    "Range": "bytes=200-299",
                    "User-Agent": "Jellyfin-Android/2.6.3",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"")
        self.assertEqual(len(_FakeAsyncClient.requests), len(responses))
        for request in _FakeAsyncClient.requests:
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["Range"], "bytes=0-1023")
            self.assertEqual(request.headers["If-Range"], '"stale-etag"')
        self.assertTrue(all(item.closed for item in responses))
        self.assertTrue(all(client.closed for client in _FakeAsyncClient.instances))

    def test_native_head_probe_injects_one_byte_range_without_reading_body(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "ignored-range-item", "ignored-range-source", "ignored-range-file"
        )
        self._grant_file(
            "client-token",
            "ignored-range-item",
            "ignored-range-source",
            "ignored-range-file",
        )

        class UnreadBodyResponse(_FakeUpstreamResponse):
            def __init__(self, **kwargs) -> None:
                super().__init__(**kwargs)
                self.raw_iterations = 0

            async def aiter_raw(self):
                self.raw_iterations += 1
                yield self._body

        one_byte_probe = UnreadBodyResponse(
            status_code=206,
            body=b"x" * 1024,
            content_type="video/mp4",
            headers={
                "accept-ranges": "bytes",
                "content-length": "1024",
                "content-range": "bytes 0-1023/99999999",
            },
        )
        _FakeAsyncClient.responses = [one_byte_probe]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.head(
                "/Videos/ignored-range-item/stream"
                "?MediaSourceId=ignored-range-source&api_key=client-token",
                headers={
                    "Accept": "application/json",
                    "If-Range": '"must-be-removed"',
                    "User-Agent": "Jellyfin-Android/2.6.3",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["content-length"], "99999999")
        self.assertNotIn("content-range", response.headers)
        self.assertEqual(one_byte_probe.raw_iterations, 0)
        self.assertTrue(one_byte_probe.closed)
        probe = _FakeAsyncClient.requests[0]
        self.assertEqual(probe.method, "GET")
        self.assertEqual(probe.headers["Accept"], "*/*")
        self.assertEqual(probe.headers["Range"], "bytes=0-1023")
        self.assertNotIn("if-range", probe.headers)

    def test_native_head_invalid_ranges_return_416_and_close_upstream(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "invalid-range-item", "invalid-range-source", "invalid-range-file"
        )
        self._grant_file(
            "client-token",
            "invalid-range-item",
            "invalid-range-source",
            "invalid-range-file",
        )
        invalid_ranges = (
            "bytes=bad-1",
            "bytes=0-1,4-5",
            "bytes=1000-",
        )
        upstream_responses = [
            _FakeUpstreamResponse(
                status_code=206,
                content_type="video/mp4",
                headers={
                    "accept-ranges": "bytes",
                    "content-length": "1000",
                    "content-range": "bytes 0-999/1000",
                },
            )
            for _ in invalid_ranges
        ]
        _FakeAsyncClient.responses = upstream_responses.copy()
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            responses = [
                client.head(
                    "/Videos/invalid-range-item/stream"
                    "?MediaSourceId=invalid-range-source&api_key=client-token",
                    headers={
                        "Range": value,
                        "User-Agent": "Jellyfin-Android/2.6.3",
                    },
                    follow_redirects=False,
                )
                for value in invalid_ranges
            ]

        for response in responses:
            self.assertEqual(response.status_code, 416)
            self.assertEqual(response.content, b"")
            self.assertEqual(response.headers["content-range"], "bytes */1000")
            self.assertEqual(response.headers.get("content-length"), "0")
        self.assertTrue(all(response.closed for response in upstream_responses))
        for request in _FakeAsyncClient.requests:
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["Range"], "bytes=0-1023")

    def test_native_head_probe_has_absolute_deadline_and_closes_client(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "slow-probe-item", "slow-probe-source", "slow-probe-file"
        )
        self._grant_file(
            "client-token",
            "slow-probe-item",
            "slow-probe-source",
            "slow-probe-file",
        )

        class SlowAsyncClient(_FakeAsyncClient):
            requests: list[httpx.Request] = []
            init_kwargs: list[dict] = []
            instances: list["SlowAsyncClient"] = []

            async def send(
                self, request: httpx.Request, stream: bool = False
            ) -> _FakeUpstreamResponse:
                self.__class__.requests.append(request)
                await asyncio.sleep(0.05)
                return _FakeUpstreamResponse(status_code=206)

        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", SlowAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            patch(
                "app.modules.media_proxy._SIGNED_MEDIA_PROBE_TOTAL_TIMEOUT_SECONDS",
                0.01,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.head(
                "/Videos/slow-probe-item/stream"
                "?MediaSourceId=slow-probe-source&api_key=client-token",
                headers={"User-Agent": "Jellyfin-Android/2.6.3"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.content, b"")
        self.assertEqual(len(SlowAsyncClient.requests), 1)
        self.assertTrue(all(client.closed for client in SlowAsyncClient.instances))

    def test_native_head_request_queue_shares_the_absolute_deadline(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "queued-head-item", "queued-head-source", "queued-head-file"
        )
        self._grant_file(
            "client-token",
            "queued-head-item",
            "queued-head-source",
            "queued-head-file",
        )
        real_async_client = httpx.AsyncClient
        first_relay_entered: asyncio.Event

        class QueuedProbeAsyncClient(_FakeAsyncClient):
            requests: list[httpx.Request] = []
            init_kwargs: list[dict] = []
            instances: list["QueuedProbeAsyncClient"] = []

            async def send(
                self, request: httpx.Request, stream: bool = False
            ) -> _FakeUpstreamResponse:
                self.__class__.requests.append(request)
                if len(self.__class__.requests) == 1:
                    first_relay_entered.set()
                await asyncio.sleep(0.05)
                return _FakeUpstreamResponse(
                    status_code=206,
                    headers={
                        "accept-ranges": "bytes",
                        "content-length": "100",
                        "content-range": "bytes 0-99/100",
                    },
                )

        with patch(
            "app.modules.media_proxy._SIGNED_MEDIA_PROBE_MAX_CONCURRENCY",
            1,
        ):
            app = media_proxy.create_proxy_app(7)

        target = (
            "/Videos/queued-head-item/stream"
            "?MediaSourceId=queued-head-source&api_key=client-token"
        )

        async def scenario() -> tuple[httpx.Response, httpx.Response, float]:
            nonlocal first_relay_entered
            first_relay_entered = asyncio.Event()
            transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=False,
            )
            async with app.router.lifespan_context(app):
                async with real_async_client(
                    transport=transport,
                    base_url="http://mediaflux.test",
                ) as client:
                    first_task = asyncio.create_task(
                        client.head(target, follow_redirects=False)
                    )
                    await asyncio.wait_for(first_relay_entered.wait(), timeout=1.0)
                    second_started = time.monotonic()
                    second_task = asyncio.create_task(
                        client.head(target, follow_redirects=False)
                    )
                    first, second = await asyncio.gather(
                        first_task,
                        second_task,
                    )
                    return first, second, time.monotonic() - second_started

        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch(
                "app.modules.media_proxy.httpx.AsyncClient",
                QueuedProbeAsyncClient,
            ),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            patch(
                "app.modules.media_proxy._SIGNED_MEDIA_PROBE_TOTAL_TIMEOUT_SECONDS",
                0.08,
            ),
            patch(
                "app.modules.media_proxy._SIGNED_MEDIA_PROBE_QUEUE_TIMEOUT_SECONDS",
                0.08,
            ),
        ):
            first, second, second_elapsed = asyncio.run(scenario())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 504)
        self.assertLess(second_elapsed, 0.11)
        self.assertEqual(len(QueuedProbeAsyncClient.requests), 2)

    def test_native_head_deadline_includes_client_authorization(self):
        entered = asyncio.Event()
        captured: dict[str, object] = {}

        async def slow_authorize(_instance, _request, **kwargs):
            captured.update(kwargs)
            entered.set()
            await asyncio.sleep(0.2)
            return True

        class UnexpectedGuangYaClient:
            def __init__(self) -> None:
                raise AssertionError("鉴权超时后不应初始化光鸭客户端")

        app = media_proxy.create_proxy_app(7)
        started = time.monotonic()
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=slow_authorize,
            ),
            patch(
                "app.modules.media_proxy.GuangYaClient",
                UnexpectedGuangYaClient,
            ),
            patch(
                "app.modules.media_proxy._SIGNED_MEDIA_PROBE_TOTAL_TIMEOUT_SECONDS",
                0.02,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.head(
                "/playgy/auth-timeout-file/e/1/a.mkv",
                follow_redirects=False,
            )
            elapsed = time.monotonic() - started

        self.assertTrue(entered.is_set())
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.content, b"")
        self.assertLess(elapsed, 0.15)
        self.assertTrue(captured["raise_timeout"])
        self.assertTrue(callable(captured["blocking_runner"]))

    def test_native_head_deadline_includes_signed_url_acquisition(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "slow-url-item", "slow-url-source", "slow-url-file"
        )
        self._grant_file(
            "client-token",
            "slow-url-item",
            "slow-url-source",
            "slow-url-file",
        )

        class SlowUrlClient(_FakeGuangYaClient):
            finished = threading.Event()

            def get_download_url(self, file_id: str, **kwargs) -> str | None:
                try:
                    time.sleep(0.2)
                    return super().get_download_url(file_id, **kwargs)
                finally:
                    self.__class__.finished.set()

        app = media_proxy.create_proxy_app(7)
        started = time.monotonic()
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", SlowUrlClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ) as pin_target,
            patch(
                "app.modules.media_proxy._SIGNED_MEDIA_PROBE_TOTAL_TIMEOUT_SECONDS",
                0.02,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.head(
                "/Videos/slow-url-item/stream"
                "?MediaSourceId=slow-url-source&api_key=client-token",
                headers={"User-Agent": "Jellyfin-Android/2.6.3"},
                follow_redirects=False,
            )
            elapsed = time.monotonic() - started
            self.assertTrue(SlowUrlClient.finished.wait(timeout=1.0))

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.content, b"")
        self.assertLess(elapsed, 0.15)
        pin_target.assert_not_called()
        self.assertEqual(_FakeAsyncClient.requests, [])

    def test_native_head_blocked_dns_keeps_worker_capacity_until_completion(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "blocked-dns-item", "blocked-dns-source", "blocked-dns-file"
        )
        self._grant_file(
            "client-token",
            "blocked-dns-item",
            "blocked-dns-source",
            "blocked-dns-file",
        )
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def blocking_pin(value: str) -> media_proxy._PinnedUpstreamTarget:
            nonlocal calls
            with calls_lock:
                calls += 1
                current_call = calls
            if current_call == 1:
                entered.set()
                try:
                    release.wait(timeout=1.0)
                finally:
                    finished.set()
            return self._signed_target(value)

        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                status_code=206,
                headers={
                    "accept-ranges": "bytes",
                    "content-length": "100",
                    "content-range": "bytes 0-99/100",
                },
            )
        ]
        target = (
            "/Videos/blocked-dns-item/stream"
            "?MediaSourceId=blocked-dns-source&api_key=client-token"
        )
        with patch(
            "app.modules.media_proxy._SIGNED_MEDIA_PROBE_MAX_CONCURRENCY",
            1,
        ):
            app = media_proxy.create_proxy_app(7)

        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=blocking_pin,
            ),
            patch(
                "app.modules.media_proxy._SIGNED_MEDIA_PROBE_TOTAL_TIMEOUT_SECONDS",
                0.02,
            ),
            patch(
                "app.modules.media_proxy._SIGNED_MEDIA_PROBE_QUEUE_TIMEOUT_SECONDS",
                0.005,
            ),
            patch(
                "app.modules.media_proxy._signed_media_probe_worker_capacity",
                threading.BoundedSemaphore(1),
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            try:
                first = client.head(target, follow_redirects=False)
                self.assertTrue(entered.wait(timeout=1.0))
                second = client.head(target, follow_redirects=False)
                with calls_lock:
                    calls_after_second = calls
            finally:
                release.set()
            self.assertTrue(finished.wait(timeout=1.0))
            time.sleep(0.05)
            third = client.head(target, follow_redirects=False)

        self.assertEqual(first.status_code, 504)
        self.assertEqual(second.status_code, 503)
        self.assertEqual(calls_after_second, 1)
        self.assertEqual(third.status_code, 200)
        self.assertEqual(third.content, b"")
        with calls_lock:
            self.assertEqual(calls, 2)

    def test_native_clients_share_signed_url_when_provider_is_not_ua_bound(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "ua-item", "ua-source", "ua-file"
        )
        self._grant_file("client-token", "ua-item", "ua-source", "ua-file")
        _FakeGuangYaClient.results["ua-file"] = [
            "https://signed.invalid/ua-shared",
        ]
        stack, client = self._client()
        target = (
            "/Videos/ua-item/stream"
            "?MediaSourceId=ua-source&api_key=client-token"
        )
        with stack, patch(
            "app.modules.media_proxy.database.get_media_proxy_binding",
            return_value=None,
        ), client:
            first = client.get(
                target,
                headers={"User-Agent": "Native-Player/A"},
                follow_redirects=False,
            )
            second = client.get(
                target,
                headers={"User-Agent": "Native-Player/B"},
                follow_redirects=False,
            )
            repeated = client.get(
                target,
                headers={"User-Agent": "Native-Player/B"},
                follow_redirects=False,
            )

        self.assertEqual(first.headers["location"], "https://signed.invalid/ua-shared")
        self.assertEqual(second.headers["location"], "https://signed.invalid/ua-shared")
        self.assertEqual(repeated.headers["location"], "https://signed.invalid/ua-shared")
        self.assertEqual(_FakeGuangYaClient.calls, ["ua-file"])

    def test_native_clients_isolate_cache_when_provider_declares_ua_binding(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "ua-bound-item", "ua-bound-source", "ua-bound-file"
        )
        self._grant_file(
            "client-token",
            "ua-bound-item",
            "ua-bound-source",
            "ua-bound-file",
        )
        calls: list[str] = []

        class BoundClient:
            logged_in = True

            def __init__(self) -> None:
                self.raw = SimpleNamespace(
                    token="provider-token",
                    download_url_user_agent_bound=True,
                )

            def get_download_url(self, file_id: str, **_kwargs) -> str:
                calls.append(file_id)
                return f"https://signed.invalid/bound-{len(calls)}"

        app = media_proxy.create_proxy_app(7)
        target = (
            "/Videos/ua-bound-item/stream"
            "?MediaSourceId=ua-bound-source&api_key=client-token"
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.GuangYaClient", BoundClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            first = client.get(
                target,
                headers={"User-Agent": "Native-Player/A"},
                follow_redirects=False,
            )
            second = client.get(
                target,
                headers={"User-Agent": "Native-Player/B"},
                follow_redirects=False,
            )
            repeated = client.get(
                target,
                headers={"User-Agent": "Native-Player/B"},
                follow_redirects=False,
            )

        self.assertEqual(first.headers["location"], "https://signed.invalid/bound-1")
        self.assertEqual(second.headers["location"], "https://signed.invalid/bound-2")
        self.assertEqual(repeated.headers["location"], "https://signed.invalid/bound-2")
        self.assertEqual(calls, ["ua-bound-file", "ua-bound-file"])

    def test_browser_head_relays_range_headers_without_body(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "head-item", "head-source", "head-file"
        )
        self._grant_file("client-token", "head-item", "head-source", "head-file")
        upstream = _FakeUpstreamResponse(
            status_code=206,
            body=b"must-not-be-returned",
            content_type="video/mp4",
            headers={
                "accept-ranges": "bytes",
                "content-length": "100",
                "content-range": "bytes 0-99/100",
            },
        )
        _FakeAsyncClient.responses = [upstream]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.head(
                "/Videos/head-item/stream"
                "?MediaSourceId=head-source&api_key=client-token",
                headers={
                    "Origin": "http://testserver",
                    "Range": "bytes=0-0",
                    "Sec-Fetch-Dest": "video",
                    "Sec-Fetch-Mode": "cors",
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["content-range"], "bytes 0-0/100")
        self.assertEqual(response.headers["content-length"], "1")
        self.assertTrue(upstream.closed)
        relay_request = _FakeAsyncClient.requests[0]
        self.assertEqual(relay_request.method, "GET")
        self.assertEqual(relay_request.headers["Accept"], "*/*")
        self.assertEqual(relay_request.headers["Range"], "bytes=0-1023")

    def test_browser_relay_follows_provider_redirect_internally(self):
        media_proxy._dynamic_guangya_mappings.register(
            7, "redirect-item", "redirect-source", "redirect-file"
        )
        self._grant_file(
            "client-token", "redirect-item", "redirect-source", "redirect-file"
        )
        first = _FakeUpstreamResponse(
            status_code=302,
            headers={"location": "https://edge.invalid/final.mp4"},
        )
        second = _FakeUpstreamResponse(
            status_code=206,
            body=b"edge",
            content_type="video/mp4",
            headers={
                "content-length": "4",
                "content-range": "bytes 0-3/4",
            },
        )
        _FakeAsyncClient.responses = [first, second]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Videos/redirect-item/stream"
                "?MediaSourceId=redirect-source&api_key=client-token",
                headers={
                    "Origin": "http://testserver",
                    "Range": "bytes=0-3",
                    "Sec-Fetch-Dest": "video",
                    "Sec-Fetch-Mode": "cors",
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"edge")
        self.assertNotIn("location", response.headers)
        self.assertEqual(len(_FakeAsyncClient.requests), 2)
        self.assertEqual(
            str(_FakeAsyncClient.requests[1].url),
            "https://203.0.113.10/final.mp4",
        )
        self.assertEqual(_FakeAsyncClient.requests[1].headers["Host"], "edge.invalid")
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_signed_media_target_rejects_local_and_credentialed_urls(self):
        for value in (
            "http://127.0.0.1/private",
            "http://10.0.0.8/private",
            "http://100.64.0.1/private",
            "http://169.254.169.254/latest/meta-data",
            "http://[fec0::1]/private",
            "http://[::ffff:100.64.0.1]/private",
            "https://user:password@example.com/file",
            "https://example.com/file#fragment",
            "javascript:alert(1)",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                media_proxy._pin_signed_media_target(value)

        with patch(
            "app.modules.media_proxy._resolve_upstream_addresses",
            return_value=("100.64.0.1",),
        ), self.assertRaises(ValueError):
            media_proxy._pin_signed_media_target("https://cdn.invalid/file")

    def test_post_playback_info_body_is_forwarded_and_response_is_rewritten(self):
        payload = {
            "MediaSources": [
                {"Id": "cloud", "Path": "/playgy/file-post/e/1/a.mkv"}
            ]
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        request_body = {"DeviceProfile": {"Name": "Integration Client"}}
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/Items/item-post/PlaybackInfo?api_key=client-token",
                json=request_body,
                headers={"Accept-Encoding": "br, gzip, deflate"},
            )

        self.assertEqual(response.status_code, 200)
        direct_url = response.json()["MediaSources"][0]["DirectStreamUrl"]
        self.assertEqual(urlsplit(direct_url).path, "/Videos/item-post/stream")
        self.assertEqual(_FakeAsyncClient.requests[0].method, "POST")
        self.assertEqual(
            _FakeAsyncClient.requests[0].headers["Accept-Encoding"],
            "identity",
        )
        self.assertEqual(
            json.loads(_FakeAsyncClient.requests[0].content), request_body
        )

    def test_web_quality_switch_reuses_rewritten_direct_play_path(self):
        payload = {
            "PlaySessionId": "web-quality-session",
            "MediaSources": [{
                "Id": "web-quality-source",
                "Path": "/playgy/web-quality-file/e/1/video.mp4",
                "Protocol": "File",
                "Container": "mp4",
                "SupportsDirectPlay": True,
                "SupportsDirectStream": True,
                "SupportsTranscoding": True,
                "TranscodingUrl": "/Videos/web-quality-item/master.m3u8",
                "TranscodingContainer": "ts",
                "TranscodingSubProtocol": "hls",
            }],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
        ]
        initial_request_body = {
            "MediaSourceId": "web-quality-source",
            "MaxStreamingBitrate": 20_000_000,
            "DeviceProfile": {
                "Name": "Jellyfin Web",
                "DirectPlayProfiles": [{
                    "Type": "Video",
                    "Container": "mp4",
                    "VideoCodec": "h264",
                    "AudioCodec": "aac",
                }],
            },
        }
        switched_request_body = copy.deepcopy(initial_request_body)
        switched_request_body["MaxStreamingBitrate"] = 4_000_000
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            initial_response = client.post(
                "/Items/web-quality-item/PlaybackInfo?api_key=client-token",
                json=initial_request_body,
                headers={
                    "X-Emby-Client": "Jellyfin Web",
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                },
            )
            switched_response = client.post(
                "/Items/web-quality-item/PlaybackInfo?api_key=client-token",
                json=switched_request_body,
                headers={
                    "X-Emby-Client": "Jellyfin Web",
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                },
            )

        self.assertEqual(initial_response.status_code, 200)
        self.assertEqual(switched_response.status_code, 200)
        for response in (initial_response, switched_response):
            source = response.json()["MediaSources"][0]
            self.assertTrue(source["enableDirectPlay"])
            self.assertTrue(source["SupportsDirectPlay"])
            self.assertTrue(source["SupportsTranscoding"])
            self.assertTrue(source["IsRemote"])
            self.assertEqual(source["Protocol"], "Http")
            self.assertEqual(source["RequiredHttpHeaders"], {})
            self.assertNotIn("TranscodingSubProtocol", source)
            self.assertEqual(
                source["TranscodingUrl"],
                "/Videos/web-quality-item/master.m3u8",
            )
            self.assertEqual(source["DirectStreamUrl"], source["Path"])
            stream_url = urlsplit(source["Path"])
            self.assertEqual(stream_url.path, "/Videos/web-quality-item/stream")
            stream_query = parse_qs(stream_url.query)
            self.assertEqual(
                stream_query["MediaSourceId"],
                ["web-quality-source"],
            )
            self.assertEqual(len(stream_query["_mfps"]), 1)
            self.assertEqual(len(stream_query["_mfss"]), 1)

        self.assertEqual(len(_FakeAsyncClient.requests), 2)
        for upstream_request, request_body in zip(
            _FakeAsyncClient.requests,
            (initial_request_body, switched_request_body),
            strict=True,
        ):
            self.assertEqual(upstream_request.method, "POST")
            upstream_query = parse_qs(upstream_request.url.query.decode())
            self.assertEqual(upstream_query["EnableDirectPlay"], ["true"])
            self.assertEqual(upstream_query["EnableDirectStream"], ["true"])
            self.assertEqual(json.loads(upstream_request.content), request_body)

    def test_web_playback_retries_default_audio_and_grants_true_302(self):
        first_payload = {
            "PlaySessionId": "web-audio-session",
            "MediaSources": [{
                "Id": "web-audio-source",
                "Path": "/playgy/web-audio-file/e/1/video.mkv",
                "Container": "mkv",
                "SupportsDirectPlay": False,
                "SupportsDirectStream": True,
                "SupportsTranscoding": True,
                "TranscodingUrl": "/Videos/web-audio/master.m3u8",
                "DefaultAudioStreamIndex": 1,
                "MediaStreams": [
                    {"Type": "Video", "Index": 0, "Codec": "h264"},
                    {
                        "Type": "Audio", "Index": 1, "Codec": "eac3",
                        "Language": "eng", "IsDefault": True, "Channels": 6,
                    },
                    {
                        "Type": "Audio", "Index": 2, "Codec": "aac",
                        "Language": "eng", "Channels": 2,
                    },
                ],
            }],
        }
        retry_payload = copy.deepcopy(first_payload)
        retry_payload["MediaSources"][0]["SupportsDirectPlay"] = True
        first_response = _FakeUpstreamResponse(
            body=json.dumps(first_payload).encode("utf-8"),
            content_type="application/json",
        )
        retry_response = _FakeUpstreamResponse(
            body=json.dumps(retry_payload).encode("utf-8"),
            content_type="application/json",
        )
        _FakeAsyncClient.responses = [first_response, retry_response]
        request_body = {
            "DeviceProfile": {
                "Name": "Jellyfin Web",
                "DirectPlayProfiles": [{
                    "Type": "Video",
                    "Container": "mkv",
                    "VideoCodec": "h264",
                    "AudioCodec": "aac",
                }],
            },
        }
        authorization = (
            'MediaBrowser Client="Jellyfin Web", Device="Browser", '
            'DeviceId="web-device", Version="10.10.7", Token="client-token"'
        )
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.post(
                "/Items/web-audio-item/PlaybackInfo",
                json=request_body,
                headers={
                    "X-Emby-Authorization": authorization,
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                },
            )
            source = playback.json()["MediaSources"][0]
            direct = client.get(
                source["Path"],
                headers={
                    "Origin": "http://testserver",
                    "Sec-Fetch-Dest": "video",
                    "Sec-Fetch-Mode": "no-cors",
                    "X-Emby-Authorization": authorization,
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                },
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertTrue(source["SupportsDirectPlay"])
        self.assertEqual(direct.status_code, 302)
        self.assertEqual(
            direct.headers["location"],
            "https://signed.invalid/web-audio-file",
        )
        self.assertEqual(len(_FakeAsyncClient.requests), 2)
        self.assertEqual(
            json.loads(_FakeAsyncClient.requests[0].content),
            request_body,
        )
        retried_request = json.loads(_FakeAsyncClient.requests[1].content)
        self.assertEqual(retried_request["AudioStreamIndex"], 2)
        self.assertEqual(retried_request["MediaSourceId"], "web-audio-source")
        self.assertEqual(
            retried_request["DeviceProfile"],
            request_body["DeviceProfile"],
        )
        self.assertTrue(first_response.closed)
        self.assertTrue(retry_response.closed)

    def test_web_explicit_default_audio_track_is_not_overridden(self):
        payload = {
            "MediaSources": [{
                "Id": "web-default-source",
                "Path": "/playgy/web-default/e/1/video.mkv",
                "Container": "mkv",
                "SupportsDirectPlay": False,
                "DefaultAudioStreamIndex": 1,
                "MediaStreams": [
                    {"Type": "Video", "Index": 0, "Codec": "h264"},
                    {
                        "Type": "Audio", "Index": 1, "Codec": "eac3",
                        "Language": "eng", "IsDefault": True,
                    },
                    {"Type": "Audio", "Index": 2, "Codec": "aac", "Language": "eng"},
                ],
            }],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        request_body = {
            "AudioStreamIndex": 1,
            "DeviceProfile": {
                "Name": "Jellyfin Web",
                "DirectPlayProfiles": [{
                    "Type": "Video",
                    "Container": "mkv",
                    "VideoCodec": "h264",
                    "AudioCodec": "aac",
                }],
            },
        }
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/Items/web-default-item/PlaybackInfo?api_key=client-token",
                json=request_body,
                headers={"User-Agent": "Mozilla/5.0 Jellyfin Web"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(_FakeAsyncClient.requests), 1)
        self.assertEqual(
            json.loads(_FakeAsyncClient.requests[0].content)["AudioStreamIndex"],
            1,
        )

    def test_web_audio_retry_preserves_explicit_non_default_track(self):
        request_payload = {
            "MediaSourceId": "cloud",
            "AudioStreamIndex": 3,
            "DeviceProfile": {
                "DirectPlayProfiles": [{
                    "Type": "Video",
                    "Container": "mkv",
                    "VideoCodec": "h264",
                    "AudioCodec": "aac",
                }],
            },
        }
        response_payload = {
            "MediaSources": [{
                "Id": "cloud",
                "Path": "/playgy/file/e/1/video.mkv",
                "Container": "mkv",
                "SupportsDirectPlay": False,
                "DefaultAudioStreamIndex": 1,
                "MediaStreams": [
                    {"Type": "Video", "Index": 0, "Codec": "h264"},
                    {
                        "Type": "Audio", "Index": 1, "Codec": "eac3",
                        "Language": "eng", "IsDefault": True,
                    },
                    {"Type": "Audio", "Index": 2, "Codec": "aac", "Language": "eng"},
                    {"Type": "Audio", "Index": 3, "Codec": "eac3", "Language": "eng"},
                ],
            }],
        }

        self.assertIsNone(
            media_proxy._web_direct_audio_retry_payload(
                request_payload, response_payload
            )
        )

    def test_web_audio_retry_without_direct_gain_keeps_first_hls_contract(self):
        first_payload = {
            "PlaySessionId": "web-hls-session",
            "MediaSources": [
                {
                    "Id": "web-hls-source",
                    "Path": "/playgy/web-hls-file/e/1/video.mkv",
                    "Container": "mkv",
                    "SupportsDirectPlay": False,
                    "SupportsDirectStream": True,
                    "SupportsTranscoding": True,
                    "TranscodingUrl": "/Videos/web-hls/first-master.m3u8",
                    "DefaultAudioStreamIndex": 1,
                    "MediaStreams": [
                        {"Type": "Video", "Index": 0, "Codec": "h264"},
                        {
                            "Type": "Audio", "Index": 1, "Codec": "eac3",
                            "Language": "eng", "IsDefault": True,
                        },
                        {
                            "Type": "Audio", "Index": 2, "Codec": "aac",
                            "Language": "eng",
                        },
                    ],
                },
                {
                    "Id": "other-source",
                    "Path": "/playgy/other-file/e/1/video.mp4",
                    "Container": "mp4",
                    "SupportsDirectPlay": False,
                },
            ],
        }
        second_payload = copy.deepcopy(first_payload)
        second_payload["MediaSources"][0]["TranscodingUrl"] = (
            "/Videos/web-hls/retry-master.m3u8"
        )
        # 全局 DirectPlay 数量虽然增加，但目标 source 仍不可直放；
        # 必须保留首次 HLS 合同，不能误采纳另一个媒体版本。
        second_payload["MediaSources"][1]["SupportsDirectPlay"] = True
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(first_payload).encode("utf-8"),
                content_type="application/json",
            ),
            _FakeUpstreamResponse(
                body=json.dumps(second_payload).encode("utf-8"),
                content_type="application/json",
            ),
        ]
        request_body = {
            "MediaSourceId": "web-hls-source",
            "DeviceProfile": {
                "Name": "Jellyfin Web",
                "DirectPlayProfiles": [{
                    "Type": "Video",
                    "Container": "mkv",
                    "VideoCodec": "h264",
                    "AudioCodec": "aac",
                }],
            },
        }
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/Items/web-hls-item/PlaybackInfo?api_key=client-token",
                json=request_body,
                headers={"User-Agent": "Mozilla/5.0 Jellyfin Web"},
            )

        self.assertEqual(response.status_code, 200)
        source = response.json()["MediaSources"][0]
        self.assertFalse(source["SupportsDirectPlay"])
        self.assertEqual(
            source["TranscodingUrl"],
            "/Videos/web-hls/first-master.m3u8",
        )
        self.assertEqual(len(_FakeAsyncClient.requests), 2)

    def test_web_query_audio_selection_is_never_overridden_by_retry(self):
        payload = {
            "MediaSources": [{
                "Id": "web-query-source",
                "Path": "/playgy/web-query/e/1/video.mkv",
                "Container": "mkv",
                "SupportsDirectPlay": False,
                "DefaultAudioStreamIndex": 1,
                "MediaStreams": [
                    {"Type": "Video", "Index": 0, "Codec": "h264"},
                    {
                        "Type": "Audio", "Index": 1, "Codec": "eac3",
                        "Language": "eng", "IsDefault": True,
                    },
                    {"Type": "Audio", "Index": 2, "Codec": "aac", "Language": "eng"},
                    {"Type": "Audio", "Index": 3, "Codec": "eac3", "Language": "eng"},
                ],
            }],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        request_body = {
            "MediaSourceId": "web-query-source",
            "DeviceProfile": {
                "Name": "Jellyfin Web",
                "DirectPlayProfiles": [{
                    "Type": "Video",
                    "Container": "mkv",
                    "VideoCodec": "h264",
                    "AudioCodec": "aac",
                }],
            },
        }
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/Items/web-query-item/PlaybackInfo"
                "?api_key=client-token&AudioStreamIndex=3",
                json=request_body,
                headers={"User-Agent": "Mozilla/5.0 Jellyfin Web"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(_FakeAsyncClient.requests), 1)
        query = parse_qs(urlsplit(str(_FakeAsyncClient.requests[0].url)).query)
        self.assertEqual(query["AudioStreamIndex"], ["3"])

    def test_web_empty_query_audio_key_is_not_auto_overridden(self):
        payload = {
            "MediaSources": [{
                "Id": "web-empty-query-source",
                "Path": "/playgy/web-empty-query/e/1/video.mkv",
                "Container": "mkv",
                "SupportsDirectPlay": False,
                "DefaultAudioStreamIndex": 1,
                "MediaStreams": [
                    {"Type": "Video", "Index": 0, "Codec": "h264"},
                    {
                        "Type": "Audio", "Index": 1, "Codec": "eac3",
                        "Language": "eng", "IsDefault": True,
                    },
                    {"Type": "Audio", "Index": 2, "Codec": "aac", "Language": "eng"},
                ],
            }],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        request_body = {
            "DeviceProfile": {
                "Name": "Jellyfin Web",
                "DirectPlayProfiles": [{
                    "Type": "Video",
                    "Container": "mkv",
                    "VideoCodec": "h264",
                    "AudioCodec": "aac",
                }],
            },
        }
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/Items/web-empty-query-item/PlaybackInfo"
                "?api_key=client-token&AudioStreamIndex=",
                json=request_body,
                headers={"User-Agent": "Mozilla/5.0 Jellyfin Web"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(_FakeAsyncClient.requests), 1)

    def test_web_explicit_direct_fallback_does_not_retry_audio_track(self):
        payload = {
            "MediaSources": [{
                "Id": "web-fallback-source",
                "Path": "/playgy/web-fallback/e/1/video.mkv",
                "Protocol": "File",
                "Container": "mkv",
                "SupportsDirectPlay": True,
                "SupportsDirectStream": True,
                "SupportsTranscoding": True,
                "TranscodingUrl": "/Videos/web-fallback-item/master.m3u8",
                "TranscodingContainer": "ts",
                "TranscodingSubProtocol": "hls",
                "DefaultAudioStreamIndex": 1,
                "MediaStreams": [
                    {"Type": "Video", "Index": 0, "Codec": "h264"},
                    {"Type": "Audio", "Index": 1, "Codec": "eac3", "Language": "eng"},
                    {"Type": "Audio", "Index": 2, "Codec": "aac", "Language": "eng"},
                ],
            }],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        request_body = {
            "MediaSourceId": "web-fallback-source",
            "AudioStreamIndex": 1,
            "EnableDirectPlay": False,
            "DeviceProfile": {
                "Name": "Jellyfin Web",
                "DirectPlayProfiles": [{
                    "Type": "Video",
                    "Container": "mkv",
                    "VideoCodec": "h264",
                    "AudioCodec": "aac",
                }],
            },
        }
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/Items/web-fallback-item/PlaybackInfo?api_key=client-token",
                json=request_body,
                headers={"User-Agent": "Mozilla/5.0 Jellyfin Web"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(_FakeAsyncClient.requests), 1)
        source = response.json()["MediaSources"][0]
        self.assertNotIn("enableDirectPlay", source)
        self.assertTrue(source["SupportsDirectPlay"])
        self.assertEqual(
            source["Path"],
            "/playgy/web-fallback/e/1/video.mkv",
        )
        self.assertNotIn("_mfps", source["Path"])
        self.assertNotIn("_mfss", source["Path"])
        self.assertEqual(source["Protocol"], "File")
        self.assertEqual(source["TranscodingSubProtocol"], "hls")
        self.assertEqual(
            source["TranscodingUrl"],
            "/Videos/web-fallback-item/master.m3u8",
        )

    def test_android_retry_two_preserves_upstream_direct_stream_contract(self):
        payload = {
            "PlaySessionId": "android-retry-two-session",
            "MediaSources": [
                {
                    "Id": "android-retry-two-source",
                    "Path": "/playgy/android-retry-two/e/1/video.mkv",
                    "Protocol": "File",
                    "Container": "mkv",
                    "SupportsDirectPlay": False,
                    "SupportsDirectStream": True,
                    "SupportsTranscoding": True,
                    "DirectStreamUrl": "/Videos/android-retry-two/stream.mkv",
                    "TranscodingUrl": "/Videos/android-retry-two/master.m3u8",
                    "TranscodingContainer": "ts",
                }
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        request_body = {
            "MediaSourceId": "android-retry-two-source",
            "EnableDirectPlay": False,
        }
        authorize = AsyncMock(return_value=True)
        app = media_proxy.create_proxy_app(7)
        headers = {
            "Authorization": (
                'MediaBrowser Client="Jellyfin for Android", '
                'Device="Pixel 10 Pro", DeviceId="android-retry-device", '
                'Version="2.7.0", Token="client-token"'
            ),
            "User-Agent": "Jellyfin-Android/2.7.0",
        }
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/Items/android-retry-two/PlaybackInfo",
                headers=headers,
                json=request_body,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        source = response.json()["MediaSources"][0]
        self.assertFalse(source["SupportsDirectPlay"])
        self.assertTrue(source["SupportsDirectStream"])
        self.assertTrue(source["SupportsTranscoding"])
        self.assertEqual(
            source["TranscodingUrl"],
            "/Videos/android-retry-two/master.m3u8",
        )
        upstream = _FakeAsyncClient.requests[0]
        self.assertEqual(json.loads(upstream.content), request_body)
        query = parse_qs(urlsplit(str(upstream.url)).query)
        self.assertNotIn("EnableDirectPlay", query)
        self.assertNotIn("EnableDirectStream", query)
        self.assertEqual(media_proxy._playback_sessions._capability_index, {})
        self.assertEqual(media_proxy._playback_grants._entries, {})
        authorize.assert_not_awaited()

    def test_android_retry_three_preserves_upstream_transcoding_contract(self):
        payload = {
            "PlaySessionId": "android-retry-three-session",
            "MediaSources": [
                {
                    "Id": "android-retry-three-source",
                    "Path": "/playgy/android-retry-three/e/1/video.mkv",
                    "Protocol": "File",
                    "Container": "mkv",
                    "SupportsDirectPlay": False,
                    "SupportsDirectStream": False,
                    "SupportsTranscoding": True,
                    "TranscodingUrl": "/Videos/android-retry-three/master.m3u8",
                    "TranscodingContainer": "ts",
                    "TranscodingSubProtocol": "hls",
                }
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        request_body = {
            "MediaSourceId": "android-retry-three-source",
            "EnableDirectPlay": False,
            "EnableDirectStream": False,
        }
        authorize = AsyncMock(return_value=True)
        app = media_proxy.create_proxy_app(7)
        headers = {
            "Authorization": (
                'MediaBrowser Client="Jellyfin for Android", '
                'Device="Pixel 10 Pro", DeviceId="android-retry-device", '
                'Version="2.7.0", Token="client-token"'
            ),
            "User-Agent": "Jellyfin-Android/2.7.0",
        }
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/Items/android-retry-three/PlaybackInfo",
                headers=headers,
                json=request_body,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        source = response.json()["MediaSources"][0]
        self.assertFalse(source["SupportsDirectPlay"])
        self.assertFalse(source["SupportsDirectStream"])
        self.assertTrue(source["SupportsTranscoding"])
        self.assertEqual(
            source["TranscodingUrl"],
            "/Videos/android-retry-three/master.m3u8",
        )
        self.assertEqual(source["TranscodingSubProtocol"], "hls")
        upstream = _FakeAsyncClient.requests[0]
        self.assertEqual(json.loads(upstream.content), request_body)
        query = parse_qs(urlsplit(str(upstream.url)).query)
        self.assertNotIn("EnableDirectPlay", query)
        self.assertNotIn("EnableDirectStream", query)
        self.assertEqual(media_proxy._playback_sessions._capability_index, {})
        self.assertEqual(media_proxy._playback_grants._entries, {})
        authorize.assert_not_awaited()

    def test_android_direct_stream_and_yamby_direct_play_after_playback_info(self):
        cases = (
            (
                "android-item",
                "android-file",
                "Jellyfin for Android",
                "Jellyfin-Android/2.7.0",
            ),
            (
                "yamby-item",
                "yamby-file",
                "Yamby",
                "Yamby/2.0.5.5",
            ),
            (
                "moonfin-item",
                "moonfin-file",
                "Moonfin for Android",
                "Moonfin/2.4.0 Media3/1.8.0",
            ),
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(
                    {
                        "PlaySessionId": f"session-{item_id}",
                        "MediaSources": [
                            {
                                "Id": "cloud",
                                "Path": f"/playgy/{file_id}/etag/1/video.mkv",
                                "Protocol": "Http",
                                "Container": "mkv",
                                "SupportsTranscoding": True,
                                "TranscodingUrl": f"/Videos/{item_id}/master.m3u8",
                            }
                        ],
                    }
                ).encode("utf-8"),
                content_type="application/json",
            )
            for item_id, file_id, _client_name, _user_agent in cases
        ]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            TestClient(
                app,
                base_url="https://testserver",
                raise_server_exceptions=False,
            ) as client,
        ):
            for item_id, file_id, client_name, user_agent in cases:
                with self.subTest(client=client_name):
                    headers = {
                        "X-Emby-Token": "client-token",
                        "X-Emby-Client": client_name,
                        "User-Agent": user_agent,
                    }
                    playback = client.get(
                        f"/Items/{item_id}/PlaybackInfo",
                        headers=headers,
                    )
                    self.assertEqual(playback.status_code, 200)
                    source = playback.json()["MediaSources"][0]
                    self.assertEqual(source["Protocol"], "File")
                    self.assertEqual(
                        source["SupportsDirectPlay"],
                        client_name != "Jellyfin for Android",
                    )
                    self.assertTrue(source["SupportsDirectStream"])
                    self.assertFalse(source["SupportsTranscoding"])
                    self.assertNotIn("TranscodingUrl", source)
                    # jellyfin-sdk-kotlin 1.7.1 将该字段定义为非空必填；
                    # 关闭转码时仍需返回合法占位，避免原生客户端反序列化失败。
                    self.assertEqual(source["TranscodingSubProtocol"], "http")
                    expected_suffix = ".mkv" if client_name == "Jellyfin for Android" else ""
                    self.assertEqual(
                        urlsplit(source["DirectStreamUrl"]).path,
                        f"/Videos/{item_id}/stream{expected_suffix}",
                    )

                    # Android 2.7 进入 File DirectStream 并请求带容器扩展名的
                    # stream URL；Yamby/Moonfin 保留已验证可用的 File DirectPlay。
                    stream_suffix = (
                        ".mkv" if client_name == "Jellyfin for Android" else ""
                    )
                    stream = client.get(
                        f"/Videos/{item_id}/stream{stream_suffix}"
                        "?static=true&MediaSourceId=cloud"
                        f"&PlaySessionId=session-{item_id}&DeviceId=device-1",
                        headers=headers,
                        follow_redirects=False,
                    )
                    self.assertEqual(stream.status_code, 302)
                    self.assertEqual(
                        stream.headers["location"],
                        f"https://signed.invalid/{file_id}",
                    )

        self.assertEqual(
            _FakeGuangYaClient.calls,
            ["android-file", "yamby-file", "moonfin-file"],
        )

    def test_android_standard_direct_stream_accepts_sdk_api_key_without_play_session(self):
        payload = {
            "PlaySessionId": "android-upstream-session",
            "MediaSources": [
                {
                    "Id": "android-sdk-item",
                    "Path": "/playgy/android-sdk-file/e/1/video.mkv",
                    "Protocol": "File",
                    "Container": "mkv",
                }
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        _FakeGuangYaClient.results["android-sdk-file"] = [
            "http://signed.invalid/android-sdk-file"
        ]
        authorize = AsyncMock(return_value=True)
        app = media_proxy.create_proxy_app(7)
        playback_headers = {
            "Authorization": (
                'MediaBrowser Client="Jellyfin for Android", '
                'Device="Pixel", DeviceId="android-device", '
                'Version="2.7.0", Token="client-token"'
            ),
            "User-Agent": "Jellyfin Android",
        }
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.post(
                "/Items/android-sdk-item/PlaybackInfo",
                headers=playback_headers,
                json={"IsPlayback": True},
            )
            stream = client.get(
                "/Videos/android-sdk-item/stream.mkv"
                "?ApiKey=client-token&DeviceId=android-device"
                "&MediaSourceId=android-sdk-item&static=true&Tag=media-tag",
                headers={"User-Agent": "Dalvik/2.1.0"},
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(stream.status_code, 302)
        self.assertEqual(
            stream.headers["location"],
            "http://signed.invalid/android-sdk-file",
        )
        self.assertEqual(_FakeGuangYaClient.calls, ["android-sdk-file"])
        self.assertEqual(len(_FakeAsyncClient.requests), 1)
        self.assertEqual(authorize.await_count, 2)

    def test_android_http_stream_recovers_tokenless_session_and_uses_same_protocol_cdn(self):
        payload = {
            "PlaySessionId": "android-tokenless-session",
            "MediaSources": [
                {
                    "Id": "android-source",
                    "Path": "/playgy/android-tokenless-file/etag/1/video.mkv",
                    "Protocol": "File",
                    "Container": "mkv",
                }
            ],
        }
        wrong_session_response = _FakeUpstreamResponse(
            body=b"wrong-session-fallback",
            content_type="video/x-matroska",
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            wrong_session_response,
        ]
        authorize = AsyncMock(return_value=True)
        _FakeGuangYaClient.results["android-tokenless-file"] = [
            "http://signed.invalid/android-tokenless-file"
        ] * 8
        app = media_proxy.create_proxy_app(7)
        playback_headers = {
            "Authorization": (
                'MediaBrowser Client="Jellyfin for Android", '
                'Device="Pixel 10 Pro", DeviceId="android-device", '
                'Version="2.7.0", Token="client-token"'
            ),
            "User-Agent": "Jellyfin Android",
        }
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.post(
                "/Items/android-tokenless-item/PlaybackInfo",
                headers=playback_headers,
                json={"IsPlayback": True},
            )
            source = playback.json()["MediaSources"][0]

            conflicting_session = client.get(
                "/Videos/android-tokenless-item/stream.mkv"
                "?static=true&mediaSourceId=android-source"
                "&playSessionId=android-tokenless-session"
                "&PlaySessionId=other-session&deviceId=android-device",
                headers={"Range": "bytes=0-12", "User-Agent": "Jellyfin Android"},
                follow_redirects=False,
            )
            wrong_session = client.get(
                "/Videos/android-tokenless-item/stream.mkv"
                "?static=true&mediaSourceId=android-source"
                "&playSessionId=wrong-session&deviceId=android-device",
                headers={"Range": "bytes=0-12", "User-Agent": "Jellyfin Android"},
                follow_redirects=False,
            )
            self.assertEqual(_FakeGuangYaClient.calls, [])
            wrong_device = client.get(
                "/Videos/android-tokenless-item/stream.mkv"
                "?static=true&mediaSourceId=android-source"
                "&playSessionId=android-tokenless-session&deviceId=other-device",
                headers={"Range": "bytes=0-12", "User-Agent": "Jellyfin Android"},
                follow_redirects=False,
            )

            stream = client.get(
                "/Videos/android-tokenless-item/stream.mkv"
                "?static=true&mediaSourceId=android-source"
                "&playSessionId=android-tokenless-session&deviceId=android-device",
                headers={"Range": "bytes=0-12", "User-Agent": "Jellyfin Android"},
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(source["Protocol"], "File")
        self.assertFalse(source["SupportsDirectPlay"])
        self.assertTrue(source["SupportsDirectStream"])
        self.assertEqual(
            urlsplit(source["DirectStreamUrl"]).path,
            "/Videos/android-tokenless-item/stream.mkv",
        )
        self.assertIn("_mfps=", source["DirectStreamUrl"])
        self.assertEqual(conflicting_session.status_code, 400)
        self.assertEqual(wrong_session.status_code, 200)
        self.assertEqual(wrong_session.content, b"wrong-session-fallback")
        self.assertEqual(wrong_device.status_code, 302)
        self.assertEqual(
            wrong_device.headers["location"],
            "http://signed.invalid/android-tokenless-file",
        )
        self.assertEqual(stream.status_code, 302)
        self.assertEqual(
            stream.headers["location"],
            "http://signed.invalid/android-tokenless-file",
        )
        self.assertEqual(_FakeGuangYaClient.calls, ["android-tokenless-file"])
        self.assertEqual(len(_FakeAsyncClient.requests), 2)
        self.assertTrue(wrong_session_response.closed)
        authorize.assert_awaited_once()

    def test_web_rewrite_preserves_upstream_transcoding_contract_verbatim(self):
        source = {
            "Id": "web-source",
            "Path": "/playgy/web-file/e/1/video.mkv",
            "SupportsDirectPlay": False,
            "SupportsDirectStream": False,
            "SupportsTranscoding": True,
            "TranscodingUrl": "/Videos/web-item/master.m3u8",
            "TranscodingContainer": "ts",
        }
        payload = {"MediaSources": [source]}

        with patch(
            "app.modules.media_proxy.database.get_media_proxy_binding",
            return_value=None,
        ):
            rewritten, changed = media_proxy.rewrite_playback_info(
                payload, 7, "web-item", native_client_mode="web"
            )

        self.assertTrue(changed)
        rewritten_source = rewritten["MediaSources"][0]
        self.assertFalse(rewritten_source["SupportsDirectPlay"])
        self.assertFalse(rewritten_source["SupportsDirectStream"])
        self.assertTrue(rewritten_source["SupportsTranscoding"])
        self.assertEqual(
            rewritten_source["TranscodingUrl"],
            "/Videos/web-item/master.m3u8",
        )
        self.assertEqual(rewritten_source["TranscodingContainer"], "ts")
        self.assertNotIn("TranscodingSubProtocol", rewritten_source)

    def test_android_embedded_webview_uses_web_playback_contract(self):
        payload = {
            "PlaySessionId": "android-web-session",
            "MediaSources": [
                {
                    "Id": "android-web-source",
                    "Path": "/playgy/android-web-file/e/1/video.mkv",
                    "Protocol": "File",
                    "Container": "mkv",
                    "SupportsDirectPlay": False,
                    "SupportsDirectStream": False,
                    "SupportsTranscoding": True,
                    "TranscodingUrl": "/Videos/android-web-item/master.m3u8",
                    "TranscodingContainer": "ts",
                }
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        authorize = AsyncMock(return_value=True)
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.post(
                "/Items/android-web-item/PlaybackInfo",
                headers={
                    "X-Emby-Token": "client-token",
                    "X-Emby-Client": "Jellyfin for Android",
                    "User-Agent": (
                        "Mozilla/5.0 (Linux; Android 16; device; wv) "
                        "AppleWebKit/537.36 Chrome/151 Mobile Safari/537.36"
                    ),
                },
                json={
                    "IsPlayback": True,
                    "DeviceProfile": {
                        "Name": "",
                        "DirectPlayProfiles": [{}, {}],
                        "TranscodingProfiles": [{}, {}],
                    },
                },
            )

        self.assertEqual(playback.status_code, 200)
        source = playback.json()["MediaSources"][0]
        self.assertFalse(source["SupportsDirectPlay"])
        self.assertFalse(source["SupportsDirectStream"])
        self.assertTrue(source["SupportsTranscoding"])
        self.assertEqual(
            source["TranscodingUrl"],
            "/Videos/android-web-item/master.m3u8",
        )
        self.assertEqual(source["TranscodingContainer"], "ts")
        self.assertTrue(
            any(
                entry.browser_relay
                for entry in media_proxy._playback_sessions._entries.values()
            )
        )
        authorize.assert_not_awaited()

    def test_android_webview_direct_play_source_keeps_true_302(self):
        payload = {
            "PlaySessionId": "android-web-direct-session",
            "MediaSources": [
                {
                    "Id": "android-web-direct-source",
                    "Path": "/playgy/android-web-direct-file/e/1/video.mp4",
                    "Protocol": "File",
                    "Container": "mp4",
                    "SupportsDirectPlay": True,
                    "SupportsDirectStream": True,
                    "SupportsTranscoding": True,
                    "TranscodingUrl": "/Videos/android-web-direct-item/master.m3u8",
                    "TranscodingContainer": "ts",
                    "TranscodingSubProtocol": "hls",
                }
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        authorize = AsyncMock(return_value=True)
        app = media_proxy.create_proxy_app(7)
        headers = {
            "X-Emby-Token": "client-token",
            "X-Emby-Client": "Jellyfin for Android",
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 16; device; wv) "
                "AppleWebKit/537.36 Chrome/151 Mobile Safari/537.36"
            ),
        }
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.post(
                "/Items/android-web-direct-item/PlaybackInfo",
                headers=headers,
                json={
                    "IsPlayback": True,
                    "DeviceProfile": {
                        "Name": "",
                        "DirectPlayProfiles": [{"Container": "mp4"}],
                        "TranscodingProfiles": [{"Container": "ts"}],
                    },
                },
            )
            source = playback.json()["MediaSources"][0]
            stream = client.get(
                source["Path"],
                headers={
                    **headers,
                    "Origin": "http://testserver",
                    "Range": "bytes=0-1",
                    "Sec-Fetch-Dest": "video",
                    "Sec-Fetch-Mode": "no-cors",
                },
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertTrue(source["SupportsDirectPlay"])
        self.assertTrue(source["SupportsTranscoding"])
        self.assertEqual(stream.status_code, 302)
        self.assertEqual(
            stream.headers["location"],
            "https://signed.invalid/android-web-direct-file",
        )
        self.assertEqual(
            _FakeGuangYaClient.calls,
            ["android-web-direct-file"],
        )
        authorize.assert_awaited_once()

    def test_android_native_profile_overrides_webview_user_agent(self):
        request = SimpleNamespace(
            headers=Headers({
                "X-Emby-Client": "Jellyfin for Android",
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 16; device; wv) "
                    "AppleWebKit/537.36 Chrome/151 Mobile Safari/537.36"
                ),
            })
        )

        mode = media_proxy._native_playback_rewrite_mode(
            request,
            {"DeviceProfile": {"Name": "Jellyfin for Android"}},
        )

        self.assertEqual(mode, "jellyfin_android")

    def test_android_infers_missing_container_and_uses_direct_stream(self):
        cases = (
            (
                {
                    "Id": "android-source-a",
                    "Path": "/playgy/android-a/e/1/video.mkv",
                },
                "/Videos/android-item/stream.mkv",
            ),
            (
                {
                    "Id": "android-source-b",
                    "Path": "/playgy/android-b/e/1/video",
                    "Container": "mkv;unsafe",
                },
                "/Videos/android-item/stream",
            ),
        )

        for source, expected_path in cases:
            with self.subTest(source=source):
                payload = {"MediaSources": [copy.deepcopy(source)]}
                with patch(
                    "app.modules.media_proxy.database.get_media_proxy_binding",
                    return_value=None,
                ):
                    rewritten, changed = media_proxy.rewrite_playback_info(
                        payload,
                        7,
                        "android-item",
                        native_client_mode="jellyfin_android",
                    )

                self.assertTrue(changed)
                rewritten_source = rewritten["MediaSources"][0]
                self.assertEqual(
                    urlsplit(rewritten_source["DirectStreamUrl"]).path,
                    expected_path,
                )
                self.assertFalse(rewritten_source["SupportsDirectPlay"])
                self.assertTrue(rewritten_source["SupportsDirectStream"])
                self.assertFalse(rewritten_source["SupportsTranscoding"])
                if expected_path.endswith(".mkv"):
                    self.assertEqual(rewritten_source["Container"], "mkv")
                self.assertEqual(
                    rewritten_source["TranscodingSubProtocol"], "http"
                )

    def test_findroid_capability_relays_full_redirect_chain_and_rejects_same_nat_standard_url(self):
        payload = {
            "PlaySessionId": "findroid-upstream-session",
            "MediaSources": [
                {
                    "Id": "findroid-source",
                    "Path": "/playgy/findroid-file/etag/1/video.mkv",
                    "Protocol": "Http",
                    "Container": "mkv",
                }
            ]
        }
        first_hop = _FakeUpstreamResponse(
            status_code=302,
            headers={"location": "https://edge.invalid/final.mkv"},
        )
        final_media = _FakeUpstreamResponse(
            status_code=206,
            body=b"x",
            content_type="video/x-matroska",
            headers={
                "accept-ranges": "bytes",
                "content-length": "1",
                "content-range": "bytes 0-0/100",
            },
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            first_hop,
            final_media,
            _FakeUpstreamResponse(
                body=b"unauthorized-standard-stream-fallback",
                content_type="video/x-matroska",
            ),
        ]
        app = media_proxy.create_proxy_app(7)
        _FakeGuangYaClient.results["findroid-file"] = [
            "http://signed.invalid/findroid-file"
        ]
        headers = {
            "Authorization": (
                'MediaBrowser Client="Findroid", Device="Pixel 10 Pro", '
                'DeviceId="findroid-device", Version="1.1.0", '
                'Token="client-token"'
            ),
            "User-Agent": "ExoPlayerLib/1.10.1",
        }
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.post(
                "/Items/findroid-item/PlaybackInfo",
                headers=headers,
                json={"IsPlayback": True},
            )
            source = playback.json()["MediaSources"][0]
            # Findroid 的 DataSource 请求可能不再携带 Findroid/ExoPlayer 标记；
            # capability 关联出的已验证会话仍必须接管完整 signed URL 跳转链。
            stream = client.get(
                source["Path"],
                headers={
                    "Range": "bytes=0-0",
                    "User-Agent": "okhttp/4.12.0",
                },
                follow_redirects=False,
            )
            # 即使网络对端地址与刚完成 PlaybackInfo 的 Findroid 相同，没有
            # capability / token / PlaySessionId 的标准 URL也不能复用授权。
            same_nat_standard_stream = client.get(
                "/Videos/findroid-item/stream"
                "?static=true&MediaSourceId=findroid-source",
                headers={"Range": "bytes=0-0"},
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(source["Protocol"], "Http")
        self.assertTrue(source["SupportsDirectPlay"])
        self.assertTrue(source["SupportsDirectStream"])
        self.assertTrue(source["IsRemote"])
        self.assertEqual(
            urlsplit(source["Path"]).path,
            "/Videos/findroid-item/stream",
        )
        self.assertEqual(urlsplit(source["Path"]).scheme, "http")
        self.assertEqual(urlsplit(source["Path"]).netloc, "testserver")
        self.assertIn("_mfps=", source["Path"])
        self.assertEqual(source["TranscodingSubProtocol"], "http")
        self.assertEqual(stream.status_code, 206)
        self.assertEqual(stream.content, b"x")
        self.assertNotIn("location", stream.headers)
        self.assertEqual(stream.headers["content-range"], "bytes 0-0/100")
        first_request = _FakeAsyncClient.requests[1]
        final_request = _FakeAsyncClient.requests[2]
        self.assertEqual(first_request.url.scheme, "http")
        self.assertEqual(first_request.url.host, "203.0.113.10")
        self.assertEqual(first_request.headers["Host"], "signed.invalid")
        self.assertEqual(first_request.headers["Range"], "bytes=0-0")
        self.assertEqual(final_request.url.scheme, "https")
        self.assertEqual(final_request.url.host, "203.0.113.10")
        self.assertEqual(final_request.headers["Host"], "edge.invalid")
        self.assertEqual(final_request.headers["Range"], "bytes=0-0")
        self.assertTrue(first_hop.closed)
        self.assertTrue(final_media.closed)
        self.assertEqual(same_nat_standard_stream.status_code, 200)
        self.assertEqual(
            same_nat_standard_stream.content,
            b"unauthorized-standard-stream-fallback",
        )
        self.assertEqual(_FakeGuangYaClient.calls, ["findroid-file"])

    def test_invalid_findroid_token_does_not_mint_capability_path(self):
        payload = {
            "PlaySessionId": "findroid-invalid-session",
            "MediaSources": [
                {
                    "Id": "findroid-invalid-source",
                    "Path": "/playgy/findroid-invalid-file/e/1/video.mkv",
                    "Protocol": "Http",
                }
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        authorize = AsyncMock(return_value=False)
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.post(
                "/Items/findroid-invalid-item/PlaybackInfo",
                headers={
                    "Authorization": (
                        'MediaBrowser Client="Findroid", Device="Pixel", '
                        'DeviceId="findroid-device", Version="1.1.0", '
                        'Token="invalid-token"'
                    ),
                    "User-Agent": "ExoPlayerLib/1.10.1",
                },
                json={"IsPlayback": True},
            )

        self.assertEqual(playback.status_code, 200)
        source = playback.json()["MediaSources"][0]
        self.assertEqual(source["Path"], payload["MediaSources"][0]["Path"])
        self.assertNotIn("_mfps=", source["Path"])
        self.assertEqual(_FakeGuangYaClient.calls, [])
        authorize.assert_awaited_once()

    def test_android_playback_info_without_device_recovers_stream_with_device_via_verified_token_scope(self):
        payload = {
            "PlaySessionId": "android-no-device-session",
            "MediaSources": [
                {
                    "Id": "android-no-device-source",
                    "Path": "/playgy/android-no-device-file/e/1/video.mkv",
                    "Container": "mkv",
                }
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        app = media_proxy.create_proxy_app(7)
        playback_headers = {
            "Authorization": (
                'MediaBrowser Client="Jellyfin for Android", '
                'Device="Pixel", Version="2.7.0", Token="client-token"'
            ),
            "User-Agent": "Jellyfin Android",
        }
        authorize = AsyncMock(return_value=True)
        _FakeGuangYaClient.results["android-no-device-file"] = [
            "http://signed.invalid/android-no-device-file"
        ]
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.post(
                "/Items/android-no-device-item/PlaybackInfo",
                headers=playback_headers,
                json={"IsPlayback": True},
            )
            stream = client.get(
                "/Videos/android-no-device-item/stream.mkv"
                "?static=true&MediaSourceId=android-no-device-source"
                "&PlaySessionId=android-no-device-session&DeviceId=android-device",
                headers={
                    "Range": "bytes=0-0",
                    "User-Agent": "ExoPlayerLib/1.4.1",
                },
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(stream.status_code, 302)
        self.assertEqual(
            stream.headers["location"],
            "http://signed.invalid/android-no-device-file",
        )
        self.assertEqual(_FakeGuangYaClient.calls, ["android-no-device-file"])
        authorize.assert_awaited_once()

    def test_android_tokenless_session_is_bound_to_client_address(self):
        payload = {
            "PlaySessionId": "address-bound-session",
            "MediaSources": [
                {
                    "Id": "cloud",
                    "Path": "/playgy/address-bound-file/etag/1/video.mkv",
                }
            ],
        }
        fallback = _FakeUpstreamResponse(
            body=b"upstream-only",
            content_type="video/x-matroska",
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            fallback,
        ]
        authorize = AsyncMock(return_value=True)
        _FakeGuangYaClient.results["address-bound-file"] = [
            "http://signed.invalid/address-bound-file"
        ]
        app = media_proxy.create_proxy_app(7)
        playback_headers = {
            "Authorization": (
                'MediaBrowser Client="Jellyfin for Android", '
                'Device="Pixel", DeviceId="android-device", '
                'Version="2.7.0", Token="client-token"'
            ),
            "User-Agent": "Jellyfin Android",
        }
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
        ):
            with TestClient(
                app,
                client=("10.0.0.10", 50000),
                raise_server_exceptions=False,
            ) as client:
                playback = client.post(
                    "/Items/address-bound-item/PlaybackInfo",
                    headers=playback_headers,
                    json={"IsPlayback": True},
                )
                direct_url = playback.json()["MediaSources"][0][
                    "DirectStreamUrl"
                ]
                same_client_capability = client.get(
                    direct_url,
                    headers={
                        "Range": "bytes=0-16",
                        "User-Agent": "Jellyfin Android",
                    },
                    follow_redirects=False,
                )
                # 同一 ASGI app/事件循环中模拟请求来自另一台终端，避免
                # TestClient 重启 lifespan 干扰播放记录器。
                client._transport.client = ("10.0.0.11", 50001)
                stolen_capability = client.get(
                    direct_url,
                    headers={"User-Agent": "Jellyfin Android"},
                    follow_redirects=False,
                )
                stream = client.get(
                    "/Videos/address-bound-item/stream"
                    "?mediaSourceId=cloud&playSessionId=address-bound-session"
                    "&deviceId=android-device",
                    headers={"User-Agent": "Jellyfin Android"},
                    follow_redirects=False,
                )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(same_client_capability.status_code, 302)
        self.assertEqual(
            same_client_capability.headers["location"],
            "http://signed.invalid/address-bound-file",
        )
        self.assertEqual(stolen_capability.status_code, 401)
        self.assertEqual(stream.status_code, 200)
        self.assertEqual(stream.content, b"upstream-only")
        self.assertEqual(_FakeGuangYaClient.calls, ["address-bound-file"])
        self.assertTrue(fallback.closed)
        authorize.assert_awaited_once()

    def test_android_device_session_does_not_inherit_ambiguous_previous_source(self):
        payload = {
            "PlaySessionId": "multi-source-session",
            "MediaSources": [
                {
                    "Id": "source-a",
                    "Path": "/playgy/multi-source-file-a/e/1/a.mkv",
                    "Container": "mkv",
                },
                {
                    "Id": "source-b",
                    "Path": "/playgy/multi-source-file-b/e/1/b.mkv",
                    "Container": "mkv",
                },
            ],
        }
        fallback = _FakeUpstreamResponse(
            body=b"ambiguous-source-upstream",
            content_type="video/x-matroska",
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            fallback,
        ]
        authorize = AsyncMock(return_value=True)
        _FakeGuangYaClient.results["multi-source-file-a"] = [
            "http://signed.invalid/multi-source-file-a"
        ]
        _FakeGuangYaClient.results["multi-source-file-b"] = [
            "http://signed.invalid/multi-source-file-b"
        ]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
        ):
            with TestClient(
                app,
                client=("10.0.0.10", 50000),
                raise_server_exceptions=False,
            ) as client:
                playback = client.post(
                    "/Items/multi-source-item/PlaybackInfo",
                    headers={
                        "Authorization": (
                            'MediaBrowser Client="Jellyfin for Android", '
                            'Device="Pixel", DeviceId="android-device", '
                            'Version="2.7.0", Token="client-token"'
                        ),
                        "User-Agent": "Jellyfin Android",
                    },
                    json={"IsPlayback": True},
                )
                source_a = client.get(
                    "/Videos/multi-source-item/stream"
                    "?MediaSourceId=source-a&PlaySessionId=multi-source-session"
                    "&DeviceId=android-device",
                    headers={"User-Agent": "Jellyfin Android"},
                    follow_redirects=False,
                )
                ambiguous = client.get(
                    "/Videos/multi-source-item/stream"
                    "?PlaySessionId=multi-source-session&DeviceId=android-device",
                    headers={"User-Agent": "Jellyfin Android"},
                    follow_redirects=False,
                )
                source_b = client.get(
                    "/Videos/multi-source-item/stream"
                    "?MediaSourceId=source-b&PlaySessionId=multi-source-session"
                    "&DeviceId=android-device",
                    headers={"User-Agent": "Jellyfin Android"},
                    follow_redirects=False,
                )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(source_a.status_code, 302)
        self.assertEqual(
            source_a.headers["location"],
            "http://signed.invalid/multi-source-file-a",
        )
        self.assertEqual(ambiguous.status_code, 200)
        self.assertEqual(ambiguous.content, b"ambiguous-source-upstream")
        self.assertEqual(source_b.status_code, 302)
        self.assertEqual(
            source_b.headers["location"],
            "http://signed.invalid/multi-source-file-b",
        )
        self.assertEqual(
            _FakeGuangYaClient.calls,
            ["multi-source-file-a", "multi-source-file-b"],
        )
        self.assertTrue(fallback.closed)
        authorize.assert_awaited_once()

    def test_android_playback_info_rejects_cross_carrier_device_id_conflict(self):
        app = media_proxy.create_proxy_app(7)
        authorize = AsyncMock(return_value=True)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/Items/device-conflict-item/PlaybackInfo?DeviceId=query-device",
                headers={
                    "Authorization": (
                        'MediaBrowser Client="Jellyfin for Android", '
                        'Device="Pixel", DeviceId="auth-device", '
                        'Version="2.7.0", Token="client-token"'
                    ),
                    "User-Agent": "Jellyfin Android",
                },
                json={"IsPlayback": True},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(_FakeAsyncClient.requests, [])
        authorize.assert_not_awaited()

    def test_android_transcode_fallback_bypasses_native_auth_capacity(self):
        payload = {
            "PlaySessionId": "busy-fallback-session",
            "MediaSources": [
                {
                    "Id": "busy-fallback-source",
                    "Path": "/playgy/busy-fallback-file/etag/1/video.mkv",
                    "SupportsDirectPlay": False,
                    "SupportsDirectStream": False,
                    "SupportsTranscoding": True,
                    "TranscodingUrl": "/Videos/busy-fallback/master.m3u8",
                }
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]

        class UnexpectedCapacity:
            @staticmethod
            def acquire(*, blocking):
                raise AssertionError("fallback must not acquire native auth capacity")

            @staticmethod
            def release():
                raise AssertionError("fallback must not release native auth capacity")

        authorize = AsyncMock(return_value=True)
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
            patch(
                "app.modules.media_proxy._native_android_auth_capacity",
                new=UnexpectedCapacity(),
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/Items/busy-fallback/PlaybackInfo",
                headers={
                    "Authorization": (
                        'MediaBrowser Client="Jellyfin for Android", '
                        'Device="Pixel", DeviceId="android-device", '
                        'Version="2.7.0", Token="client-token"'
                    ),
                    "User-Agent": "Jellyfin Android",
                },
                json={
                    "EnableDirectPlay": False,
                    "EnableDirectStream": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        self.assertEqual(media_proxy._playback_sessions._capability_index, {})
        self.assertEqual(media_proxy._playback_grants._entries, {})
        authorize.assert_not_awaited()

    def test_android_playback_info_fails_fast_when_auth_capacity_is_full(self):
        payload = {
            "PlaySessionId": "busy-auth-session",
            "MediaSources": [
                {
                    "Id": "cloud",
                    "Path": "/playgy/busy-auth-file/etag/1/video.mkv",
                }
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]

        class BusyCapacity:
            @staticmethod
            def acquire(*, blocking):
                self.assertFalse(blocking)
                return False

            @staticmethod
            def release():
                raise AssertionError("busy capacity must not be released")

        authorize = AsyncMock(return_value=True)
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
            patch(
                "app.modules.media_proxy._native_android_auth_capacity",
                new=BusyCapacity(),
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/Items/busy-auth-item/PlaybackInfo",
                headers={
                    "Authorization": (
                        'MediaBrowser Client="Jellyfin for Android", '
                        'Device="Pixel", DeviceId="android-device", '
                        'Version="2.7.0", Token="client-token"'
                    ),
                    "User-Agent": "Jellyfin Android",
                },
                json={"IsPlayback": True},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"error": "原生客户端播放鉴权繁忙，请稍后重试"},
        )
        authorize.assert_not_awaited()

    def test_invalid_android_token_cannot_mint_tokenless_playback_session(self):
        payload = {
            "PlaySessionId": "invalid-token-session",
            "MediaSources": [
                {
                    "Id": "cloud",
                    "Path": "/playgy/invalid-token-file/etag/1/video.mkv",
                }
            ],
        }
        fallback = _FakeUpstreamResponse(
            body=b"upstream-only",
            content_type="video/x-matroska",
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            fallback,
        ]
        authorize = AsyncMock(return_value=False)
        app = media_proxy.create_proxy_app(7)
        playback_headers = {
            "Authorization": (
                'MediaBrowser Client="Jellyfin for Android", '
                'Device="Pixel", DeviceId="android-device", '
                'Version="2.7.0", Token="invalid-token"'
            ),
            "User-Agent": "Jellyfin Android",
        }
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.post(
                "/Items/invalid-token-item/PlaybackInfo",
                headers=playback_headers,
                json={"IsPlayback": True},
            )
            stream = client.get(
                "/Videos/invalid-token-item/stream"
                "?mediaSourceId=cloud&playSessionId=invalid-token-session"
                "&deviceId=android-device",
                headers={"User-Agent": "Jellyfin Android"},
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(stream.status_code, 200)
        self.assertEqual(stream.content, b"upstream-only")
        self.assertEqual(_FakeGuangYaClient.calls, [])
        self.assertTrue(fallback.closed)
        authorize.assert_awaited_once()

    def test_non_android_playback_session_cannot_authorize_tokenless_stream(self):
        payload = {
            "PlaySessionId": "yamby-session",
            "MediaSources": [
                {
                    "Id": "cloud",
                    "Path": "/playgy/yamby-file/etag/1/video.mkv",
                }
            ],
        }
        fallback = _FakeUpstreamResponse(
            body=b"upstream-only",
            content_type="video/x-matroska",
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            fallback,
        ]
        app = media_proxy.create_proxy_app(7)
        playback_headers = {
            "Authorization": (
                'MediaBrowser Client="Yamby", Device="Phone", '
                'DeviceId="yamby-device", Version="2.0.5.5", '
                'Token="client-token"'
            )
        }
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.post(
                "/Items/yamby-tokenless-item/PlaybackInfo",
                headers=playback_headers,
                json={"IsPlayback": True},
            )
            stream = client.get(
                "/Videos/yamby-tokenless-item/stream"
                "?mediaSourceId=cloud&playSessionId=yamby-session"
                "&deviceId=yamby-device",
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(stream.status_code, 200)
        self.assertEqual(stream.content, b"upstream-only")
        self.assertEqual(_FakeGuangYaClient.calls, [])
        self.assertTrue(fallback.closed)

    def test_request_play_session_is_not_used_when_upstream_omits_session_id(self):
        payload = {
            "MediaSources": [
                {
                    "Id": "cloud",
                    "Path": "/playgy/no-upstream-session-file/etag/1/video.mkv",
                }
            ],
        }
        fallback = _FakeUpstreamResponse(
            body=b"upstream-only",
            content_type="video/x-matroska",
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            fallback,
        ]
        app = media_proxy.create_proxy_app(7)
        playback_headers = {
            "Authorization": (
                'MediaBrowser Client="Jellyfin for Android", '
                'Device="Pixel", DeviceId="android-device", '
                'Version="2.7.0", Token="client-token"'
            )
        }
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.post(
                "/Items/no-upstream-session-item/PlaybackInfo"
                "?PlaySessionId=client-chosen-session",
                headers=playback_headers,
                json={"IsPlayback": True},
            )
            stream = client.get(
                "/Videos/no-upstream-session-item/stream"
                "?mediaSourceId=cloud&playSessionId=client-chosen-session"
                "&deviceId=android-device",
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(stream.status_code, 200)
        self.assertEqual(stream.content, b"upstream-only")
        self.assertEqual(_FakeGuangYaClient.calls, [])
        self.assertTrue(fallback.closed)

    def test_web_file_protocol_standard_stream_still_relays_same_origin(self):
        payload = {
            "PlaySessionId": "web-standard-session",
            "MediaSources": [
                {
                    "Id": "cloud",
                    "Path": "/playgy/web-standard-file/etag/1/video.mp4",
                    "Protocol": "Http",
                    "SupportsTranscoding": True,
                    "TranscodingUrl": "/Videos/web-standard-item/master.m3u8",
                }
            ],
        }
        media_response = _FakeUpstreamResponse(
            status_code=206,
            body=b"w",
            content_type="video/mp4",
            headers={
                "content-length": "1",
                "content-range": "bytes 0-0/100",
            },
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            media_response,
        ]
        authorize = AsyncMock(return_value=True)
        app = media_proxy.create_proxy_app(7)
        authorization = (
            'MediaBrowser Client="Jellyfin Web", Device="Browser", '
            'Token="client-token"'
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=authorize,
            ),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/web-standard-item/PlaybackInfo?api_key=client-token",
                headers={"X-Emby-Authorization": authorization},
            )
            source = playback.json()["MediaSources"][0]
            stream = client.get(
                "/Videos/web-standard-item/stream"
                "?static=true&MediaSourceId=cloud"
                "&PlaySessionId=web-standard-session&DeviceId=browser-1",
                headers={
                    "X-Emby-Authorization": authorization,
                    "Range": "bytes=0-0",
                    "Sec-Fetch-Dest": "video",
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                },
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(source["Protocol"], "File")
        self.assertEqual(stream.status_code, 206)
        self.assertEqual(stream.content, b"w")
        self.assertNotIn("location", stream.headers)
        self.assertEqual(_FakeGuangYaClient.calls, ["web-standard-file"])
        self.assertEqual(len(_FakeAsyncClient.requests), 2)
        self.assertTrue(media_response.closed)
        authorize.assert_awaited_once()

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
            upstream_session_stream = client.get(
                "/emby/Videos/item-session/stream"
                "?MediaSourceId=cloud&PlaySessionId=emby-session-1",
                headers={"X-Emby-Token": "client-token"},
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(stream.status_code, 302)
        self.assertEqual(upstream_session_stream.status_code, 302)
        self.assertEqual(len(captured), 3)
        self.assertEqual(
            {record["playback_session_key"] for record in captured},
            {captured[0]["playback_session_key"]},
        )
        self.assertTrue(captured[0]["playback_session_key"])
        self.assertTrue(all(record["media_item_id"] == "item-session" for record in captured))
        self.assertTrue(all(record["media_name"] == "a.mkv" for record in captured))
        self.assertEqual(captured[-1]["media_source_id"], "cloud")
        self.assertEqual(captured[-1]["guangya_file_id"], "file-session")

    def test_quality_switch_sessions_keep_recent_media_name_without_merging(self):
        class Raw:
            token = "provider-secret"

        class GuangYa:
            logged_in = True
            raw = Raw()

            def get_download_url(self, _file_id, **_kwargs):
                return "https://signed.invalid/file?Expires=4102444800"

        payload = {
            "Name": "Quality Switch Movie",
            "PlaySessionId": "quality-original",
            "MediaSources": [
                {
                    "Id": "cloud",
                    "Path": "/playgy/file-quality/e/1/quality.mkv",
                }
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            _FakeUpstreamResponse(
                body=b"#EXTM3U\n#EXT-X-VERSION:3\n",
                content_type="application/vnd.apple.mpegurl",
            ),
        ]
        captured = []
        recorder = media_proxy.PlaybackRecordWriter(
            write_record=lambda record: captured.append(dict(record))
        )
        app = media_proxy.create_proxy_app(7, playback_record_writer=recorder)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch("app.modules.media_proxy.GuangYaClient", GuangYa),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/emby/Items/item-quality/PlaybackInfo?api_key=client-token"
            )
            direct = client.get(
                "/emby/Videos/item-quality/stream"
                "?MediaSourceId=cloud&PlaySessionId=quality-direct",
                headers={"X-Emby-Token": "client-token"},
                follow_redirects=False,
            )
            hls = client.get(
                "/emby/Videos/item-quality/master.m3u8"
                "?MediaSourceId=cloud&PlaySessionId=quality-hls",
                headers={"X-Emby-Token": "client-token"},
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(direct.status_code, 302)
        self.assertEqual(hls.status_code, 200)
        self.assertEqual(len(captured), 3)
        self.assertEqual(
            [record["media_name"] for record in captured],
            ["Quality Switch Movie"] * 3,
        )
        self.assertEqual(
            len({record["playback_session_key"] for record in captured}),
            3,
        )

    def test_playback_info_does_not_cache_title_for_unconfirmed_source(self):
        payload = {
            "Name": "Confirmed Source Movie",
            "PlaySessionId": "confirmed-source-session",
            "MediaSources": [
                {
                    "Id": "source-confirmed",
                    "Path": "/playgy/file-confirmed/e/1/confirmed.mkv",
                }
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
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
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/Items/item-confirmed/PlaybackInfo?api_key=client-token",
                json={"MediaSourceId": "source-forged"},
            )

        scope = media_proxy._auth_scope_fingerprint("client-token")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            media_proxy._playback_sessions.resolve_media_name(
                scope,
                7,
                "item-confirmed",
                ("source-forged",),
            ),
            "",
        )
        self.assertEqual(
            media_proxy._playback_sessions.resolve_media_name(
                scope,
                7,
                "item-confirmed",
                ("source-confirmed",),
            ),
            "Confirmed Source Movie",
        )

    def test_hls_conflicting_playback_parameters_are_rejected_before_upstream(self):
        app = media_proxy.create_proxy_app(7)
        queries = (
            "MediaSourceId=source-a&MediaSourceId=source-b"
            "&PlaySessionId=session-a",
            "MediaSourceId=source-a&PlaySessionId=session-a"
            "&PlaySessionId=session-b",
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            for query in queries:
                with self.subTest(query=query):
                    response = client.get(
                        f"/Videos/hls-item/master.m3u8?{query}"
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(
                        response.json(),
                        {"error": "播放参数重复或冲突"},
                    )

        self.assertEqual(_FakeAsyncClient.requests, [])

    def test_repeated_playback_info_with_same_upstream_session_stays_aggregated(self):
        payload = {
            "PlaySessionId": "emby-session-repeat",
            "MediaSources": [
                {"Id": "cloud", "Path": "/playgy/file-repeat/e/1/repeat.mkv"}
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
        ]
        captured = []
        recorder = media_proxy.PlaybackRecordWriter(
            write_record=lambda record: captured.append(dict(record))
        )
        app = media_proxy.create_proxy_app(7, playback_record_writer=recorder)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            first = client.get(
                "/Items/repeat-item/PlaybackInfo?api_key=client-token"
            )
            second = client.get(
                "/Items/repeat-item/PlaybackInfo?api_key=client-token"
            )
            first_url = first.json()["MediaSources"][0]["DirectStreamUrl"]
            second_url = second.json()["MediaSources"][0]["DirectStreamUrl"]
            first_stream = client.get(first_url, follow_redirects=False)
            second_stream = client.get(second_url, follow_redirects=False)

        first_capability = parse_qs(urlsplit(first_url).query)["_mfps"][0]
        second_capability = parse_qs(urlsplit(second_url).query)["_mfps"][0]
        self.assertNotEqual(first_capability, second_capability)
        self.assertEqual(first_stream.status_code, 302)
        self.assertEqual(second_stream.status_code, 302)
        self.assertEqual(len(captured), 4)
        self.assertEqual(
            {record["playback_session_key"] for record in captured},
            {captured[0]["playback_session_key"]},
        )
        self.assertTrue(all(record["media_name"] == "repeat.mkv" for record in captured))

    def test_passthrough_playback_info_records_safe_media_name_and_session(self):
        payload = {
            "PlaySessionId": "local-session-1",
            "MediaSources": [
                {
                    "Id": "local",
                    "Path": (
                        "https://user:password@media.invalid/private/"
                        "Local%20Movie.mkv?api_key=secret"
                    ),
                }
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
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/local-item/PlaybackInfo?api_key=client-token"
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0]["playback_session_key"])
        self.assertEqual(captured[0]["media_item_id"], "local-item")
        self.assertEqual(captured[0]["media_name"], "Local Movie.mkv")
        self.assertNotIn("secret", json.dumps(captured[0], ensure_ascii=False))

    def test_browser_stream_uses_opaque_session_when_custom_token_header_is_missing(self):
        payload = {
            "PlaySessionId": "upstream-session-is-not-a-capability",
            "MediaSources": [
                {"Id": "cloud", "Path": "/playgy/browser-file/e/1/a.mkv"}
            ],
        }
        media_response = _FakeUpstreamResponse(
            status_code=206,
            body=b"browser-media",
            content_type="video/mp4",
            headers={
                "content-length": "13",
                "content-range": "bytes 0-12/13",
            },
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            media_response,
        ]
        authorize = AsyncMock(return_value=False)
        captured = []
        recorder = media_proxy.PlaybackRecordWriter(
            write_record=lambda record: captured.append(dict(record))
        )
        app = media_proxy.create_proxy_app(7, playback_record_writer=recorder)
        authorization = (
            'MediaBrowser Client="Jellyfin Web", Device="Legacy Browser", '
            'Token="client-token"'
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy._client_is_authorized", new=authorize),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/browser-item/PlaybackInfo?api_key=client-token",
                headers={"X-Emby-Authorization": authorization},
            )
            direct_url = playback.json()["MediaSources"][0]["DirectStreamUrl"]
            direct_query = parse_qs(urlsplit(direct_url).query)
            stream = client.get(
                direct_url,
                headers={
                    "Range": "bytes=0-12",
                    "User-Agent": "Mozilla/5.0 Legacy Jellyfin Web",
                },
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertNotIn("api_key", direct_query)
        self.assertNotEqual(
            direct_query["_mfps"], ["upstream-session-is-not-a-capability"]
        )
        self.assertIn("no-store", playback.headers["cache-control"])
        self.assertEqual(playback.headers["pragma"], "no-cache")
        self.assertEqual(playback.headers["referrer-policy"], "no-referrer")
        self.assertEqual(stream.status_code, 206)
        self.assertEqual(stream.content, b"browser-media")
        self.assertNotIn("location", stream.headers)
        self.assertEqual(_FakeGuangYaClient.calls, ["browser-file"])
        self.assertEqual(len(_FakeAsyncClient.requests), 2)
        self.assertEqual(captured[-1]["media_item_id"], "browser-item")
        self.assertEqual(captured[-1]["media_source_id"], "cloud")
        self.assertEqual(captured[-1]["guangya_file_id"], "browser-file")
        self.assertTrue(media_response.closed)
        authorize.assert_not_awaited()

    def test_web_standard_stream_accepts_compact_uuid_after_hyphenated_playback_info(self):
        item_hyphenated = "b3851937-35e9-8d81-287b-2b7ccc84fe42"
        item_compact = "b385193735e98d81287b2b7ccc84fe42"
        payload = {
            "MediaSources": [
                {
                    "Id": item_hyphenated,
                    "Path": "/playgy/uuid-browser-file/e/1/a.mkv",
                    "SupportsDirectPlay": True,
                    "SupportsDirectStream": True,
                    "SupportsTranscoding": True,
                    "TranscodingSubProtocol": "hls",
                }
            ]
        }
        media_response = _FakeUpstreamResponse(
            status_code=206,
            body=b"uuid-browser",
            content_type="video/x-matroska",
            headers={
                "content-length": "12",
                "content-range": "bytes 0-11/120",
            },
        )
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
            media_response,
        ]
        app = media_proxy.create_proxy_app(7)
        authorization = (
            'MediaBrowser Client="Jellyfin Web", Device="Chrome", '
            'Token="client-token"'
        )
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            patch(
                "app.modules.media_proxy._pin_signed_media_target",
                side_effect=self._signed_target,
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                f"/Items/{item_hyphenated}/PlaybackInfo?api_key=client-token",
                headers={"X-Emby-Authorization": authorization},
            )
            stream = client.get(
                f"/Videos/{item_compact}/stream"
                f"?static=true&MediaSourceId={item_compact}&api_key=client-token",
                headers={
                    "Range": "bytes=0-11",
                    "Origin": "http://testserver",
                    "Sec-Fetch-Dest": "video",
                    "Sec-Fetch-Mode": "cors",
                    "User-Agent": "Mozilla/5.0 Jellyfin Web",
                },
                follow_redirects=False,
            )

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(stream.status_code, 206)
        self.assertEqual(stream.content, b"uuid-browser")
        self.assertNotIn("location", stream.headers)
        self.assertEqual(_FakeGuangYaClient.calls, ["uuid-browser-file"])
        self.assertTrue(media_response.closed)

    def test_active_long_playback_renews_authorization_without_proxying_media(self):
        now = [100.0]
        sessions = media_proxy.PlaybackSessionRegistry(
            ttl_seconds=3600,
            capability_ttl_seconds=900,
            capability_max_ttl_seconds=7200,
            clock=lambda: now[0],
        )
        mappings = media_proxy.DynamicGuangYaMappings(
            ttl_seconds=900,
            max_ttl_seconds=7200,
            clock=lambda: now[0],
        )
        scopes = media_proxy.ItemLevelBindingScopes(
            ttl_seconds=900,
            max_ttl_seconds=7200,
            clock=lambda: now[0],
        )
        grants = media_proxy.PlaybackGrantRegistry(
            ttl_seconds=900,
            max_ttl_seconds=7200,
            clock=lambda: now[0],
        )
        payload = {
            "MediaSources": [
                {"Id": "cloud", "Path": "/playgy/long-file/e/1/a.mkv"}
            ]
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        authorize = AsyncMock(return_value=False)
        app = media_proxy.create_proxy_app(7)
        with (
            patch.object(media_proxy, "_playback_sessions", sessions),
            patch.object(media_proxy, "_dynamic_guangya_mappings", mappings),
            patch.object(media_proxy, "_item_level_binding_scopes", scopes),
            patch.object(media_proxy, "_playback_grants", grants),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch(
                "app.modules.media_proxy.database.get_media_proxy_binding",
                return_value=None,
            ),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy._client_is_authorized", new=authorize),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/long-item/PlaybackInfo?api_key=client-token"
            )
            direct_url = playback.json()["MediaSources"][0]["DirectStreamUrl"]
            now[0] = 999.0
            first = client.get(direct_url, follow_redirects=False)
            now[0] = 1800.0
            second = client.get(direct_url, follow_redirects=False)

        self.assertEqual(playback.status_code, 200)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(first.headers["location"], "https://signed.invalid/long-file")
        self.assertEqual(second.headers["location"], first.headers["location"])
        self.assertEqual(len(_FakeAsyncClient.requests), 1)
        authorize.assert_not_awaited()

    def test_client_supplied_mfps_cannot_mint_browser_capability(self):
        payload = {
            "MediaSources": [
                {"Id": "cloud", "Path": "/playgy/browser-file/e/1/a.mkv"}
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
        ]
        authorize = AsyncMock(return_value=True)
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy._client_is_authorized", new=authorize),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/browser-item/PlaybackInfo?api_key=client-token"
            )
            source_id = playback.json()["MediaSources"][0]["Id"]
            attacker_url = (
                "/Videos/browser-item/stream?MediaSourceId="
                f"{source_id}&_mfps=attacker-selected"
            )
            authenticated = client.get(
                f"{attacker_url}&api_key=client-token", follow_redirects=False
            )
            unauthenticated = client.get(attacker_url, follow_redirects=False)

        self.assertEqual(authenticated.status_code, 401)
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(_FakeGuangYaClient.calls, [])
        authorize.assert_not_awaited()
        self.assertEqual(len(_FakeAsyncClient.requests), 1)

    def test_unknown_mfps_with_raw_credential_is_rejected_before_upstream(self):
        authorize = AsyncMock(return_value=True)
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy._client_is_authorized", new=authorize),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Videos/unbound/stream?MediaSourceId=source"
                "&_mfps=unknown&_mfss=invalid&api_key=client-token",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(_FakeAsyncClient.requests, [])
        authorize.assert_not_awaited()

    def test_unknown_mfps_on_hls_stream_is_rejected_before_upstream(self):
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Videos/unbound/stream.m3u8?MediaSourceId=source"
                "&_mfps=unknown&_mfss=invalid&api_key=client-token",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(_FakeAsyncClient.requests, [])

    def test_capability_cannot_authorize_direct_playgy_for_another_item(self):
        first_payload = {
            "MediaSources": [
                {"Id": "source-a", "Path": "/playgy/file-a/e/1/a.mkv"}
            ],
        }
        second_payload = {
            "MediaSources": [
                {"Id": "source-b", "Path": "/playgy/file-b/e/1/b.mkv"}
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(first_payload).encode("utf-8"),
                content_type="application/json",
            ),
            _FakeUpstreamResponse(
                body=json.dumps(second_payload).encode("utf-8"),
                content_type="application/json",
            ),
        ]
        authorize = AsyncMock(return_value=False)
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy._client_is_authorized", new=authorize),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            first = client.get("/Items/item-a/PlaybackInfo?api_key=client-token")
            client.get("/Items/item-b/PlaybackInfo?api_key=client-token")
            capability = parse_qs(
                urlsplit(first.json()["MediaSources"][0]["DirectStreamUrl"]).query
            )["_mfps"][0]
            response = client.get(
                f"/playgy/file-b/e/1/b.mkv?_mfps={capability}",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(_FakeGuangYaClient.calls, [])
        authorize.assert_not_awaited()

    def test_capability_cannot_cross_items_on_stream_route(self):
        first_payload = {
            "MediaSources": [
                {"Id": "source-a", "Path": "/playgy/file-a/e/1/a.mkv"}
            ],
        }
        second_payload = {
            "MediaSources": [
                {"Id": "source-b", "Path": "/playgy/file-b/e/1/b.mkv"}
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(first_payload).encode("utf-8"),
                content_type="application/json",
            ),
            _FakeUpstreamResponse(
                body=json.dumps(second_payload).encode("utf-8"),
                content_type="application/json",
            ),
            _FakeUpstreamResponse(status_code=401, body=b"unauthorized"),
        ]
        authorize = AsyncMock(return_value=False)
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy._client_is_authorized", new=authorize),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            first = client.get("/Items/item-a/PlaybackInfo?api_key=client-token")
            second = client.get("/Items/item-b/PlaybackInfo?api_key=client-token")
            first_query = parse_qs(
                urlsplit(first.json()["MediaSources"][0]["DirectStreamUrl"]).query
            )
            second_url = urlsplit(
                second.json()["MediaSources"][0]["DirectStreamUrl"]
            )
            cross_item_url = (
                f"{second_url.path}?MediaSourceId=source-b"
                f"&_mfps={first_query['_mfps'][0]}"
                f"&_mfss={first_query['_mfss'][0]}"
            )
            rejected = client.get(cross_item_url, follow_redirects=False)
            accepted = client.get(
                first.json()["MediaSources"][0]["DirectStreamUrl"],
                follow_redirects=False,
            )

        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(_FakeGuangYaClient.calls, ["file-a"])
        authorize.assert_not_awaited()
        self.assertEqual(len(_FakeAsyncClient.requests), 2)

    def test_capability_source_signature_prevents_media_source_mutation(self):
        payload = {
            "MediaSources": [
                {"Id": "source-a", "Path": "/playgy/file-a/e/1/a.mkv"},
                {"Id": "source-b", "Path": "/playgy/file-b/e/1/b.mkv"},
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
        ]
        authorize = AsyncMock(return_value=False)
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy._client_is_authorized", new=authorize),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/multi-source/PlaybackInfo?api_key=client-token"
            )
            sources = playback.json()["MediaSources"]
            source_a_url = urlsplit(sources[0]["DirectStreamUrl"])
            source_b_url = urlsplit(sources[1]["DirectStreamUrl"])
            source_a_query = parse_qs(source_a_url.query)
            source_b_query = parse_qs(source_b_url.query)
            mutated = client.get(
                f"{source_a_url.path}?MediaSourceId=source-b"
                f"&_mfps={source_a_query['_mfps'][0]}"
                f"&_mfss={source_a_query['_mfss'][0]}",
                follow_redirects=False,
            )
            duplicated = client.get(
                f"{sources[0]['DirectStreamUrl']}&MediaSourceId=source-b",
                follow_redirects=False,
            )
            duplicated_empty = client.get(
                f"{sources[0]['DirectStreamUrl']}&MediaSourceId=",
                follow_redirects=False,
            )
            duplicated_same = client.get(
                f"{sources[0]['DirectStreamUrl']}&MediaSourceId=source-a",
                follow_redirects=False,
            )
            accepted = client.get(sources[0]["DirectStreamUrl"], follow_redirects=False)
            accepted_second = client.get(
                sources[1]["DirectStreamUrl"], follow_redirects=False
            )

        self.assertEqual(source_a_query["_mfps"], source_b_query["_mfps"])
        self.assertNotEqual(source_a_query["_mfss"], source_b_query["_mfss"])
        self.assertEqual(mutated.status_code, 401)
        self.assertEqual(duplicated.status_code, 400)
        self.assertEqual(duplicated_empty.status_code, 400)
        self.assertEqual(duplicated_same.status_code, 302)
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(accepted_second.status_code, 302)
        self.assertEqual(_FakeGuangYaClient.calls, ["file-a", "file-b"])
        authorize.assert_not_awaited()
        self.assertEqual(len(_FakeAsyncClient.requests), 1)

    def test_foreign_credential_cannot_poison_existing_capability(self):
        payload = {
            "MediaSources": [
                {"Id": "cloud", "Path": "/playgy/browser-file/e/1/a.mkv"}
            ],
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            ),
        ]
        authorize = AsyncMock(return_value=False)
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            patch("app.modules.media_proxy._client_is_authorized", new=authorize),
            patch("app.modules.media_proxy.GuangYaClient", _FakeGuangYaClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            playback = client.get(
                "/Items/browser-item/PlaybackInfo?api_key=owner-token"
            )
            direct_url = playback.json()["MediaSources"][0]["DirectStreamUrl"]
            poisoned = client.get(
                f"{direct_url}&api_key=foreign-token", follow_redirects=False
            )
            owner = client.get(direct_url, follow_redirects=False)

        self.assertEqual(poisoned.status_code, 401)
        self.assertEqual(owner.status_code, 302)
        self.assertEqual(_FakeGuangYaClient.calls, ["browser-file"])
        authorize.assert_not_awaited()

    def test_failed_playback_info_does_not_issue_capability(self):
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(status_code=500, body=b"upstream failure")
        ]
        app = media_proxy.create_proxy_app(7)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Items/failed-item/PlaybackInfo?api_key=client-token"
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(media_proxy._playback_sessions._capability_index, {})
        self.assertEqual(media_proxy._playback_sessions._entries, {})

    def test_passthrough_playback_info_does_not_issue_capability(self):
        payload = {
            "MediaSources": [
                {"Id": "local", "Path": "/media/local.mkv", "Protocol": "File"}
            ]
        }
        _FakeAsyncClient.responses = [
            _FakeUpstreamResponse(
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        ]
        recorder = media_proxy.PlaybackRecordWriter(write_record=lambda _record: None)
        app = media_proxy.create_proxy_app(7, playback_record_writer=recorder)
        with (
            patch(
                "app.modules.media_proxy.database.get_media_proxy_instance",
                return_value=self._instance(),
            ),
            patch("app.modules.media_proxy.database.get_media_proxy_binding", return_value=None),
            patch("app.modules.media_proxy.httpx.AsyncClient", _FakeAsyncClient),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/Items/local-item/PlaybackInfo?api_key=client-token"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        self.assertEqual(media_proxy._playback_sessions._capability_index, {})
        self.assertEqual(len(media_proxy._playback_sessions._entries), 1)
        entry = next(iter(media_proxy._playback_sessions._entries.values()))
        self.assertEqual(entry.media_name, "local.mkv")
        self.assertEqual(entry.capability_expires_at, 0.0)

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
