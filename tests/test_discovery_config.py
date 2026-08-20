from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse

from app.routes.api import get_config, save_config


class DiscoveryConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = SimpleNamespace(
            session={"logged_in": True},
            app=SimpleNamespace(
                state=SimpleNamespace(background_services_enabled=False)
            ),
        )

    @staticmethod
    def _payload(response):
        if isinstance(response, JSONResponse):
            return json.loads(response.body)
        return response

    def _save(self, data, *, existing=None, return_shutdown=False):
        existing = existing or {}

        def get_value(key, default=""):
            return existing.get(key, default)

        with patch("app.routes.api.config.get", side_effect=get_value), patch(
            "app.routes.api.config.set_and_save"
        ) as persist, patch("app.services.clear_dashboard_cache"), patch(
            "app.modules.scheduler.get_scheduler"
        ), patch("app.discovery.service.shutdown_discovery_service") as shutdown:
            response = save_config(self.request, data)
        if return_shutdown:
            return response, persist, shutdown
        return response, persist

    def test_accepts_all_discovery_configuration_keys_and_normalizes_dbcl2(self):
        values = {
            "DISCOVERY_ENABLED": "1",
            "DISCOVERY_CACHE_TTL_SECONDS": "3600",
            "DISCOVERY_STALE_TTL_SECONDS": "604800",
            "DISCOVERY_DOUBAN_ENABLED": "1",
            "ORGANIZE_DOUBAN_HINTS_ENABLED": "1",
            "ORGANIZE_BANGUMI_HINTS_ENABLED": "1",
            "DOUBAN_DBCL2": 'bid=abc; dbcl2="123456789:test-dbcl2-value"; ck=xyz',
            "DOUBAN_CACHE_TTL_SECONDS": "21600",
            "BANGUMI_USER_AGENT": "MediaFlux/1.0 (contact: admin@example.invalid)",
        }

        response, persist = self._save(values)

        self.assertEqual(response, {"success": True})
        expected = dict(values)
        expected["DOUBAN_DBCL2"] = "123456789:test-dbcl2-value"
        persist.assert_called_once_with(expected)

    def test_discovery_runtime_config_save_shuts_down_cached_service(self):
        values = {
            "DISCOVERY_ENABLED": "0",
            "DISCOVERY_CACHE_TTL_SECONDS": "3600",
            "DISCOVERY_STALE_TTL_SECONDS": "604800",
            "DISCOVERY_DOUBAN_ENABLED": "0",
            "ORGANIZE_DOUBAN_HINTS_ENABLED": "0",
            "ORGANIZE_BANGUMI_HINTS_ENABLED": "0",
            "DOUBAN_DBCL2": "123456789:test-dbcl2-value",
            "DOUBAN_CACHE_TTL_SECONDS": "21600",
            "BANGUMI_USER_AGENT": "MediaFlux/1.0",
            "TMDB_API_KEY": "tmdb-key",
            "TMDB_API_URL": "https://api.themoviedb.org/3",
            "TMDB_MATCH_MODE": "strict",
            "PROXY_URL": "http://127.0.0.1:7890",
        }
        for key, value in values.items():
            with self.subTest(key=key):
                response, persist, shutdown = self._save(
                    {key: value}, return_shutdown=True
                )
                self.assertEqual(response, {"success": True})
                persist.assert_called_once()
                shutdown.assert_called_once_with()

    def test_rejects_removed_tmdb_preview_confirm_key(self):
        response, persist = self._save({"TMDB_PREVIEW_CONFIRM": "1"})

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 400)
        persist.assert_not_called()

    def test_indexer_config_save_refreshes_web_and_telegram_services(self):
        with patch("app.routes.api.config.get", return_value=""), patch(
            "app.routes.api.config.set_and_save"
        ) as persist, patch("app.services.clear_dashboard_cache"), patch(
            "app.modules.scheduler.get_scheduler"
        ), patch(
            "app.indexers.runtime.shutdown_indexer_service"
        ) as shutdown_web, patch(
            "app.modules.telegram_resource_search.shutdown_telegram_indexer_worker",
            return_value=True,
        ) as shutdown_telegram:
            response = save_config(
                self.request,
                {"INDEXER_ENABLED_SITES": "nyaa,mikan"},
            )

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({
            "INDEXER_ENABLED_SITES": "nyaa,mikan",
            "INDEXER_SUKEBEI_ENABLED": "0",
        })
        shutdown_web.assert_awaited_once()
        shutdown_telegram.assert_called_once_with(timeout=5.0)

    def test_discovery_runtime_config_save_also_rebuilds_search_clients(self):
        with patch("app.routes.api.config.get", return_value=""), patch(
            "app.routes.api.config.set_and_save"
        ), patch("app.services.clear_dashboard_cache"), patch(
            "app.modules.scheduler.get_scheduler"
        ), patch("app.discovery.service.shutdown_discovery_service"), patch(
            "app.discovery.search.shutdown_discovery_search_service"
        ) as shutdown_search:
            response = save_config(
                self.request,
                {"TMDB_API_KEY": "new-key", "BANGUMI_USER_AGENT": "MediaFlux/2.0"},
            )

        self.assertEqual(response, {"success": True})
        shutdown_search.assert_called_once_with()

    def test_unrelated_config_save_does_not_shutdown_discovery_service(self):
        response, persist, shutdown = self._save(
            {"RSS_QB_CATEGORY": "mediaflux"}, return_shutdown=True
        )

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({"RSS_QB_CATEGORY": "mediaflux"})
        shutdown.assert_not_called()

    def test_unknown_configuration_key_remains_rejected(self):
        response, persist = self._save({"DISCOVERY_UNKNOWN_OPTION": "1"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("不允许", self._payload(response)["error"])
        persist.assert_not_called()

    def test_config_api_masks_dbcl2_and_omits_all_frodo_credentials(self):
        with patch(
            "app.routes.api.config.all_items",
            return_value={
                "DOUBAN_FRODO_API_KEY": "server-key-must-not-return",
                "DOUBAN_FRODO_API_SECRET": "server-secret-must-not-return",
                "DOUBAN_DBCL2": "123456789:test-dbcl2-value",
                "BANGUMI_USER_AGENT": "MediaFlux/1.0",
            },
        ):
            response = get_config(self.request)

        self.assertNotIn("DOUBAN_FRODO_API_KEY", response)
        self.assertNotIn("DOUBAN_FRODO_API_SECRET", response)
        self.assertEqual(response["DOUBAN_DBCL2"], "********")
        self.assertEqual(response["BANGUMI_USER_AGENT"], "MediaFlux/1.0")

    def test_masked_dbcl2_preserves_existing_value(self):
        response, persist = self._save(
            {"DISCOVERY_DOUBAN_ENABLED": "1", "DOUBAN_DBCL2": "  ********  "}
        )

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({"DISCOVERY_DOUBAN_ENABLED": "1"})

    def test_frodo_credentials_are_no_longer_accepted_by_config_api(self):
        for key in ("DOUBAN_FRODO_API_KEY", "DOUBAN_FRODO_API_SECRET"):
            with self.subTest(key=key):
                response, persist = self._save({key: "must-stay-server-only"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("不允许", self._payload(response)["error"])
                persist.assert_not_called()

    def test_invalid_dbcl2_is_rejected_without_persisting_cookie_fragments(self):
        for value in (
            "bid=abc; ck=xyz",
            "dbcl2=value\r\nX-Test: injected",
            "x" * 513,
        ):
            with self.subTest(value=value[:40]):
                response, persist = self._save({"DOUBAN_DBCL2": value})
                self.assertEqual(response.status_code, 400)
                self.assertIn("DOUBAN_DBCL2", self._payload(response)["error"])
                persist.assert_not_called()

    def test_invalid_discovery_boolean_is_rejected(self):
        for key in (
            "DISCOVERY_ENABLED",
            "DISCOVERY_DOUBAN_ENABLED",
            "DISCOVERY_RESOURCE_RESULTS_ENABLED",
        ):
            with self.subTest(key=key):
                response, persist = self._save({key: "sometimes"})
                self.assertEqual(response.status_code, 400)
                self.assertIn(key, self._payload(response)["error"])
                persist.assert_not_called()

    def test_resource_results_switch_is_validated_and_persisted(self):
        response, persist = self._save(
            {"DISCOVERY_RESOURCE_RESULTS_ENABLED": "false"}
        )

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with(
            {"DISCOVERY_RESOURCE_RESULTS_ENABLED": "0"}
        )

    def test_invalid_or_out_of_range_ttls_are_rejected(self):
        invalid_values = {
            "DISCOVERY_CACHE_TTL_SECONDS": ("not-a-number", "59", "604801"),
            "DISCOVERY_STALE_TTL_SECONDS": ("not-a-number", "299", "2592001"),
            "DOUBAN_CACHE_TTL_SECONDS": ("not-a-number", "299", "604801"),
        }
        for key, values in invalid_values.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    response, persist = self._save({key: value})
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(key, self._payload(response)["error"])
                    persist.assert_not_called()

    def test_stale_ttl_cannot_be_shorter_than_fresh_cache_ttl(self):
        response, persist = self._save(
            {
                "DISCOVERY_CACHE_TTL_SECONDS": "7200",
                "DISCOVERY_STALE_TTL_SECONDS": "3600",
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("DISCOVERY_STALE_TTL_SECONDS", self._payload(response)["error"])
        persist.assert_not_called()

    def test_public_douban_config_save_restarts_discovery_service(self):
        response, persist, shutdown = self._save(
            {"DISCOVERY_DOUBAN_ENABLED": "1", "DOUBAN_DBCL2": ""},
            return_shutdown=True,
        )

        self.assertEqual(response, {"success": True})
        # 未变化的空 Cookie 不再触发磁盘写入；只持久化真实变化。
        persist.assert_called_once_with({"DISCOVERY_DOUBAN_ENABLED": "1"})
        shutdown.assert_called_once_with()

    def test_bangumi_user_agent_rejects_blank_or_header_injection(self):
        for value in ("   ", "MediaFlux/1.0\r\nX-Test: injected"):
            with self.subTest(value=value):
                response, persist = self._save({"BANGUMI_USER_AGENT": value})
                self.assertEqual(response.status_code, 400)
                self.assertIn("BANGUMI_USER_AGENT", self._payload(response)["error"])
                persist.assert_not_called()

    def test_bangumi_user_agent_accepts_256_characters(self):
        user_agent = "M" * 256

        response, persist = self._save({"BANGUMI_USER_AGENT": user_agent})

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({"BANGUMI_USER_AGENT": user_agent})

    def test_bangumi_user_agent_rejects_257_characters(self):
        response, persist = self._save({"BANGUMI_USER_AGENT": "M" * 257})

        self.assertEqual(response.status_code, 400)
        self.assertIn("256", self._payload(response)["error"])
        persist.assert_not_called()

    def test_bangumi_user_agent_trims_surrounding_whitespace(self):
        response, persist = self._save(
            {"BANGUMI_USER_AGENT": "  MediaFlux/1.0  "}
        )

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({"BANGUMI_USER_AGENT": "MediaFlux/1.0"})

    def test_indexer_switch_and_site_intervals_are_validated(self):
        response, persist = self._save({
            "INDEXER_SEARCH_ENABLED": "false",
            "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS": "8",
            "INDEXER_1LOU_MIN_INTERVAL_SECONDS": "6",
            "INDEXER_1LOU_GOOGLE_ENABLED": "true",
        })
        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({
            "INDEXER_SEARCH_ENABLED": "0",
            "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS": "8",
            "INDEXER_1LOU_MIN_INTERVAL_SECONDS": "6",
            "INDEXER_1LOU_GOOGLE_ENABLED": "1",
        })
        invalid_values = {
            "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS": ("-1", "61", "1.5"),
            "INDEXER_1LOU_MIN_INTERVAL_SECONDS": ("-1", "11", "1.5"),
        }
        for key, values in invalid_values.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    response, persist = self._save({key: value})
                    self.assertEqual(response.status_code, 400)
                    persist.assert_not_called()

    def test_indexer_site_selection_is_ordered_deduplicated_and_syncs_sukebei(self):
        response, persist = self._save({
            "DISCOVERY_RESOURCE_RESULTS_ENABLED": "1",
            "INDEXER_ENABLED_SITES": "tpb, nyaa, sukebei, tpb",
        })

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({
            "DISCOVERY_RESOURCE_RESULTS_ENABLED": "1",
            "INDEXER_ENABLED_SITES": "nyaa,tpb,sukebei",
            "INDEXER_SUKEBEI_ENABLED": "1",
        })

    def test_indexer_site_selection_rejects_unknown_or_empty_enabled_selection(self):
        for value, message in (("nyaa,evil", "未知资源站点"), ("", "至少选择一个资源站点")):
            with self.subTest(value=value):
                response, persist = self._save(
                    {
                        "DISCOVERY_RESOURCE_RESULTS_ENABLED": "1",
                        "INDEXER_ENABLED_SITES": value,
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, self._payload(response)["error"])
                persist.assert_not_called()

    def test_indexer_site_selection_clears_legacy_sukebei_toggle(self):
        response, persist = self._save({
            "DISCOVERY_RESOURCE_RESULTS_ENABLED": "1",
            "INDEXER_ENABLED_SITES": "nyaa,mikan",
        })

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({
            "DISCOVERY_RESOURCE_RESULTS_ENABLED": "1",
            "INDEXER_ENABLED_SITES": "nyaa,mikan",
            "INDEXER_SUKEBEI_ENABLED": "0",
        })

    def test_env_example_delegates_discovery_configuration_to_web_settings(self):
        env_text = Path(".env.example").read_text(encoding="utf-8")
        self.assertIn("业务配置请在 Web「设置」页维护", env_text)
        self.assertIn("./db/user.env", env_text)
        for key in (
            "DISCOVERY_ENABLED",
            "DISCOVERY_CACHE_TTL_SECONDS",
            "DISCOVERY_STALE_TTL_SECONDS",
            "DISCOVERY_DOUBAN_ENABLED",
            "DISCOVERY_RESOURCE_RESULTS_ENABLED",
            "INDEXER_SEARCH_ENABLED",
            "INDEXER_ENABLED_SITES",
            "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS",
            "INDEXER_1LOU_MIN_INTERVAL_SECONDS",
            "INDEXER_1LOU_GOOGLE_ENABLED",
            "DOUBAN_FRODO_API_KEY",
            "DOUBAN_FRODO_API_SECRET",
            "DOUBAN_DBCL2",
            "DOUBAN_CACHE_TTL_SECONDS",
            "BANGUMI_USER_AGENT",
        ):
            self.assertNotRegex(env_text, rf"(?m)^{key}=")
        self.assertNotIn("********", env_text)
        self.assertNotIn("123456789:test-dbcl2-value", env_text)

    def test_settings_page_exposes_dbcl2_but_no_frodo_controls(self):
        html = (Path("app/templates/settings.html").read_text(encoding="utf-8") + Path("app/static/js/settings.js").read_text(encoding="utf-8"))
        for key in (
            "DISCOVERY_ENABLED",
            "DISCOVERY_CACHE_TTL_SECONDS",
            "DISCOVERY_STALE_TTL_SECONDS",
            "DISCOVERY_DOUBAN_ENABLED",
            "DISCOVERY_RESOURCE_RESULTS_ENABLED",
            "INDEXER_SEARCH_ENABLED",
            "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS",
            "INDEXER_1LOU_MIN_INTERVAL_SECONDS",
            "INDEXER_1LOU_GOOGLE_ENABLED",
            "DOUBAN_CACHE_TTL_SECONDS",
            "DOUBAN_DBCL2",
        ):
            self.assertIn(f'data-key="{key}"', html)
        self.assertRegex(
            html,
            r'<input[^>]+type="password"[^>]+data-key="DOUBAN_DBCL2"[^>]*>',
        )
        self.assertNotIn("DOUBAN_FRODO_API_KEY", html)
        self.assertNotIn("DOUBAN_FRODO_API_SECRET", html)
        self.assertNotIn("data-disclosure-toggle", html)
        self.assertNotIn("配置可选回退", html)
        self.assertNotIn("可选 dbcl2 回退", html)
        self.assertRegex(
            html,
            re.compile(
                r'豆瓣公共探索[\s\S]+data-key="DOUBAN_DBCL2"'
            ),
        )
        self.assertIn('data-settings-target="discovery"', html)
        self.assertIn('data-settings-panel="discovery"', html)
        self.assertNotIn('data-key="BANGUMI_USER_AGENT"', html)

    def test_readme_documents_discovery_risk_cache_rollback_and_watchlist_boundary(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        for phrase in (
            "媒体探索（实验性）",
            "TMDB_API_KEY",
            "豆瓣 Frodo",
            "BANGUMI_USER_AGENT",
            "SQLite",
            "stale",
            "DISCOVERY_ENABLED=0",
            "DISCOVERY_DOUBAN_ENABLED=0",
            "探索收藏不等于 RSS 订阅",
        ):
            self.assertIn(phrase, readme)


if __name__ == "__main__":
    unittest.main()
