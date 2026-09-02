"""短生命周期媒体服务器客户端必须在所有聚合路径显式释放。"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from app.clients.base import DashboardData, MediaServerClient, SeriesSearchResult


class MediaServerClientLifecycleTests(unittest.TestCase):
    @staticmethod
    def _source(client: MagicMock) -> list[tuple[str, str, str, MagicMock]]:
        return [("jellyfin", "Jellyfin", "http://private.local", client)]

    def test_dashboard_jobs_close_each_client(self) -> None:
        from app import services

        emby_client = MagicMock()
        emby_client.get_dashboard.return_value = DashboardData(server_name="Emby")
        jellyfin_client = MagicMock()
        jellyfin_client.get_dashboard.return_value = DashboardData(server_name="Jellyfin")
        enabled = {"EMBY_ENABLED": True, "JELLYFIN_ENABLED": True}
        values = {
            "EMBY_URL": "http://emby.local",
            "EMBY_TOKEN": "emby-token",
            "JELLYFIN_URL": "http://jellyfin.local",
            "JELLYFIN_API_KEY": "jellyfin-key",
        }
        with (
            patch(
                "app.services.get_bool",
                side_effect=lambda key, default=False: enabled.get(key, default),
            ),
            patch(
                "app.services.get",
                side_effect=lambda key, default="": values.get(key, default),
            ),
            patch("app.services.EmbyClient", return_value=emby_client),
            patch("app.services.JellyfinClient", return_value=jellyfin_client),
        ):
            boards = services._fetch_dashboards()

        self.assertEqual([board.server_type for board in boards], ["emby", "jellyfin"])
        emby_client.close.assert_called_once_with()
        jellyfin_client.close.assert_called_once_with()

    def test_context_manager_close_failure_does_not_replace_business_result(self) -> None:
        client = object.__new__(MediaServerClient)
        client.close = MagicMock(side_effect=RuntimeError("close failed"))

        with client as entered:
            self.assertIs(entered, client)

        client.close.assert_called_once_with()

    def test_context_manager_close_failure_preserves_business_exception(self) -> None:
        client = object.__new__(MediaServerClient)
        client.close = MagicMock(side_effect=RuntimeError("close failed"))

        with self.assertRaisesRegex(ValueError, "business failed"):
            with client:
                raise ValueError("business failed")

        client.close.assert_called_once_with()

    def test_partial_source_construction_closes_already_created_clients(self) -> None:
        from app import services

        emby_client = MagicMock()
        enabled = {"EMBY_ENABLED": True, "JELLYFIN_ENABLED": True}
        values = {
            "EMBY_URL": "http://emby.local",
            "EMBY_TOKEN": "emby-token",
            "JELLYFIN_URL": "http://jellyfin.local",
            "JELLYFIN_API_KEY": "jellyfin-key",
        }
        with (
            patch(
                "app.services.get_bool",
                side_effect=lambda key, default=False: enabled.get(key, default),
            ),
            patch(
                "app.services.get",
                side_effect=lambda key, default="": values.get(key, default),
            ),
            patch("app.services.EmbyClient", return_value=emby_client),
            patch("app.services.JellyfinClient", side_effect=RuntimeError("constructor failed")),
            self.assertRaisesRegex(RuntimeError, "constructor failed"),
        ):
            services._configured_media_sources()

        emby_client.close.assert_called_once_with()

    def test_all_parallel_media_aggregators_close_clients(self) -> None:
        from app import services

        cases = []

        recent_client = MagicMock()
        recent_client.recent_media.return_value = []
        cases.append(("recent", recent_client, lambda: services.build_recent_media()))

        search_client = MagicMock()
        search_client.search_media.return_value = []
        cases.append(("search", search_client, lambda: services.search_media_servers("test")))

        identity_client = MagicMock()
        identity_client.has_tmdb_media.return_value = True
        cases.append((
            "identity",
            identity_client,
            lambda: services.inspect_media_identity_sources("12345", "movie"),
        ))

        strict_client = MagicMock()
        strict_client.find_series_candidates_by_tmdb.return_value = SeriesSearchResult(
            candidates=[], total=0, truncated=False
        )
        cases.append((
            "strict_inventory",
            strict_client,
            lambda: services.inspect_series_episode_inventory_by_tmdb("12345"),
        ))

        series_client = MagicMock()
        series_client.search_series_candidates.return_value = SeriesSearchResult(
            candidates=[], total=0, truncated=False
        )
        cases.append((
            "series_inventory",
            series_client,
            lambda: services.inspect_series_episode_sources("Example"),
        ))

        for label, client, invoke in cases:
            with self.subTest(label=label), patch(
                "app.services._configured_media_sources",
                return_value=self._source(client),
            ):
                invoke()
            client.close.assert_called_once_with()

    def test_media_aggregator_closes_client_after_provider_failure(self) -> None:
        from app import services

        client = MagicMock()
        client.recent_media.side_effect = RuntimeError("provider unavailable")
        with patch(
            "app.services._configured_media_sources",
            return_value=self._source(client),
        ):
            result = services.build_recent_media()

        self.assertEqual(result[0]["error"], "媒体服务器暂时不可用")
        client.close.assert_called_once_with()

    def test_close_failure_does_not_replace_successful_media_result(self) -> None:
        from app import services

        client = MagicMock()
        client.recent_media.return_value = []
        client.close.side_effect = RuntimeError("close failed")
        with patch(
            "app.services._configured_media_sources",
            return_value=self._source(client),
        ):
            result = services.build_recent_media()

        self.assertEqual(result[0]["items"], [])
        client.close.assert_called_once_with()

    def test_library_inventory_closes_client_on_deadline_and_provider_failure(self) -> None:
        from app import services

        expired_client = MagicMock()
        with patch(
            "app.services._configured_media_sources",
            return_value=self._source(expired_client),
        ):
            expired = services.inspect_library_series_sources(
                deadline_at=time.monotonic() - 1
            )
        self.assertTrue(expired[0]["deadline_exhausted"])
        expired_client.list_library_series.assert_not_called()
        expired_client.close.assert_called_once_with()

        failed_client = MagicMock()
        failed_client.list_library_series.side_effect = RuntimeError("provider unavailable")
        with patch(
            "app.services._configured_media_sources",
            return_value=self._source(failed_client),
        ):
            failed = services.inspect_library_series_sources(
                deadline_at=time.monotonic() + 30
            )
        self.assertEqual(failed[0]["status"], "unavailable")
        failed_client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
