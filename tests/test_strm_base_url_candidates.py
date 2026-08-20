from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse

from app.modules.network_addresses import (
    build_lan_url_candidates,
    discover_lan_ipv4_addresses,
)
from app.routes import strm_api
from app.routes.api import save_config


class StrmBaseUrlCandidateTests(unittest.TestCase):
    @staticmethod
    def _request():
        return SimpleNamespace(
            session={"logged_in": True},
            app=SimpleNamespace(
                state=SimpleNamespace(
                    background_services_enabled=False,
                    media_proxy_manager=None,
                )
            ),
        )

    @staticmethod
    def _payload(response):
        if isinstance(response, JSONResponse):
            return json.loads(response.body)
        return response

    def test_lan_addresses_keep_only_rfc1918_and_stable_order(self):
        self.assertEqual(
            discover_lan_ipv4_addresses(
                hostname_source=lambda: ("127.0.0.1", "192.168.50.8", "10.0.0.7"),
                route_source=lambda: ("192.168.50.8", "169.254.1.2", "203.0.113.4"),
                proc_source=lambda: (),
            ),
            ["10.0.0.7", "192.168.50.8"],
        )

    def test_candidates_never_guess_container_or_loopback_bind_address(self):
        loopback = build_lan_url_candidates(
            bind_host="127.0.0.1", port=1258, container=False,
            addresses=["192.168.1.9"],
        )
        self.assertEqual(loopback["candidates"], [])
        self.assertIn("lan_binding_disabled", loopback["warnings"])

        container = build_lan_url_candidates(
            bind_host="0.0.0.0", port=1258, container=True,
            addresses=["172.18.0.2", "192.168.1.9"],
        )
        self.assertEqual(container["candidates"], [])
        self.assertIn("container_address_unreliable", container["warnings"])

    def test_candidate_api_returns_bind_and_config_without_mutation(self):
        values = {
            "WEB_HOST": "0.0.0.0",
            "GY_STRM_BASE_URL": "http://mediaflux.home/",
        }
        with patch.object(strm_api, "require_api_login"), patch.object(
            strm_api.config, "get", side_effect=lambda key, default="": values.get(key, default)
        ), patch.object(strm_api.config, "flask_port", return_value=1258), patch(
            "app.modules.network_addresses._is_container", return_value=False
        ), patch(
            "app.modules.network_addresses.discover_lan_ipv4_addresses",
            return_value=["192.168.1.20"],
        ):
            response = strm_api.base_url_candidates(self._request())

        payload = self._payload(response)
        self.assertEqual(payload["configured"], "http://mediaflux.home")
        self.assertEqual(payload["bind"], {"host": "0.0.0.0", "port": 1258})
        self.assertEqual(payload["candidates"][0]["url"], "http://192.168.1.20:1258")

    def test_config_rejects_unspecified_target_and_normalizes_trailing_slash(self):
        with patch("app.routes.api.config.set_and_save") as persist, patch(
            "app.services.clear_dashboard_cache"
        ), patch("app.modules.scheduler.get_scheduler"):
            rejected = save_config(
                self._request(), {"GY_STRM_BASE_URL": "http://0.0.0.0:1258"}
            )
            accepted = save_config(
                self._request(), {"GY_STRM_BASE_URL": "http://192.168.1.20:1258/"}
            )

        self.assertEqual(rejected.status_code, 400)
        self.assertIn("0.0.0.0", self._payload(rejected)["error"])
        self.assertEqual(accepted, {"success": True})
        persist.assert_called_once_with({"GY_STRM_BASE_URL": "http://192.168.1.20:1258"})


if __name__ == "__main__":
    unittest.main()
