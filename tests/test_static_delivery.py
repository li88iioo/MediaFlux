from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import create_app


class StaticDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.client.close()

    def test_text_static_assets_are_cached_and_gzipped(self) -> None:
        response = self.client.get(
            "/static/js/app.js",
            headers={"Accept-Encoding": "gzip"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-encoding"), "gzip")
        self.assertEqual(
            response.headers.get("cache-control"),
            "public, max-age=3600, must-revalidate",
        )
        self.assertEqual(response.headers.get("vary"), "Accept-Encoding")
        self.assertTrue(response.headers.get("etag"))

    def test_static_etag_revalidation_keeps_cache_headers(self) -> None:
        initial = self.client.get(
            "/static/css/main.css",
            headers={"Accept-Encoding": "identity"},
        )
        response = self.client.get(
            "/static/css/main.css",
            headers={
                "Accept-Encoding": "identity",
                "If-None-Match": initial.headers["etag"],
            },
        )

        self.assertEqual(response.status_code, 304)
        self.assertEqual(
            response.headers.get("cache-control"),
            "public, max-age=3600, must-revalidate",
        )
        self.assertEqual(response.headers.get("vary"), "Accept-Encoding")

    def test_static_range_request_is_not_gzipped(self) -> None:
        response = self.client.get(
            "/static/js/app.js",
            headers={"Accept-Encoding": "gzip", "Range": "bytes=0-99"},
        )

        self.assertEqual(response.status_code, 206)
        self.assertIsNone(response.headers.get("content-encoding"))
        self.assertEqual(response.headers.get("content-range", "").split("/")[0], "bytes 0-99")
        self.assertEqual(len(response.content), 100)

    def test_static_middleware_does_not_change_dynamic_responses(self) -> None:
        response = self.client.get("/healthz", headers={"Accept-Encoding": "gzip"})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("content-encoding"))
        self.assertNotEqual(response.headers.get("cache-control"), "public, max-age=3600, must-revalidate")


if __name__ == "__main__":
    unittest.main()
