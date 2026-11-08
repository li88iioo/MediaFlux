"""索引器就绪诊断的本地聚合、安全、路由与 API 契约。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from app import sdk
from app.indexers.models import IndexerCapabilities
from tests.support import IsolatedDatabaseTestCase


class _Adapter:
    def __init__(
        self, site_id: str, site_name: str, *, pagination: bool, kinds: tuple[str, ...]
    ):
        self.site_id = site_id
        self.site_name = site_name
        self.base_url = f"https://{site_id}.INDEXER_ADAPTER_SECRET.invalid/"
        self.capabilities = IndexerCapabilities(pagination, kinds)
        self.http = SimpleNamespace(cookies={"session": "INDEXER_COOKIE_SECRET"})


class _Registry:
    def __init__(
        self, adapters: dict[str, _Adapter], *, broken: set[str] | None = None
    ):
        self._adapters = adapters
        self._broken = set(broken or ())

    def ids(self):
        return tuple(self._adapters)

    def get(self, site_id: str):
        if site_id in self._broken:
            raise RuntimeError("INDEXER_EXCEPTION_SECRET /srv/indexer/private")
        return self._adapters[site_id]


class _Service:
    def __init__(
        self, adapters: dict[str, _Adapter], enabled: set[str], *, broken=None
    ):
        self.registry = _Registry(adapters, broken=broken)
        self.enabled_site_ids = frozenset(enabled)

    def site_ids_for_scope(self, scope="manual"):
        assert scope == "agent"
        return tuple(self.enabled_site_ids)

    async def search(self, *_args, **_kwargs):
        raise AssertionError("readiness diagnosis must not search providers")

    async def search_media(self, *_args, **_kwargs):
        raise AssertionError("readiness diagnosis must not search providers")


class IndexerReadinessUnitTests(IsolatedDatabaseTestCase):
    def test_native_signature_rejects_extra_fields(self):
        self.assertNotIn("resources.sources", sdk.WRITE_METHODS)
        with self.assertRaises(TypeError):
            sdk.METHODS["resources.sources"](debug=True)

    def test_disabled_state_does_not_construct_service(self):
        with (
            patch(
                "app.indexers.runtime.config.get_bool",
                return_value=False,
            ),
            patch(
                "app.indexers.runtime.get_indexer_service"
            ) as service_factory,
        ):
            result = sdk.resources.sources()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["indexer_enabled"])
        self.assertFalse(result["network_accessed"])
        self.assertFalse(result["filesystem_accessed"])
        service_factory.assert_not_called()

    def test_ready_projection_reports_effective_capabilities_without_network(self):
        service = _Service(
            {
                "nyaa": _Adapter(
                    "nyaa", "Nyaa", pagination=True, kinds=("magnet", "torrent")
                ),
                "search-only": _Adapter(
                    "search-only", "Search-only", pagination=False, kinds=()
                ),
                "sukebei": _Adapter(
                    "sukebei", "Sukebei", pagination=True, kinds=("magnet", "torrent")
                ),
            },
            {"nyaa", "search-only"},
        )
        with (
            patch(
                "app.indexers.runtime.config.get_bool", return_value=True
            ),
            patch(
                "app.indexers.runtime.get_indexer_service",
                return_value=service,
            ),
        ):
            result = sdk.resources.sources()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(
            result["counts"],
            {
                "registered": 3,
                "enabled": 2,
                "searchable": 2,
                "downloadable": 1,
                "attention": 0,
            },
        )
        by_id = {site["site_id"]: site for site in result["sites"]}
        self.assertEqual(by_id["nyaa"]["download_kinds"], ["magnet", "torrent"])
        self.assertEqual(by_id["search-only"]["reason"], "search_only")
        self.assertTrue(by_id["search-only"]["search_available"])
        self.assertFalse(by_id["search-only"]["download_available"])
        self.assertTrue(by_id["sukebei"]["sensitive"])
        self.assertFalse(by_id["sukebei"]["enabled"])
        self.assertEqual(
            result["transport"],
            {"mode": "direct", "outbound_proxy_applied": False},
        )

    def test_no_enabled_sites_and_partial_registry_failure_are_explicit(self):
        adapters = {
            "nyaa": _Adapter("nyaa", "Nyaa", pagination=True, kinds=("magnet",)),
            "mikan": _Adapter("mikan", "Mikan", pagination=False, kinds=("magnet",)),
        }
        with (
            patch(
                "app.indexers.runtime.config.get_bool", return_value=True
            ),
            patch(
                "app.indexers.runtime.get_indexer_service",
                return_value=_Service(adapters, set()),
            ),
        ):
            empty = sdk.resources.sources()
        self.assertEqual(empty["status"], "no_enabled_sites")
        self.assertEqual(empty["counts"]["enabled"], 0)
        with (
            patch(
                "app.indexers.runtime.config.get_bool", return_value=True
            ),
            patch(
                "app.indexers.runtime.get_indexer_service",
                return_value=_Service(adapters, {"nyaa", "mikan"}, broken={"mikan"}),
            ),
        ):
            partial = sdk.resources.sources()
        self.assertEqual(partial["status"], "attention")
        self.assertEqual(partial["counts"]["enabled"], 2)
        self.assertEqual(partial["counts"]["attention"], 1)
        mikan = next(
            site for site in partial["sites"] if site["site_id"] == "mikan"
        )
        self.assertEqual(mikan["reason"], "registry_unavailable")
        self.assertFalse(mikan["search_available"])

    def test_output_omits_urls_cookies_config_values_and_raw_errors(self):
        service = _Service(
            {"nyaa": _Adapter("nyaa", "Nyaa", pagination=True, kinds=("magnet",))},
            {"nyaa"},
        )
        with (
            patch(
                "app.indexers.runtime.config.get_bool", return_value=True
            ),
            patch(
                "app.indexers.runtime.get_indexer_service",
                return_value=service,
            ),
        ):
            result = sdk.resources.sources()
        serialized = json.dumps(result, ensure_ascii=False)
        for secret in (
            "INDEXER_ADAPTER_SECRET",
            "INDEXER_COOKIE_SECRET",
            "base_url",
            "cookies",
            "allowed_hosts",
            "INDEXER_USER_AGENT",
        ):
            self.assertNotIn(secret, serialized)
        with (
            patch(
                "app.indexers.runtime.config.get_bool", return_value=True
            ),
            patch(
                "app.indexers.runtime.get_indexer_service",
                side_effect=RuntimeError(
                    "INDEXER_EXCEPTION_SECRET /srv/indexer/private"
                ),
            ),
        ):
            unavailable = sdk.resources.sources()
        serialized = json.dumps(unavailable, ensure_ascii=False)
        self.assertFalse(unavailable["ok"])
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertNotIn("INDEXER_EXCEPTION_SECRET", serialized)
        self.assertNotIn("/srv/indexer/private", serialized)
        with patch(
            "app.indexers.runtime.config.get_bool",
            side_effect=RuntimeError("INDEXER_CONFIG_SECRET /srv/config/private"),
        ):
            unavailable = sdk.resources.sources()
        serialized = json.dumps(unavailable, ensure_ascii=False)
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertIsNone(unavailable["indexer_enabled"])
        self.assertNotIn("INDEXER_CONFIG_SECRET", serialized)
        self.assertNotIn("/srv/config/private", serialized)
