from __future__ import annotations

import asyncio
import unittest

import httpx

from app.indexers.errors import IndexerResponseTooLarge, IndexerSecurityError
from app.indexers.http import BrowserImpersonatingHttpClient, FixedHostHttpClient


PUBLIC_DNS = lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))]
PRIVATE_DNS = lambda host, port: [(2, 1, 6, "", ("127.0.0.1", port))]




class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class FakeCurlResponse:
    def __init__(self, status_code=200, content=b"ok", headers=None, url="https://example.com/"):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "text/html"}
        self.url = url


class FakeCurlSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False
        self.cookies = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class IndexerHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        client = getattr(self, "client", None)
        if client is not None:
            await client.aclose()

    async def test_close_tolerates_only_asyncio_closed_loop_provenance(self):
        client = self.make_client(
            lambda request: httpx.Response(200, content=b"ok")
        )
        original = client._client
        await original.aclose()
        closed_loop = asyncio.new_event_loop()
        closed_loop.close()

        class ClosedLoopClient:
            async def aclose(self):
                closed_loop.call_soon(lambda: None)

        client._client = ClosedLoopClient()
        await client.aclose()

        class SameTextClient:
            async def aclose(self):
                raise RuntimeError("Event loop is closed")

        client._client = SameTextClient()
        with self.assertRaisesRegex(RuntimeError, "Event loop is closed"):
            await client.aclose()

        class OtherTextClient:
            async def aclose(self):
                raise RuntimeError("transport shutdown failed")

        client._client = OtherTextClient()
        with self.assertRaisesRegex(RuntimeError, "transport shutdown failed"):
            await client.aclose()

        class ClosedLoopSubclass(RuntimeError):
            pass

        class SubclassClient:
            async def aclose(self):
                try:
                    closed_loop.call_soon(lambda: None)
                except RuntimeError as exc:
                    raise ClosedLoopSubclass(*exc.args) from exc

        client._client = SubclassClient()
        with self.assertRaises(ClosedLoopSubclass):
            await client.aclose()
        self.client = None

    def make_client(self, handler, *, resolver=PUBLIC_DNS, max_response_bytes=1024):
        self.client = FixedHostHttpClient(
            allowed_hosts={"nyaa.si"},
            transport=httpx.MockTransport(handler),
            resolver=resolver,
            max_response_bytes=max_response_bytes,
            timeout_seconds=1,
        )
        return self.client

    async def test_rejects_non_https_off_host_credentials_and_private_dns(self):
        client = self.make_client(lambda request: httpx.Response(200, content=b"ok"))
        rejected = (
            "http://nyaa.si/",
            "https://example.com/",
            "https://user:pass@nyaa.si/",
            "https://localhost/",
        )
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaises(IndexerSecurityError):
                    await client.get(url)

        await client.aclose()
        self.client = FixedHostHttpClient(
            allowed_hosts={"nyaa.si"},
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok")),
            resolver=PRIVATE_DNS,
        )
        with self.assertRaises(IndexerSecurityError):
            await self.client.get("https://nyaa.si/")

    async def test_follows_relative_redirect_but_rejects_redirect_to_unregistered_host(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "/final"})
            return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"done")

        client = self.make_client(handler)
        response = await client.get("https://nyaa.si/start")
        self.assertEqual(response.body, b"done")
        self.assertEqual([httpx.URL(url).path for url in seen], ["/start", "/final"])

        await client.aclose()
        client = self.make_client(
            lambda request: httpx.Response(302, headers={"Location": "https://evil.example/file"})
        )
        with self.assertRaises(IndexerSecurityError):
            await client.get("https://nyaa.si/start")

    async def test_enforces_declared_and_streamed_response_size_limits(self):
        client = self.make_client(
            lambda request: httpx.Response(200, headers={"Content-Length": "11"}, content=b"01234567890"),
            max_response_bytes=10,
        )
        with self.assertRaises(IndexerResponseTooLarge):
            await client.get("https://nyaa.si/")

        await client.aclose()
        client = self.make_client(
            lambda request: httpx.Response(200, content=b"01234567890"),
            max_response_bytes=10,
        )
        with self.assertRaises(IndexerResponseTooLarge):
            await client.get("https://nyaa.si/")

    async def test_browser_client_rewrites_sni_keeps_host_and_impersonates_chrome(self):
        session = FakeCurlSession([FakeCurlResponse(url="https://btbtlb.com/search/demo")])
        client = BrowserImpersonatingHttpClient(
            allowed_hosts={"www.btbtlb.com", "btbtlb.com"},
            resolver=PUBLIC_DNS,
            session_factory=lambda: session,
            sni_host="btbtlb.com",
        )
        self.client = client

        response = await client.get("https://www.btbtlb.com/search/demo")

        url, options = session.calls[0]
        self.assertEqual(url, "https://btbtlb.com/search/demo")
        self.assertEqual(options["headers"]["Host"], "www.btbtlb.com")
        self.assertEqual(options["impersonate"], "chrome")
        self.assertFalse(options["allow_redirects"] )
        self.assertEqual(response.body, b"ok")

    async def test_browser_client_rejects_unregistered_sni_host(self):
        with self.assertRaisesRegex(ValueError, "sni_host"):
            BrowserImpersonatingHttpClient(
                allowed_hosts={"www.btbtlb.com"},
                resolver=PUBLIC_DNS,
                session_factory=lambda: FakeCurlSession([]),
                sni_host="btbtlb.com",
            )

    async def test_browser_client_validates_actual_sni_host_dns(self):
        session = FakeCurlSession([])

        def resolver(host, port):
            address = "127.0.0.1" if host == "btbtlb.com" else "93.184.216.34"
            return [(2, 1, 6, "", (address, port))]

        client = BrowserImpersonatingHttpClient(
            allowed_hosts={"www.btbtlb.com", "btbtlb.com"},
            resolver=resolver,
            session_factory=lambda: session,
            sni_host="btbtlb.com",
        )
        self.client = client

        with self.assertRaises(IndexerSecurityError):
            await client.get("https://www.btbtlb.com/search/demo")
        self.assertEqual(session.calls, [])

    async def test_browser_client_warms_up_once_and_reuses_cookie_session(self):
        session = FakeCurlSession([
            FakeCurlResponse(url="https://www.example.com/"),
            FakeCurlResponse(content=b"first", url="https://www.example.com/search/one"),
            FakeCurlResponse(content=b"second", url="https://www.example.com/search/two"),
        ])
        client = BrowserImpersonatingHttpClient(
            allowed_hosts={"www.example.com"},
            resolver=PUBLIC_DNS,
            session_factory=lambda: session,
            warmup_url="https://www.example.com/",
        )
        self.client = client

        first = await client.get("https://www.example.com/search/one")
        second = await client.get("https://www.example.com/search/two")

        self.assertEqual(first.body, b"first")
        self.assertEqual(second.body, b"second")
        self.assertEqual([call[0] for call in session.calls], [
            "https://www.example.com/",
            "https://www.example.com/search/one",
            "https://www.example.com/search/two",
        ])

    async def test_browser_client_injects_configured_cookies_into_private_session(self):
        session = FakeCurlSession([FakeCurlResponse(url="https://www.example.com/")])
        client = BrowserImpersonatingHttpClient(
            allowed_hosts={"www.example.com"},
            resolver=PUBLIC_DNS,
            session_factory=lambda: session,
            cookies={"cf_clearance": "secret-value"},
        )
        self.client = client

        await client.get("https://www.example.com/")

        self.assertEqual(session.cookies, {"cf_clearance": "secret-value"})
        self.assertNotIn("secret-value", repr(session.calls))

    async def test_browser_client_rejects_off_host_before_session_request(self):
        session = FakeCurlSession([])
        client = BrowserImpersonatingHttpClient(
            allowed_hosts={"www.example.com"},
            resolver=PUBLIC_DNS,
            session_factory=lambda: session,
        )
        self.client = client

        with self.assertRaises(IndexerSecurityError):
            await client.get("https://evil.example/search")
        self.assertEqual(session.calls, [])

    async def test_query_params_are_encoded_by_httpx(self):
        observed = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed["query"] = request.url.params.get("q")
            return httpx.Response(200, content=b"ok")

        client = self.make_client(handler)
        await client.get("https://nyaa.si/", params={"q": "葬送 / test"})
        self.assertEqual(observed["query"], "葬送 / test")


    async def test_pinned_request_uses_validated_ip_with_original_host_and_sni(self):
        observed = {}
        resolution_calls = []

        def resolver(host, port):
            resolution_calls.append((host, port))
            return PUBLIC_DNS(host, port)

        def handler(request: httpx.Request) -> httpx.Response:
            observed["url_host"] = request.url.host
            observed["host_header"] = request.headers.get("host")
            observed["sni_hostname"] = request.extensions.get("sni_hostname")
            return httpx.Response(200, content=b"ok")

        self.client = FixedHostHttpClient(
            allowed_hosts={"nyaa.si"},
            transport=httpx.MockTransport(handler),
            resolver=resolver,
            pin_resolved_address=True,
        )
        response = await self.client.get("https://nyaa.si/path")

        self.assertEqual(resolution_calls, [("nyaa.si", 443)])
        self.assertEqual(observed["url_host"], "93.184.216.34")
        self.assertEqual(observed["host_header"], "nyaa.si")
        self.assertEqual(observed["sni_hostname"], "nyaa.si")
        self.assertEqual(response.url, "https://nyaa.si/path")


