from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import create_app
from tests.support import isolated_test_database


class ReadinessTests(unittest.TestCase):
    def test_readyz_tracks_application_lifespan_without_changing_healthz(self) -> None:
        app = create_app(start_background=False)
        self.assertFalse(app.state.ready)

        with isolated_test_database("readiness.db"), TestClient(
            app, raise_server_exceptions=False
        ) as client:
            ready = client.get("/readyz")
            health = client.get("/healthz")
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.json()["status"], "ready")
            self.assertEqual(ready.json()["service"], "MediaFlux")
            self.assertTrue(ready.json()["version"])
            self.assertEqual(ready.headers["cache-control"], "no-store")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json(), {"status": "ok"})

            same_host = client.get(
                "/readyz",
                headers={"Origin": "http://testserver:23456"},
            )
            self.assertEqual(
                same_host.headers["access-control-allow-origin"],
                "http://testserver:23456",
            )
            vary = {
                part.strip() for part in same_host.headers["vary"].split(",")
            }
            # SessionMiddleware 是否附加 Cookie 由 Starlette 版本及请求是否触碰
            # session 决定；readyz 自身的跨域契约只要求按 Origin 分离缓存。
            self.assertIn("Origin", vary)
            self.assertTrue(vary.issubset({"Origin", "Cookie"}), vary)
            self.assertNotIn("access-control-allow-credentials", same_host.headers)

            foreign = client.get(
                "/readyz",
                headers={"Origin": "https://attacker.invalid"},
            )
            self.assertNotIn("access-control-allow-origin", foreign.headers)

        self.assertFalse(app.state.ready)


if __name__ == "__main__":
    unittest.main()
