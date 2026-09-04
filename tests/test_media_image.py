"""媒体服务器图片代理的上游错误分类与负缓存。"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException
from starlette.requests import Request

from app.modules.image_payload import MAX_IMAGE_BYTES
from app.routes.media_image import media_image


_ITEM_ID = "f" * 32


def _request(query: bytes = b"tag=poster-tag") -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": f"/media-image/jellyfin/{_ITEM_ID}",
        "headers": [],
        "query_string": query,
        "session": {"logged_in": True},
    })


def _config_get(key: str, default: str = "") -> str:
    values = {
        "JELLYFIN_URL": "http://jellyfin.example/",
        "JELLYFIN_API_KEY": "token",
    }
    return values.get(key, default)


class MediaImageProxyTests(unittest.TestCase):
    def test_missing_poster_returns_cacheable_404_without_warning_and_hits_upstream_once(self):
        upstream = Mock(status_code=404)
        with patch.dict(
            "app.routes.media_image._missing_image_cache", {}, clear=True,
        ), patch(
            "app.routes.media_image.config.get", side_effect=_config_get,
        ), patch(
            "app.routes.media_image.requests.get", return_value=upstream,
        ) as request_get, patch(
            "app.routes.media_image.logger.warning",
        ) as warning:
            first = media_image(_request(), "jellyfin", _ITEM_ID)
            second = media_image(_request(), "jellyfin", _ITEM_ID)

        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 404)
        self.assertEqual(first.headers["cache-control"], "private, max-age=30")
        request_get.assert_called_once_with(
            f"http://jellyfin.example/Items/{_ITEM_ID}/Images/Primary",
            headers={"Authorization": 'MediaBrowser Token="token"'},
            params={"maxWidth": 480, "quality": 90},
            timeout=15,
            stream=True,
        )
        upstream.close.assert_called_once_with()
        warning.assert_not_called()

    def test_transport_failure_remains_502_and_is_logged(self):
        with patch(
            "app.routes.media_image.config.get", side_effect=_config_get,
        ), patch(
            "app.routes.media_image.requests.get",
            side_effect=requests.ConnectionError("offline"),
        ), patch(
            "app.routes.media_image.log_throttled",
        ) as throttled:
            with self.assertRaises(HTTPException) as raised:
                media_image(_request(b"tag=another-tag"), "jellyfin", _ITEM_ID)

        self.assertEqual(raised.exception.status_code, 502)
        throttled.assert_called_once()

    def test_successful_image_keeps_long_browser_cache(self):
        upstream = Mock(
            status_code=200,
            headers={"Content-Type": "image/jpeg", "Content-Length": "4"},
        )
        upstream.raise_for_status.return_value = None
        upstream.iter_content.return_value = [b"\xff\xd8\xff\x00"]
        with patch(
            "app.routes.media_image.config.get", side_effect=_config_get,
        ), patch(
            "app.routes.media_image.requests.get", return_value=upstream,
        ):
            response = media_image(_request(b"tag=success-tag"), "jellyfin", _ITEM_ID)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"\xff\xd8\xff\x00")
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.headers["cache-control"], "private, max-age=3600")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        upstream.close.assert_called_once_with()

    def test_declared_oversized_image_is_rejected_before_streaming(self):
        upstream = Mock(
            status_code=200,
            headers={
                "Content-Type": "image/jpeg",
                "Content-Length": str(MAX_IMAGE_BYTES + 1),
            },
        )
        upstream.raise_for_status.return_value = None
        with patch(
            "app.routes.media_image.config.get", side_effect=_config_get,
        ), patch(
            "app.routes.media_image.requests.get", return_value=upstream,
        ):
            with self.assertRaisesRegex(HTTPException, "upstream image too large"):
                media_image(_request(b"tag=too-large"), "jellyfin", _ITEM_ID)

        upstream.iter_content.assert_not_called()
        upstream.close.assert_called_once_with()

    def test_chunked_oversized_image_is_rejected_and_closed(self):
        upstream = Mock(
            status_code=200,
            headers={"Content-Type": "image/jpeg"},
        )
        upstream.raise_for_status.return_value = None
        upstream.iter_content.return_value = [
            b"\xff\xd8\xff",
            b"x" * MAX_IMAGE_BYTES,
        ]
        with patch(
            "app.routes.media_image.config.get", side_effect=_config_get,
        ), patch(
            "app.routes.media_image.requests.get", return_value=upstream,
        ):
            with self.assertRaisesRegex(HTTPException, "upstream image too large"):
                media_image(_request(b"tag=chunk-too-large"), "jellyfin", _ITEM_ID)

        upstream.close.assert_called_once_with()

    def test_exact_maximum_image_is_accepted(self):
        content = b"\xff\xd8\xff" + b"x" * (MAX_IMAGE_BYTES - 3)
        upstream = Mock(
            status_code=200,
            headers={
                "Content-Type": "image/jpeg",
                "Content-Length": str(MAX_IMAGE_BYTES),
            },
        )
        upstream.raise_for_status.return_value = None
        upstream.iter_content.return_value = [content]
        with patch(
            "app.routes.media_image.config.get", side_effect=_config_get,
        ), patch(
            "app.routes.media_image.requests.get", return_value=upstream,
        ):
            response = media_image(
                _request(b"tag=exact-maximum"), "jellyfin", _ITEM_ID
            )

        self.assertEqual(len(response.body), MAX_IMAGE_BYTES)
        upstream.close.assert_called_once_with()

    def test_invalid_mime_or_magic_is_rejected_and_closed(self):
        cases = (
            ({"Content-Type": "text/html"}, [b"<html>"]),
            ({"Content-Type": "image/jpeg"}, [b"not-a-jpeg"]),
        )
        for index, (headers, chunks) in enumerate(cases):
            with self.subTest(index=index):
                upstream = Mock(status_code=200, headers=headers)
                upstream.raise_for_status.return_value = None
                upstream.iter_content.return_value = chunks
                with patch(
                    "app.routes.media_image.config.get", side_effect=_config_get,
                ), patch(
                    "app.routes.media_image.requests.get", return_value=upstream,
                ):
                    with self.assertRaises(HTTPException) as raised:
                        media_image(
                            _request(f"tag=invalid-{index}".encode()),
                            "jellyfin",
                            _ITEM_ID,
                        )

                self.assertEqual(raised.exception.status_code, 502)
                upstream.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