if __name__ == "__main__":
    unittest.main()

class FixedHostPostJsonTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_json_preserves_method_body_and_headers(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["body"] = request.content
            seen["authorization"] = request.headers.get("authorization")
            return httpx.Response(200, json={"results": []})

        client = FixedHostHttpClient(
            allowed_hosts={"api.tavily.com"},
            transport=httpx.MockTransport(handler),
            resolver=PUBLIC_DNS,
        )
        try:
            response = await client.post_json(
                "https://api.tavily.com/search",
                json={"query": "MediaFlux"},
                headers={"Authorization": "Bearer secret"},
                max_redirects=0,
            )
        finally:
            await client.aclose()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen["method"], "POST")
        self.assertIn(b'MediaFlux', seen["body"])
        self.assertEqual(seen["authorization"], "Bearer secret")

    async def test_stream_post_json_preserves_security_context_and_bounds_chunks(self):
        seen = {}
        stream = ChunkStream([b"data: one\n\n", b"data: two\n\n"])

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url_host"] = request.url.host
            seen["host_header"] = request.headers.get("host")
            seen["sni_hostname"] = request.extensions.get("sni_hostname")
            seen["accept"] = request.headers.get("accept")
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=stream,
            )

        client = FixedHostHttpClient(
            allowed_hosts={"api.tavily.com"},
            transport=httpx.MockTransport(handler),
            resolver=PUBLIC_DNS,
            pin_resolved_address=True,
            max_response_bytes=64,
        )
        try:
            async with client.stream_post_json(
                "https://api.tavily.com/v1/responses",
                json={"stream": True},
                headers={"Accept": "text/event-stream"},
                max_redirects=0,
            ) as response:
                chunks = [chunk async for chunk in response.aiter_bytes()]
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "text/event-stream")
        finally:
            await client.aclose()

        self.assertEqual(chunks, [b"data: one\n\n", b"data: two\n\n"])
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["url_host"], "93.184.216.34")
        self.assertEqual(seen["host_header"], "api.tavily.com")
        self.assertEqual(seen["sni_hostname"], "api.tavily.com")
        self.assertEqual(seen["accept"], "text/event-stream")
        self.assertTrue(stream.closed)

    async def test_stream_post_json_enforces_cumulative_size_limit(self):
        stream = ChunkStream([b"12345", b"67890", b"x"])
        client = FixedHostHttpClient(
            allowed_hosts={"api.tavily.com"},
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    stream=stream,
                )
            ),
            resolver=PUBLIC_DNS,
            max_response_bytes=10,
        )
        try:
            with self.assertRaises(IndexerResponseTooLarge):
                async with client.stream_post_json(
                    "https://api.tavily.com/v1/responses", json={"stream": True}
                ) as response:
                    _ = [chunk async for chunk in response.aiter_bytes()]
        finally:
            await client.aclose()
        self.assertTrue(stream.closed)

    async def test_post_json_rejects_redirect_and_off_host(self):
        client = FixedHostHttpClient(
            allowed_hosts={"api.tavily.com"},
            transport=httpx.MockTransport(
                lambda request: httpx.Response(307, headers={"Location": "https://evil.example/"})
            ),
            resolver=PUBLIC_DNS,
        )
        try:
            with self.assertRaises(IndexerSecurityError):
                await client.post_json("https://api.tavily.com/search", json={}, max_redirects=0)
            with self.assertRaises(IndexerSecurityError):
                await client.post_json("https://evil.example/search", json={})
        finally:
            await client.aclose()
