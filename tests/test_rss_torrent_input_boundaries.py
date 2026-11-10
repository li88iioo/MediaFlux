"""RSS 附件回退与种子传输边界：真实持久输入，DNS/HTTP/云盘全部替身。"""
from __future__ import annotations

import json
import socket
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape, quoteattr

import httpx

from tests.support import isolated_test_database
from app import database as db
from app.repositories import rss as rss_repo
from app.modules import offline, rss
from tests.test_guangya_offline_selection import FakeSelectionClient
from tests.test_rss_guangya_torrent_input import INFOHASH, TORRENT, TREE

_HTTP_CLIENT = httpx.Client
_PUBLIC_IP = "93.184.216.34"


class _TorrentBoundaryCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("real network forbidden")))
        self.rules = offline.OfflineRules(
            True, True, False, "target", "测试目录", False, "0", "", (), (), 0, ("mkv",)
        )
        self.enterContext(patch.object(offline.OfflineRules, "from_config", return_value=self.rules))
        self.requests = []
        self.addresses = [_PUBLIC_IP]
        self.lookup = self.enterContext(patch.object(rss.socket, "getaddrinfo", side_effect=self._lookup))
        self.handler = lambda request: httpx.Response(200, content=TORRENT)
        self.factory = self.enterContext(patch.object(rss.httpx, "Client", side_effect=self._http_client))

    def _lookup(self, host, port, **kwargs):
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in self.addresses
        ]

    def _http_client(self, **kwargs):
        self.assertFalse(kwargs["trust_env"])
        self.assertFalse(kwargs["follow_redirects"])
        return _HTTP_CLIENT(transport=httpx.MockTransport(self._handle), **kwargs)

    def _handle(self, request):
        self.requests.append(request)
        return self.handler(request)

    @staticmethod
    def cloud(tree=TREE):
        client = FakeSelectionClient(tree)
        client.create_dir = Mock(return_value="staging")
        client.close = Mock()
        return client

    def invoke(self, mode, url, client, *, rules=None):
        rules = rules or self.rules
        with patch.object(offline.OfflineRules, "from_config", return_value=rules):
            return offline.submit_offline(url, client=client, isolate_task=mode == "isolated")


class RSSTorrentEnclosureBoundaryTests(_TorrentBoundaryCase):
    def setUp(self):
        super().setUp()
        self.enterContext(isolated_test_database())

    def stored_input(self, link, enclosures):
        attachments = "".join(
            f"<enclosure url={quoteattr(url)} type={quoteattr(mime)}/>"
            for url, mime in enclosures
        )
        feed = (
            '<rss version="2.0"><channel><title>Fixture</title>'
            '<link>https://feed.invalid</link><description>Fixture</description>'
            '<item><title>[Group] Example - 01 [1080p]</title><guid>r04</guid>'
            f"<link>{escape(link)}</link>{attachments}</item></channel></rss>"
        ).encode()
        self.handler = lambda request: httpx.Response(
            200, content=feed if request.url.path == "/rss.xml" else TORRENT
        )
        sub = rss_repo.add_rss_subscription(
            name="R04", urls="https://feed.invalid/rss.xml", download_method="guangya",
            gy_target_dir="target",
        )
        engine = rss.RSSEngine()
        self.addCleanup(engine.close)
        self.assertEqual(engine.refresh(sub)["new"], 1)
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT id,payload,title FROM rss_entries WHERE rss_item_id=?", (sub,)
            ).fetchone()
        payload = json.loads(row["payload"])
        item = engine._download_input(row, payload["torrent_url"])
        return engine, int(row["id"]), payload, item

    def download(self, engine, entry_id):
        client = self.cloud()
        normalize = offline.GuangYaClient.normalize_offline_files
        with patch.object(offline, "GuangYaClient", return_value=client) as factory:
            factory.normalize_offline_files = normalize
            result = engine.download(entry_id)
        self.assertTrue(result["ok"], result)
        return client

    def test_cover_enclosure_does_not_hide_torrent_link(self):
        link = "https://feed.invalid/movie.torrent"
        engine, entry_id, payload, item = self.stored_input(
            link, [("https://feed.invalid/cover.jpg", "image/jpeg")]
        )
        self.assertEqual(payload["torrent_url"], link)
        self.assertEqual(item.source_value, link)
        self.assertEqual(item.content_type, "")
        self.assertEqual(self.download(engine, entry_id).torrent_resolve_calls, [TORRENT])

    def test_cover_enclosure_does_not_hide_magnet_link(self):
        link = f"magnet:?xt=urn:btih:{INFOHASH}"
        engine, entry_id, payload, item = self.stored_input(
            link, [("https://feed.invalid/cover.jpg", "image/jpeg")]
        )
        self.assertEqual(payload["torrent_url"], link)
        self.assertEqual(item.content_type, "")
        self.assertEqual(self.download(engine, entry_id).resolve_calls, [link])

    def test_real_http_video_enclosure_keeps_priority_over_bt_link(self):
        video = "http://feed.invalid/Movie.mkv"
        _, _, payload, item = self.stored_input(
            "https://feed.invalid/movie.torrent",
            [("https://feed.invalid/cover.jpg", "image/jpeg"), (video, "video/x-matroska")],
        )
        self.assertEqual(payload["torrent_url"], video)
        self.assertEqual(item.content_type, "video/x-matroska")
        client = self.cloud({})
        result = self.invoke("automatic", item.source_value, client, rules=replace(self.rules, http_enabled=True))
        self.assertTrue(result["ok"], result)
        self.assertEqual(client.legacy_calls[0]["url"], video)
        self.assertEqual(client.torrent_resolve_calls, [])
        self.assertEqual(len(self.requests), 1, "媒体 HTTP 不应触发本地种子读取")

    def test_dynamic_bt_enclosure_keeps_mime_after_persistence(self):
        torrent = "http://feed.invalid/download.php?id=17"
        mime = "Application/X-BitTorrent; charset=binary"
        engine, entry_id, payload, item = self.stored_input(
            "https://feed.invalid/movie.torrent",
            [("https://feed.invalid/cover.jpg", "image/jpeg"),
             ("http://feed.invalid/trailer.mkv", "video/x-matroska"), (torrent, mime)],
        )
        self.assertEqual(payload["torrent_url"], torrent)
        self.assertEqual(item.content_type, payload["content_type"])
        self.assertEqual(item.content_type, mime.lower())  # feedparser 会规范化 MIME 大小写
        client = self.download(engine, entry_id)
        self.assertEqual(client.torrent_resolve_calls, [TORRENT])
        self.assertEqual(client.resolve_calls + client.legacy_calls, [])


