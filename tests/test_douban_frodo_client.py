from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import traceback
import unittest
from datetime import datetime
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

import requests

from app.clients.douban_frodo import DoubanFrodoClient
from app.discovery.models import (
    ProviderAuthenticationError,
    ProviderInvalidResponse,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None, chunks=None):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, bytes):
            body = payload
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = b""
        self.payload = payload
        self.body = body
        self.status_code = status_code
        self.headers = dict(headers or {})
        if isinstance(payload, (dict, list)):
            self.headers.setdefault("Content-Type", "application/json; charset=utf-8")
        self.chunks = list(chunks) if chunks is not None else [body]
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def iter_content(self, chunk_size=65536):
        del chunk_size
        if isinstance(self.payload, Exception):
            raise self.payload
        yield from self.chunks

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected GET {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class CapturingSession(requests.Session):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload
        self.prepared_requests = []

    def send(self, request, **kwargs):
        del kwargs
        self.prepared_requests.append(request)
        response = requests.Response()
        response.status_code = 200
        response.headers = {"Content-Type": "application/json; charset=utf-8"}
        response.url = request.url
        response.request = request
        response._content = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        response._content_consumed = True
        return response


def frodo_subject(subject_id="1292052", subject_type="movie", **overrides):
    values = {
        "id": subject_id,
        "type": subject_type,
        "title": "测试条目",
        "original_title": "Test Subject",
        "card_subtitle": "2026 / 测试地区 / 剧情",
        "intro": "测试简介",
        "rating": {"value": 8.6},
        "pic": {"large": "https://img1.doubanio.com/view/photo/l/public/test.webp"},
        "release_date": "2026-07-25",
    }
    values.update(overrides)
    return values


PUBLIC_LIST_FIELDS = {
    "id", "media_type", "title", "original_title", "year", "overview",
    "poster_url", "rating", "release_date", "is_new", "episodes_info",
}
PUBLIC_DETAIL_FIELDS = PUBLIC_LIST_FIELDS - {"is_new", "episodes_info"}


class DoubanFrodoClientTests(unittest.TestCase):
    NOW = datetime(2026, 7, 25, 12, 0).timestamp()
    API_KEY = "test-key-not-real"
    API_SECRET = "test-secret-not-real"

    def make_client(self, *responses, **kwargs):
        session = FakeSession(*responses)
        client = DoubanFrodoClient(
            api_key=self.API_KEY,
            api_secret=self.API_SECRET,
            session=session,
            clock=lambda: self.NOW,
            timeout=(2.0, 6.0),
            page_size=2,
            **kwargs,
        )
        return client, session

    def test_omitted_credentials_use_server_only_compatibility_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOUBAN_FRODO_API_KEY", None)
            os.environ.pop("DOUBAN_FRODO_API_SECRET", None)
            client = DoubanFrodoClient(session=FakeSession())

        self.assertTrue(client.configured)
        self.assertEqual(client.credential_source, "compatibility_default")
        self.assertTrue(client.api_key)
        self.assertTrue(client.api_secret)
        self.assertNotEqual(client.api_key, client.api_secret)

    def test_environment_can_override_or_blank_compatibility_defaults(self):
        with patch.dict(
            os.environ,
            {
                "DOUBAN_FRODO_API_KEY": "rotated-key",
                "DOUBAN_FRODO_API_SECRET": "rotated-secret",
            },
        ):
            overridden = DoubanFrodoClient(session=FakeSession())
        self.assertEqual((overridden.api_key, overridden.api_secret), (
            "rotated-key", "rotated-secret",
        ))
        self.assertEqual(overridden.credential_source, "environment")

        with patch.dict(
            os.environ,
            {"DOUBAN_FRODO_API_KEY": "", "DOUBAN_FRODO_API_SECRET": ""},
        ):
            disabled = DoubanFrodoClient(session=FakeSession())
        self.assertFalse(disabled.configured)
        self.assertEqual(disabled.credential_source, "environment")

    def test_partial_environment_credentials_never_mix_with_compatibility_defaults(self):
        cases = (
            ("DOUBAN_FRODO_API_KEY", "only-key"),
            ("DOUBAN_FRODO_API_SECRET", "only-secret"),
        )
        for name, value in cases:
            with self.subTest(name=name):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("DOUBAN_FRODO_API_KEY", None)
                    os.environ.pop("DOUBAN_FRODO_API_SECRET", None)
                    os.environ[name] = value
                    client = DoubanFrodoClient(session=FakeSession())

                self.assertFalse(client.configured)
                self.assertEqual(client.credential_source, "environment")
                self.assertIn("", (client.api_key, client.api_secret))

    def test_explicit_credentials_report_safe_source(self):
        client, _session = self.make_client()

        self.assertEqual(client.credential_source, "explicit")

    def test_auth_identity_includes_user_agent(self):
        default_client, _session = self.make_client()
        custom_client, _session = self.make_client(
            user_agent="MediaFlux Frodo-Compatible Gateway/2.0"
        )

        self.assertNotEqual(
            default_client._credential_identity, custom_client._credential_identity
        )

    def test_compatibility_credentials_never_appear_in_failure_logs(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOUBAN_FRODO_API_KEY", None)
            os.environ.pop("DOUBAN_FRODO_API_SECRET", None)
            client = DoubanFrodoClient(
                session=FakeSession(requests.ConnectionError("offline")),
                clock=lambda: self.NOW,
            )

        with self.assertLogs("app.clients.douban_frodo", level="WARNING") as captured:
            with self.assertRaises(ProviderUnavailable) as raised:
                client.list_items("movie_hot", "movie", 1, {})

        rendered = "\n".join(captured.output + [str(raised.exception), repr(raised.exception)])
        self.assertNotIn(client.api_key, rendered)
        self.assertNotIn(client.api_secret, rendered)

    def test_signed_request_uses_exact_protocol_fixed_url_and_compatible_user_agent(self):
        client, session = self.make_client(FakeResponse({"items": [], "total": 0}))

        client.list_items("recommend", "movie", 1, {})

        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://frodo.douban.com/api/v2/movie/recommend")
        params = kwargs["params"]
        self.assertEqual(params["apiKey"], self.API_KEY)
        self.assertNotIn("apikey", params)
        self.assertEqual(params["_ts"], "20260725")
        raw = f"GET&{quote('/api/v2/movie/recommend', safe='')}&20260725"
        expected = base64.b64encode(
            hmac.new(
                self.API_SECRET.encode("utf-8"),
                raw.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("ascii")
        self.assertEqual(params["_sig"], expected)
        self.assertEqual(kwargs["timeout"], (2.0, 6.0))
        self.assertFalse(kwargs["allow_redirects"])
        user_agent = kwargs["headers"]["User-Agent"]
        self.assertIn("Rexxar-Core", user_agent)
        self.assertIn("com.douban.frodo", user_agent)
        self.assertIn("model/unknown", user_agent)
        self.assertNotIn("device_id", user_agent)

    def test_actual_session_credentials_are_removed_without_dropping_frodo_signature(self):
        session = CapturingSession({"items": [], "total": 0})
        client = DoubanFrodoClient(
            api_key=self.API_KEY,
            api_secret=self.API_SECRET,
            session=session,
            clock=lambda: self.NOW,
        )
        session.headers["Authorization"] = "Bearer session-secret"
        session.headers["Cookie"] = "manual=session-secret"
        session.headers["Proxy-Authorization"] = "Basic proxy-secret"
        session.auth = ("session-user", "session-password")
        session.cookies.set("sid", "cookie-secret", domain="frodo.douban.com", path="/")
        session.trust_env = True

        with patch(
            "requests.sessions.get_netrc_auth",
            return_value=("netrc-user", "netrc-secret"),
        ) as netrc:
            client.list_items("recommend", "movie", 1, {})

        prepared = session.prepared_requests[0]
        lowered = {key.lower(): value for key, value in prepared.headers.items()}
        query = parse_qs(urlsplit(prepared.url).query)
        self.assertNotIn("authorization", lowered)
        self.assertNotIn("proxy-authorization", lowered)
        self.assertNotIn("cookie", lowered)
        self.assertIsNone(session.auth)
        self.assertFalse(session.trust_env)
        netrc.assert_not_called()
        self.assertEqual(query["apiKey"], [self.API_KEY])
        self.assertEqual(query["_ts"], ["20260725"])
        self.assertTrue(query["_sig"][0])

    def test_optional_user_agent_is_used_and_requires_visible_ascii(self):
        custom_user_agent = "MediaFlux Frodo-Compatible Gateway/2.0"
        client, session = self.make_client(
            FakeResponse({"items": [], "total": 0}),
            user_agent=custom_user_agent,
        )

        client.list_items("recommend", "movie", 1, {})

        self.assertEqual(session.calls[0][1]["headers"]["User-Agent"], custom_user_agent)
        for invalid in (
            "",
            "short",
            "x" * 257,
            "valid-looking\r\nInjected: true",
            "MediaFlux 豆瓣 Frodo Client/1.0",
            "MediaFlux Frödo Client/1.0",
            "MediaFlux\tFrodo Client/1.0",
            "MediaFlux\x00Frodo Client/1.0",
            "MediaFlux\x7fFrodo Client/1.0",
        ):
            with self.subTest(user_agent=repr(invalid)):
                with self.assertRaises(ValueError):
                    DoubanFrodoClient(
                        api_key=self.API_KEY,
                        api_secret=self.API_SECRET,
                        session=FakeSession(),
                        user_agent=invalid,
                    )

    def test_collection_and_recommend_paths_are_allowlisted_with_exact_paging(self):
        client, session = self.make_client(
            FakeResponse({"subject_collection_items": [], "total": 0}),
            FakeResponse({"items": [], "total": 5}),
        )

        client.list_items("movie_hot", "movie", 1, {})
        page = client.list_items(
            "recommend",
            "tv",
            2,
            {
                "sort": "U",
                "genres": "剧情,喜剧",
                "tags": "悬疑",
                "year_range": "2020,2026",
                "countries": "中国大陆",
                "apiKey": "attacker-value",
                "unknown": "drop-me",
            },
        )

        self.assertEqual(
            session.calls[0][0],
            "https://frodo.douban.com/api/v2/subject_collection/movie_hot_gaia/items",
        )
        self.assertEqual(session.calls[1][0], "https://frodo.douban.com/api/v2/tv/recommend")
        params = session.calls[1][1]["params"]
        self.assertEqual(params["start"], 2)
        self.assertEqual(params["count"], 2)
        self.assertEqual(params["sort"], "U")
        self.assertEqual(params["genres"], "剧情,喜剧")
        self.assertEqual(params["tags"], "悬疑")
        self.assertEqual(params["year_range"], "2020,2026")
        self.assertEqual(params["countries"], "中国大陆")
        self.assertEqual(params["apiKey"], self.API_KEY)
        self.assertNotIn("unknown", params)
        self.assertTrue(page.has_more)

    def test_list_payload_variants_are_normalized_to_immutable_public_page(self):
        nested = {"subject": frodo_subject("2", "tv", title="嵌套剧集")}
        client, _ = self.make_client(
            FakeResponse(
                {
                    "subject_collection_items": [
                        frodo_subject("1", "movie"),
                        nested,
                        {"title": "缺少 ID"},
                        "invalid",
                    ],
                    "total": 4,
                }
            )
        )

        page = client.list_items("movie_showing", "movie", 1, {})

        self.assertIsInstance(page.items, tuple)
        self.assertEqual(page.source, "frodo")
        self.assertEqual(len(page.items), 2)
        self.assertEqual(
            page.items[0],
            {
                "id": "1",
                "media_type": "movie",
                "title": "测试条目",
                "original_title": "Test Subject",
                "year": "2026",
                "overview": "测试简介",
                "poster_url": "https://img1.doubanio.com/view/photo/l/public/test.webp",
                "rating": 8.6,
                "release_date": "2026-07-25",
                "is_new": False,
                "episodes_info": "",
            },
        )
        self.assertEqual(page.items[1]["id"], "2")
        self.assertEqual(page.items[1]["media_type"], "tv")
        self.assertFalse(page.has_more)

    def test_weekly_categories_use_exact_upstream_collection_names(self):
        client, session = self.make_client(
            FakeResponse({"subject_collection_items": [], "total": 0}),
            FakeResponse({"subject_collection_items": [], "total": 0}),
        )

        client.list_items("tv_chinese_weekly", "tv", 1, {})
        client.list_items("tv_global_weekly", "tv", 1, {})

        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://frodo.douban.com/api/v2/subject_collection/"
                "tv_chinese_best_weekly/items",
                "https://frodo.douban.com/api/v2/subject_collection/"
                "tv_global_best_weekly/items",
            ],
        )

    def test_upstream_collection_names_are_not_accepted_as_public_categories(self):
        client, session = self.make_client()

        for category in ("tv_chinese_best_weekly", "tv_global_best_weekly"):
            with self.subTest(category=category), self.assertRaises(ProviderInvalidResponse):
                client.list_items(category, "tv", 1, {})

        self.assertEqual(session.calls, [])

    def test_items_payload_and_detail_use_the_same_normalized_shape(self):
        client, session = self.make_client(
            FakeResponse({"items": [frodo_subject("3")], "count": 1}),
            FakeResponse(
                frodo_subject(
                    "4",
                    "tv",
                    rating={"score": "9.1"},
                    pic={"normal": "https://img2.doubanio.com/poster.webp"},
                    release_date="",
                    year="2024",
                )
            ),
        )

        page = client.list_items("recommend", "movie", 1, {})
        detail = client.get_detail("4", "tv")

        self.assertEqual(page.items[0]["id"], "3")
        self.assertEqual(detail["id"], "4")
        self.assertEqual(detail["media_type"], "tv")
        self.assertEqual(detail["rating"], 9.1)
        self.assertEqual(detail["poster_url"], "https://img2.doubanio.com/poster.webp")
        self.assertEqual(detail["year"], "2024")
        self.assertEqual(session.calls[1][0], "https://frodo.douban.com/api/v2/tv/4")

    def test_list_and_detail_field_sets_match_public_client_schema(self):
        client, _ = self.make_client(
            FakeResponse({"items": [frodo_subject("8", is_new=True, episodes_info="全 12 集")]}),
            FakeResponse(frodo_subject("8")),
        )

        page = client.list_items("recommend", "tv", 1, {})
        detail = client.get_detail("8", "tv")

        self.assertEqual(set(page.items[0]), PUBLIC_LIST_FIELDS)
        self.assertEqual(set(detail), PUBLIC_DETAIL_FIELDS)
        self.assertTrue(page.items[0]["is_new"])
        self.assertEqual(page.items[0]["episodes_info"], "全 12 集")

    def test_has_more_uses_raw_upstream_slots_when_total_exists(self):
        client, _ = self.make_client(FakeResponse({
            "items": [frodo_subject("1"), {"title": "missing id"}],
            "total": 2,
        }))

        page = client.list_items("recommend", "movie", 1, {})

        self.assertEqual([item["id"] for item in page.items], ["1"])
        self.assertFalse(page.has_more)

    def test_missing_credentials_are_unconfigured_and_never_touch_network(self):
        session = FakeSession()
        client = DoubanFrodoClient(api_key="", api_secret="", session=session)

        self.assertFalse(client.configured)
        with self.assertRaises(ProviderNotConfigured):
            client.list_items("movie_hot", "movie", 1, {})
        with self.assertRaises(ProviderNotConfigured):
            client.get_detail("1", "movie")
        self.assertEqual(session.calls, [])

    def test_partial_credentials_fail_before_network(self):
        for api_key, api_secret in ((self.API_KEY, ""), ("", self.API_SECRET)):
            with self.subTest(api_key=bool(api_key), api_secret=bool(api_secret)):
                session = FakeSession()
                client = DoubanFrodoClient(
                    api_key=api_key,
                    api_secret=api_secret,
                    session=session,
                )
                self.assertFalse(client.configured)
                with self.assertRaises(ProviderNotConfigured) as raised:
                    client.list_items("movie_hot", "movie", 1, {})
                self.assertEqual(session.calls, [])
                self.assertNotIn(self.API_KEY, str(raised.exception))
                self.assertNotIn(self.API_SECRET, str(raised.exception))

    def test_complete_credentials_report_configured(self):
        client, _ = self.make_client()
        self.assertTrue(client.configured)

    def test_invalid_category_id_and_timeout_fail_before_network(self):
        client, session = self.make_client()
        with self.assertRaises(ProviderInvalidResponse):
            client.list_items("https://attacker.invalid/path", "movie", 1, {})
        with self.assertRaises(ProviderInvalidResponse):
            client.get_detail("../other", "movie")
        self.assertEqual(session.calls, [])

        for invalid_timeout in (0, -1, math.inf, (2, 0), (2, math.inf), None):
            with self.subTest(timeout=invalid_timeout):
                with self.assertRaises(ValueError):
                    DoubanFrodoClient(
                        api_key=self.API_KEY,
                        api_secret=self.API_SECRET,
                        session=FakeSession(),
                        timeout=invalid_timeout,
                    )

    def test_authentication_failure_is_debug_only_for_provider_deduplication(self):
        client, _session = self.make_client(FakeResponse({}, status_code=401))
        client.credential_source = client.api_secret

        with self.assertLogs("app.clients.douban_frodo", level="DEBUG") as captured:
            with self.assertRaises(ProviderAuthenticationError):
                client.list_items("movie_hot", "movie", 1, {})

        rendered = "\n".join(captured.output)
        self.assertIn("error=authentication", rendered)
        self.assertIn("credential_source=unknown", rendered)
        self.assertNotIn(client.api_secret, rendered)
        self.assertFalse(any("WARNING" in line for line in captured.output))

    def test_transport_and_payload_failures_use_structured_errors(self):
        cases = (
            (FakeResponse({}, status_code=401), ProviderAuthenticationError),
            (FakeResponse({}, status_code=429, headers={"Retry-After": "19"}), ProviderRateLimited),
            (FakeResponse({}, status_code=503), ProviderUnavailable),
            (FakeResponse({}, status_code=302, headers={"Location": "https://attacker.invalid/"}), ProviderUnavailable),
            (requests.Timeout("slow"), ProviderTimeout),
            (requests.ConnectionError("offline"), ProviderUnavailable),
            (FakeResponse(b"not json", headers={"Content-Type": "application/json"}), ProviderInvalidResponse),
            (FakeResponse([]), ProviderInvalidResponse),
            (FakeResponse({"unexpected": []}), ProviderInvalidResponse),
        )
        for response, error_type in cases:
            with self.subTest(error=error_type.__name__, response=type(response).__name__):
                client, _ = self.make_client(response)
                with self.assertRaises(error_type) as raised:
                    client.list_items("movie_hot", "movie", 1, {})
                if error_type is ProviderRateLimited:
                    self.assertEqual(raised.exception.retry_after, 19)

    def test_json_content_type_and_streamed_response_size_are_enforced(self):
        oversized = b'{"items":[' + (b" " * 80) + b"]}"
        cases = (
            FakeResponse(b'{"items": []}', headers={}),
            FakeResponse(b'{"items": []}', headers={"Content-Type": "text/html"}),
            FakeResponse(
                oversized,
                headers={"Content-Type": "application/json"},
                chunks=[oversized[:32], oversized[32:]],
            ),
            FakeResponse(
                b'{"items": []}',
                headers={"Content-Type": "application/json", "Content-Length": "999"},
            ),
        )
        for response in cases:
            with self.subTest(headers=response.headers):
                client, _ = self.make_client(response, max_response_bytes=64)
                with self.assertRaises(ProviderInvalidResponse):
                    client.list_items("recommend", "movie", 1, {})
                self.assertTrue(response.closed)

    def test_sensitive_recursive_exception_chain_and_traceback_are_removed(self):
        leaked_signature = "signature-must-not-leak"
        leaked_query = (
            f"https://frodo.douban.com/api/v2/movie/recommend?"
            f"apiKey={self.API_KEY}&secret={self.API_SECRET}&_sig={leaked_signature}"
        )
        root = ValueError(leaked_query)
        middle = requests.ConnectionError(leaked_query)
        middle.__cause__ = root
        client, _ = self.make_client(middle)

        with self.assertRaises(ProviderUnavailable) as raised:
            client.list_items("recommend", "movie", 1, {})

        rendered = "".join(traceback.format_exception(raised.exception))
        current = raised.exception
        visited = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            rendered += f"\n{current!s}\n{current!r}"
            current = current.__cause__ or current.__context__
        for forbidden in (
            self.API_KEY,
            self.API_SECRET,
            leaked_signature,
            "apiKey=",
            "secret=",
            "_sig=",
            "frodo.douban.com",
            "/api/v2/movie/recommend?",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(len(visited), 1)

    def test_credentials_and_signatures_are_redacted_from_logs_and_errors(self):
        leaked_signature = "signature-must-not-leak"
        error = requests.ConnectionError(
            f"failed apiKey={self.API_KEY}&secret={self.API_SECRET}&_sig={leaked_signature}"
        )
        client, _ = self.make_client(error)

        with self.assertLogs("app.clients.douban_frodo", level="WARNING") as captured:
            with self.assertRaises(ProviderUnavailable) as raised:
                client.list_items("movie_hot", "movie", 1, {})

        output = "\n".join(captured.output + [str(raised.exception), raised.exception.safe_message])
        for secret in (self.API_KEY, self.API_SECRET, leaked_signature):
            self.assertNotIn(secret, output)
        self.assertNotIn("apiKey=", output)
        self.assertNotIn("_sig=", output)


if __name__ == "__main__":
    unittest.main()
