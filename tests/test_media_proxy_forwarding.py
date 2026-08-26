"""媒体反代可信转发头边界测试。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.modules.media_proxy import _apply_canonical_forwarded_headers
from app.modules.media_proxy_forwarding import (
    media_proxy_forwarding_config,
    normalize_trusted_proxy_cidrs,
)


class MediaProxyForwardingConfigTests(unittest.TestCase):
    def test_cidrs_are_normalized_deduplicated_and_bounded(self):
        self.assertEqual(
            normalize_trusted_proxy_cidrs(
                ["172.18.0.1", "172.18.0.1/32", "2001:db8::1"]
            ),
            ("172.18.0.1/32", "2001:db8::1/128"),
        )
        for invalid in ("*", "0.0.0.0/0", "::/0", "not-an-ip"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_trusted_proxy_cidrs([invalid])

    def test_enabled_runtime_config_fails_closed_without_trusted_source(self):
        with self.assertRaisesRegex(ValueError, "至少填写一个可信代理地址"):
            media_proxy_forwarding_config(
                {
                    "trust_forwarded_headers": 1,
                    "trusted_proxy_cidrs_json": "[]",
                }
            )
        self.assertEqual(media_proxy_forwarding_config({}), (False, ()))


class MediaProxyForwardingMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _canonical_headers(
        self,
        scope_type: str,
        peer: str,
        *,
        trusted_hosts: list[str] | None = None,
        forwarded_for: str = "1.1.1.1, 117.173.84.189",
    ) -> dict[str, str]:
        captured: dict[str, str] = {}

        async def app(scope, _receive, _send):
            request = SimpleNamespace(
                client=SimpleNamespace(host=scope["client"][0]),
                url=SimpleNamespace(scheme=scope["scheme"]),
            )
            captured.update(_apply_canonical_forwarded_headers({}, request))

        middleware = ProxyHeadersMiddleware(
            app,
            trusted_hosts=trusted_hosts or ["172.18.0.1/32"],
        )
        await middleware(
            {
                "type": scope_type,
                "client": (peer, 43123),
                "scheme": "ws" if scope_type == "websocket" else "http",
                "headers": [
                    (b"x-forwarded-for", forwarded_for.encode("ascii")),
                    (b"x-forwarded-proto", b"https"),
                ],
            },
            None,
            None,
        )
        return captured

    async def test_trusted_peer_resolves_right_to_left_for_http_and_websocket(self):
        for scope_type in ("http", "websocket"):
            with self.subTest(scope_type=scope_type):
                headers = await self._canonical_headers(scope_type, "172.18.0.1")
                self.assertEqual(headers["X-Forwarded-For"], "117.173.84.189")
                self.assertEqual(headers["X-Real-IP"], "117.173.84.189")
                self.assertEqual(headers["X-Forwarded-Proto"], "https")

    async def test_multi_hop_chain_skips_all_configured_intermediate_proxies(self):
        headers = await self._canonical_headers(
            "http",
            "172.18.0.1",
            trusted_hosts=["172.18.0.1/32", "10.0.0.4/32"],
            forwarded_for="198.51.100.20, 10.0.0.4",
        )
        self.assertEqual(headers["X-Forwarded-For"], "198.51.100.20")
        self.assertEqual(headers["X-Real-IP"], "198.51.100.20")

    async def test_untrusted_peer_cannot_spoof_forwarded_chain(self):
        headers = await self._canonical_headers("http", "192.168.88.50")
        self.assertEqual(headers["X-Forwarded-For"], "192.168.88.50")
        self.assertEqual(headers["X-Real-IP"], "192.168.88.50")
        self.assertEqual(headers["X-Forwarded-Proto"], "http")