class TorrentTransportBoundaryTests(_TorrentBoundaryCase):
    def test_public_http_and_https_torrents_use_pinned_transport_in_all_entries(self):
        for scheme in ("http", "https"):
            for mode in ("automatic", "isolated"):
                with self.subTest(scheme=scheme, mode=mode):
                    client = self.cloud()
                    result = self.invoke(mode, f"{scheme}://feed.invalid/movie.torrent", client)
                    self.assertTrue(result["ok"], result)
                    request = self.requests[-1]
                    port = 80 if scheme == "http" else 443
                    self.lookup.assert_called_with("feed.invalid", port, type=socket.SOCK_STREAM)
                    self.assertEqual(self.lookup.call_count, len(self.requests))
                    self.assertEqual(str(request.url), f"{scheme}://{_PUBLIC_IP}/movie.torrent")
                    self.assertEqual(request.headers["Host"], "feed.invalid")
                    self.assertEqual(request.extensions["sni_hostname"], "feed.invalid")
                    self.assertLessEqual(request.extensions["timeout"]["read"], 20)
                    self.assertEqual(client.torrent_resolve_calls, [TORRENT])
                    self.assertEqual(client.resolve_calls + client.legacy_calls, [])

    def test_public_ip_literal_is_allowed_without_dns(self):
        for scheme in ("http", "https"):
            with self.subTest(scheme=scheme):
                result = self.invoke("automatic", f"{scheme}://{_PUBLIC_IP}/movie.torrent", self.cloud())
                self.assertTrue(result["ok"], result)
        self.lookup.assert_not_called()

    def test_private_literals_and_embedded_credentials_are_rejected(self):
        for scheme in ("http", "https"):
            for host in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "[::1]", "[fc00::1]", "localhost", "public.localhost", "user:fixture-secret@feed.invalid"):
                with self.subTest(scheme=scheme, host=host):
                    result = self.invoke("automatic", f"{scheme}://{host}/movie.torrent", self.cloud())
                    self.assertFalse(result["ok"])
                    self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertEqual(self.requests, [])
        self.lookup.assert_not_called()

    def test_private_or_mixed_dns_is_rejected_before_transport(self):
        for scheme in ("http", "https"):
            for addresses in (["10.0.0.1"], [_PUBLIC_IP, "10.0.0.1"], [_PUBLIC_IP, "::1"]):
                with self.subTest(scheme=scheme, addresses=addresses):
                    self.addresses = addresses
                    self.lookup.reset_mock()
                    result = self.invoke("automatic", f"{scheme}://feed.invalid/movie.torrent", self.cloud())
                    self.assertFalse(result["ok"])
                    self.lookup.assert_called_once()
        self.assertEqual(self.requests, [])

    def test_redirect_rechecks_dns_even_for_same_host(self):
        def handler(request):
            self.addresses = ["127.0.0.1"]
            return httpx.Response(302, headers={"Location": "/redirected.torrent"})
        self.handler = handler
        result = self.invoke("automatic", "http://feed.invalid/movie.torrent", self.cloud())
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.lookup.call_count, 2)

    def test_http_to_https_upgrade_is_pinned_and_does_not_forward_credentials(self):
        def handler(request):
            if len(self.requests) == 1:
                return httpx.Response(302, headers={
                    "Location": "https://other.invalid/final.torrent",
                    "Set-Cookie": "session=fixture-secret; Path=/",
                })
            return httpx.Response(200, content=TORRENT)
        self.handler = handler
        result = self.invoke("automatic", "http://feed.invalid/movie.torrent?passkey=fixture-secret", self.cloud())
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.lookup.call_count, 2)
        first, second = self.requests
        self.assertIn(b"fixture-secret", first.url.query)
        self.assertEqual(str(second.url), f"https://{_PUBLIC_IP}/final.torrent")
        self.assertEqual(second.headers["Host"], "other.invalid")
        self.assertEqual(second.extensions["sni_hostname"], "other.invalid")
        for header in ("Cookie", "Authorization", "Proxy-Authorization", "Referer"):
            self.assertNotIn(header, second.headers)
        self.assertNotIn(b"fixture-secret", second.url.query)

    def test_https_cross_host_redirect_does_not_leak_cookies_on_same_pinned_ip(self):
        self.handler = lambda request: (
            httpx.Response(302, headers={"Location": "https://other.invalid/final.torrent", "Set-Cookie": "session=fixture-secret; Path=/"})
            if len(self.requests) == 1 else httpx.Response(200, content=TORRENT)
        )
        result = self.invoke("automatic", "https://feed.invalid/movie.torrent", self.cloud())
        self.assertTrue(result["ok"], result)
        self.assertNotIn("Cookie", self.requests[-1].headers)

    def test_same_origin_relative_redirect_preserves_local_cookie(self):
        self.handler = lambda request: (
            httpx.Response(302, headers={"Location": "/final.torrent", "Set-Cookie": "session=fixture-session; Path=/"})
            if len(self.requests) == 1 else httpx.Response(200, content=TORRENT)
        )
        result = self.invoke("automatic", "http://feed.invalid/movie.torrent", self.cloud())
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.requests[-1].headers["Host"], "feed.invalid")
        self.assertEqual(self.requests[-1].headers["Cookie"], "session=fixture-session")

    def test_downgrade_private_and_credential_redirects_are_rejected(self):
        for target in ("http://other.invalid/final.torrent", "https://127.0.0.1/private.torrent", "https://user:fixture-secret@other.invalid/final.torrent", "file:///tmp/private.torrent"):
            with self.subTest(target=target):
                self.requests.clear()
                self.handler = lambda request: httpx.Response(302, headers={"Location": target})
                result = self.invoke("automatic", "https://feed.invalid/movie.torrent", self.cloud())
                self.assertFalse(result["ok"])
                self.assertEqual(len(self.requests), 1)
                self.assertNotIn("fixture-secret", json.dumps(result))

    def test_rss_subscription_and_feed_redirect_remain_https_only(self):
        for url in ("http://feed.invalid/rss.xml", "https://feed.invalid:8443/rss.xml"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                rss.validate_rss_source_url(url)
        self.assertEqual(rss.MikanParser().parse("http://feed.invalid/rss.xml"), [])
        self.assertEqual(self.requests, [])
        self.handler = lambda request: httpx.Response(302, headers={"Location": "http://other.invalid/rss.xml"})
        parser = rss.MikanParser()
        self.assertEqual(parser.parse("https://feed.invalid/rss.xml"), [])
        self.assertEqual(parser.last_error_code, "fetch_failed")
        self.assertEqual(len(self.requests), 1)

    def test_ordinary_http_video_still_uses_http_policy_not_torrent_fetch(self):
        for scheme in ("http", "https"):
            for enabled in (False, True):
                for mode in ("automatic", "isolated"):
                    with self.subTest(scheme=scheme, enabled=enabled, mode=mode):
                        client = self.cloud({})
                        result = self.invoke(
                            mode, f"{scheme}://feed.invalid/Movie.mkv", client,
                            rules=replace(self.rules, http_enabled=enabled, magnet_enabled=False),
                        )
                        self.assertEqual(result["ok"], enabled, result)
                        self.assertEqual(client.torrent_resolve_calls, [])
        self.factory.assert_not_called()
        self.lookup.assert_not_called()

    def test_http_torrent_invalid_bytes_fail_closed_and_redacted(self):
        for body in (b"", b"fixture-secret not bencode", b"\xff\xfe", TORRENT[:-2]):
            for mode in ("automatic", "isolated"):
                with self.subTest(body=body, mode=mode):
                    self.handler = lambda request: httpx.Response(200, content=body)
                    client = self.cloud()
                    result = self.invoke(mode, "http://feed.invalid/movie.torrent?passkey=fixture-secret", client)
                    self.assertFalse(result["ok"])
                    self.assertNotIn("fixture-secret", json.dumps(result))
                    self.assertEqual(client.resolve_calls + client.torrent_resolve_calls + client.selection_calls + client.legacy_calls, [])
                    client.create_dir.assert_not_called()
        self.assertEqual(len(self.requests), 8)  # 四类无效载荷 × 直接/隔离两条现行路径

    def test_http_torrent_read_and_redirect_budgets_remain_bounded(self):
        self.handler = lambda request: httpx.Response(200, headers={"Content-Length": str(rss._RSS_MAX_RESPONSE_BYTES + 1)})
        result = self.invoke("automatic", "http://feed.invalid/movie.torrent", self.cloud())
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.requests), 1)
        self.requests.clear()
        self.handler = lambda request: httpx.Response(302, headers={"Location": "/loop.torrent"})
        result = self.invoke("automatic", "http://feed.invalid/movie.torrent", self.cloud())
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.requests), rss._RSS_MAX_REDIRECTS + 1)

    def test_http_upgrade_rechecks_mixed_dns_and_forbids_later_downgrade(self):
        def mixed_dns(request):
            self.addresses = [_PUBLIC_IP, "127.0.0.1"]
            return httpx.Response(302, headers={"Location": "https://other.invalid/final.torrent"})
        self.handler = mixed_dns
        result = self.invoke("automatic", "http://feed.invalid/movie.torrent", self.cloud())
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.lookup.call_count, 2)
        self.addresses = [_PUBLIC_IP]
        self.requests.clear()
        self.handler = lambda request: httpx.Response(302, headers={
            "Location": "https://other.invalid/final.torrent"
            if len(self.requests) == 1 else "http://feed.invalid/downgrade.torrent",
        })
        result = self.invoke("automatic", "http://feed.invalid/movie.torrent", self.cloud())
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.requests), 2)

    def test_http_stream_byte_limit_and_deadline_close_response_without_cloud_writes(self):
        class Stream(httpx.SyncByteStream):
            def __init__(self, chunks):
                self.chunks, self.reads, self.closed = chunks, 0, False
            def __iter__(self):
                for chunk in self.chunks:
                    self.reads += 1
                    yield chunk
            def close(self):
                self.closed = True
        stream = Stream([b"x" * rss._RSS_MAX_RESPONSE_BYTES, b"x", b"not-read"])
        self.handler = lambda request: httpx.Response(200, stream=stream)
        client = self.cloud()
        result = self.invoke("automatic", "http://feed.invalid/movie.torrent", client)
        self.assertFalse(result["ok"])
        self.assertEqual(stream.reads, 2)
        self.assertTrue(stream.closed)
        self.assertEqual(client.torrent_resolve_calls + client.legacy_calls + client.selection_calls, [])
        clock = Mock(return_value=0.0)
        class SlowStream(Stream):
            def __iter__(self):
                clock.return_value = 21.0
                yield TORRENT
        stream = SlowStream([])
        self.handler = lambda request: httpx.Response(200, stream=stream)
        with patch.object(rss.time, "monotonic", clock):
            result = self.invoke("automatic", "http://feed.invalid/movie.torrent", client)
        self.assertFalse(result["ok"])
        self.assertEqual(result["resolve_error_type"], "TimeoutError")
        self.assertTrue(stream.closed)
        self.assertEqual(client.torrent_resolve_calls + client.legacy_calls + client.selection_calls, [])
