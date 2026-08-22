from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient
from app.modules.local_media_service import LocalMediaService


class LocalMediaRefreshTests(TestCase):

    def test_virtual_folder_listing_is_normalized(self):
        client = JellyfinClient("http://jellyfin", "token")
        payload = [
            {"ItemId": "movies", "Name": "电影", "Locations": ["/media/Movies"], "CollectionType": "movies"},
            {"ItemId": "", "Name": "broken", "Locations": []},
            "invalid",
        ]
        with patch.object(client, "_request", return_value=payload):
            self.assertEqual(client.list_virtual_folders(), [{
                "id": "movies", "name": "电影", "locations": ["/media/Movies"],
                "collection_type": "movies",
            }])

    def test_emby_virtual_folder_query_result_is_normalized(self):
        client = EmbyClient("http://emby", "token")
        client.product_kind = "emby"
        payload = {
            "Items": [{
                "ItemId": "movies",
                "Name": "电影",
                "Locations": ["/media/Movies"],
                "CollectionType": "movies",
            }],
            "TotalRecordCount": 1,
        }
        with patch.object(client, "_request", return_value=payload) as request:
            self.assertEqual(client.list_virtual_folders(), [{
                "id": "movies", "name": "电影", "locations": ["/media/Movies"],
                "collection_type": "movies",
            }])
        request.assert_called_once_with(
            "/Library/VirtualFolders/Query",
            params={"StartIndex": 0, "Limit": 1000},
        )

    def test_jellyfin_10_virtual_folder_listing_keeps_legacy_endpoint(self):
        client = EmbyClient("http://jellyfin", "token")
        client.product_kind = "jellyfin"
        payload = [{
            "ItemId": "tv", "Name": "剧集", "Locations": ["/media/TV"],
            "CollectionType": "tvshows",
        }]
        with patch.object(client, "_request", return_value=payload) as request:
            folders = client.list_virtual_folders()
        self.assertEqual(folders[0]["id"], "tv")
        request.assert_called_once_with("/Library/VirtualFolders")

    def test_unbound_refresh_batches_paths_and_isolates_provider_failures(self):
        jellyfin = Mock()
        jellyfin.refresh_for_paths.side_effect = RuntimeError("temporary")
        emby = Mock()
        emby.refresh_for_paths.return_value = {"ok": True, "fallback": ""}
        with patch(
            "app.modules.local_media_service.get_bool",
            side_effect=lambda key: key in {"JELLYFIN_ENABLED", "EMBY_ENABLED"},
        ), patch(
            "app.clients.jellyfin.JellyfinClient", return_value=jellyfin,
        ), patch(
            "app.clients.emby.EmbyClient", return_value=emby,
        ):
            warnings = LocalMediaService._refresh_paths({"/media/B", "/media/A"})

        jellyfin.refresh_for_paths.assert_called_once_with(["/media/A", "/media/B"])
        emby.refresh_for_paths.assert_called_once_with(["/media/A", "/media/B"])
        self.assertTrue(any("Jellyfin 刷新失败" in item for item in warnings))
        self.assertFalse(any("Emby" in item for item in warnings))

    def test_bound_target_refreshes_only_selected_library(self):
        profile = SimpleNamespace(
            server_type="jellyfin", label="Jellyfin", url="http://jellyfin",
            credential="token", enabled=True, configured=True,
        )
        client = Mock()
        client.list_virtual_folders.return_value = [
            {"id": "movies", "name": "电影", "locations": ["/media/Movies"], "collection_type": "movies"}
        ]
        client.refresh_for_paths.return_value = {"ok": True, "fallback": ""}
        plans = [SimpleNamespace(
            provider="jellyfin", library_id="movies", library_name="电影",
            target=Path("/media/Movies/Film/Film.mkv"),
        )]
        with patch(
            "app.modules.media_server_profiles.list_configured_profiles", return_value=[profile]
        ), patch("app.clients.jellyfin.JellyfinClient", return_value=client), patch.object(
            LocalMediaService, "_refresh_paths", return_value=[]
        ) as fallback:
            warnings = LocalMediaService._refresh_plans(plans)
        self.assertEqual(warnings, [])
        client.list_virtual_folders.assert_called_once_with()
        client.refresh_for_paths.assert_called_once_with(
            ["/media/Movies/Film"], allowed_library_ids=("movies",),
        )
        client.refresh_library.assert_not_called()
        fallback.assert_not_called()

    def test_bound_target_with_mismatched_library_name_is_safely_skipped(self):
        profile = SimpleNamespace(
            server_type="jellyfin", label="Jellyfin", url="http://jellyfin",
            credential="token", enabled=True, configured=True,
        )
        client = Mock()
        client.list_virtual_folders.return_value = [
            {"id": "movies", "name": "电影", "locations": ["/media/Movies"]},
        ]
        plans = [SimpleNamespace(
            provider="jellyfin", library_id="movies", library_name="剧集",
            target=Path("/media/TV/Show/E01.mkv"),
        )]
        with patch(
            "app.modules.media_server_profiles.list_configured_profiles",
            return_value=[profile],
        ), patch("app.clients.jellyfin.JellyfinClient", return_value=client):
            warnings = LocalMediaService._refresh_plans(plans)

        self.assertTrue(any("名称与绑定 ID 不一致" in item for item in warnings))
        client.refresh_for_paths.assert_not_called()
        client.refresh_library.assert_not_called()

    def test_legacy_name_binding_requires_unique_library(self):
        profile = SimpleNamespace(
            server_type="jellyfin", label="Jellyfin", url="http://jellyfin",
            credential="token", enabled=True, configured=True,
        )
        client = Mock()
        client.list_virtual_folders.return_value = [
            {"id": "movies-a", "name": "电影", "locations": [], "collection_type": "movies"},
            {"id": "movies-b", "name": "电影", "locations": [], "collection_type": "movies"},
        ]
        plans = [SimpleNamespace(
            provider="jellyfin", library_id="", library_name="电影",
            target=Path("/media/Movies/Film/Film.mkv"),
        )]
        with patch(
            "app.modules.media_server_profiles.list_configured_profiles", return_value=[profile]
        ), patch("app.clients.jellyfin.JellyfinClient", return_value=client):
            warnings = LocalMediaService._refresh_plans(plans)
        self.assertIn("存在同名媒体库", warnings[0])
        client.refresh_for_paths.assert_not_called()
        client.refresh_library.assert_not_called()

    def test_jellyfin_refresh_for_path_only_refreshes_matching_library(self):
        client = JellyfinClient("http://jellyfin", "token")
        folders = [
            {"ItemId": "movies", "Name": "电影", "Locations": ["/media/Movies"]},
            {"ItemId": "tv", "Name": "剧集", "Locations": ["/media/TV"]},
        ]
        def request(path, **_kwargs):
            if path == "/Library/VirtualFolders":
                return folders
            if path == "/Items":
                return {"Items": [], "TotalRecordCount": 0}
            raise AssertionError(path)

        with patch.object(client, "_request", side_effect=request), patch.object(
            client, "refresh_library", return_value=True
        ) as refresh, patch.object(client, "refresh_all", return_value=True) as refresh_all:
            self.assertTrue(client.refresh_for_path("/media/Movies/Film (2026)"))
        refresh.assert_called_once_with("movies")
        refresh_all.assert_not_called()
