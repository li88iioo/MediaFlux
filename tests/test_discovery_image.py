from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.routes.discovery_image import poster


class FakeImageResponse:
    status_code = 200
    headers = {"Content-Type": "image/jpeg", "Content-Length": "4"}

    def __init__(self):
        self.closed = False

    def iter_content(self, chunk_size=65536):
        del chunk_size
        yield b"\xff\xd8\xff\x00"

    def close(self):
        self.closed = True


def _mock_session(side_effect=None, return_value=None):
    """创建替代 _get_poster_session 的 mock Session。"""
    session = MagicMock()
    if side_effect is not None:
        session.get.side_effect = side_effect
    elif return_value is not None:
        session.get.return_value = return_value
    return session


class DiscoveryImageHeaderTests(unittest.TestCase):
    def test_douban_poster_request_sends_movie_referer(self):
        upstream = FakeImageResponse()
        request = SimpleNamespace(session={"logged_in": True})
        session = _mock_session(return_value=upstream)

        with patch(
            "app.routes.discovery_image.decode_poster_token",
            return_value="img1.doubanio.com/view/photo/test.jpg",
        ), patch("app.routes.discovery_image._get_poster_session", return_value=session):
            response = poster(request, "douban", "signed-token")

        self.assertEqual(response.status_code, 200)
        call_kwargs = session.get.call_args
        self.assertEqual(call_kwargs.kwargs["headers"]["Referer"], "https://movie.douban.com/")
        self.assertTrue(upstream.closed)

    def test_non_douban_poster_request_does_not_send_douban_referer(self):
        upstream = FakeImageResponse()
        request = SimpleNamespace(session={"logged_in": True})
        session = _mock_session(return_value=upstream)

        with patch(
            "app.routes.discovery_image.decode_poster_token",
            return_value="poster.jpg",
        ), patch("app.routes.discovery_image._get_poster_session", return_value=session):
            response = poster(request, "tmdb", "signed-token")

        self.assertEqual(response.status_code, 200)
        call_kwargs = session.get.call_args
        self.assertNotIn("Referer", call_kwargs.kwargs["headers"])

    def test_poster_retries_on_transient_error(self):
        fail_resp = FakeImageResponse()
        fail_resp.status_code = 502
        success_resp = FakeImageResponse()
        request = SimpleNamespace(session={"logged_in": True})
        session = _mock_session(side_effect=[fail_resp, success_resp])

        with patch(
            "app.routes.discovery_image.decode_poster_token",
            return_value="img1.doubanio.com/view/photo/test.jpg",
        ), patch("app.routes.discovery_image._get_poster_session", return_value=session):
            response = poster(request, "douban", "signed-token")

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(session.get.call_count, 2)
        self.assertTrue(fail_resp.closed)
        self.assertTrue(success_resp.closed)

    def test_douban_poster_falls_back_to_alternate_cdn_host(self):
        """当原始 CDN 节点 403 时，尝试备用节点。"""
        fail_resp = FakeImageResponse()
        fail_resp.status_code = 403
        fail_resp2 = FakeImageResponse()
        fail_resp2.status_code = 403
        success_resp = FakeImageResponse()
        request = SimpleNamespace(session={"logged_in": True})
        # 原始 URL 两次 403 → 备用节点 200
        session = _mock_session(side_effect=[fail_resp, fail_resp2, success_resp])

        with patch(
            "app.routes.discovery_image.decode_poster_token",
            return_value="img9.doubanio.com/view/photo/poster.jpg",
        ), patch("app.routes.discovery_image._get_poster_session", return_value=session):
            response = poster(request, "douban", "signed-token")

        self.assertEqual(response.status_code, 200)
        # 至少 3 次调用：原始 URL 2 次 + 备用节点 1 次
        self.assertGreaterEqual(session.get.call_count, 3)

    def test_douban_poster_headers_include_sec_fetch(self):
        """豆瓣请求头必须包含 Sec-Fetch-* 系列反爬头。"""
        upstream = FakeImageResponse()
        request = SimpleNamespace(session={"logged_in": True})
        session = _mock_session(return_value=upstream)

        with patch(
            "app.routes.discovery_image.decode_poster_token",
            return_value="img1.doubanio.com/view/photo/test.jpg",
        ), patch("app.routes.discovery_image._get_poster_session", return_value=session):
            poster(request, "douban", "signed-token")

        call_kwargs = session.get.call_args
        headers = call_kwargs.kwargs["headers"]
        self.assertEqual(headers["Sec-Fetch-Dest"], "image")
        self.assertEqual(headers["Sec-Fetch-Mode"], "no-cors")
        self.assertIn("Accept-Language", headers)
        # UA 不得包含 MediaFlux 标识
        self.assertNotIn("MediaFlux", headers["User-Agent"])

    def test_douban_poster_allows_redirects(self):
        """豆瓣 CDN 请求必须跟随 302 重定向。"""
        upstream = FakeImageResponse()
        request = SimpleNamespace(session={"logged_in": True})
        session = _mock_session(return_value=upstream)

        with patch(
            "app.routes.discovery_image.decode_poster_token",
            return_value="img1.doubanio.com/view/photo/test.jpg",
        ), patch("app.routes.discovery_image._get_poster_session", return_value=session):
            poster(request, "douban", "signed-token")

        call_kwargs = session.get.call_args
        self.assertTrue(call_kwargs.kwargs["allow_redirects"])




class DiscoveryImageSessionLifecycleTests(unittest.TestCase):
    def tearDown(self) -> None:
        from app.routes import discovery_image

        discovery_image.close_poster_session()

    def test_close_poster_session_is_idempotent_and_allows_recreation(self) -> None:
        from app.routes import discovery_image

        existing = MagicMock()
        replacement = MagicMock()
        discovery_image._poster_session = existing

        discovery_image.close_poster_session()
        discovery_image.close_poster_session()

        existing.close.assert_called_once_with()
        self.assertIsNone(discovery_image._poster_session)
        with patch("app.routes.discovery_image.requests.Session", return_value=replacement):
            self.assertIs(discovery_image._get_poster_session(), replacement)
        self.assertIs(discovery_image._poster_session, replacement)

    def test_close_failure_still_resets_global_session(self) -> None:
        from app.routes import discovery_image

        existing = MagicMock()
        existing.close.side_effect = RuntimeError("close failed")
        discovery_image._poster_session = existing

        discovery_image.close_poster_session()

        self.assertIsNone(discovery_image._poster_session)


if __name__ == "__main__":
    unittest.main()
