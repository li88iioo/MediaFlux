"""媒体列表共用响应合同，保留合法空值与不同服务器的业务投影。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient


class MediaListPayloadContractTests(unittest.TestCase):
    def client(self, cls):
        client = cls("http://synthetic.invalid", "synthetic")
        client._cached_user_id = "viewer"
        self.addCleanup(client.close)
        if isinstance(client, EmbyClient):
            client.product_kind = "emby"
        else:
            self.enterContext(
                patch.object(
                    client,
                    "_recent_played_from_activity",
                    side_effect=RuntimeError("unsupported"),
                )
            )
        return client

    @staticmethod
    def readers(client):
        return {
            "resume": lambda: client.continue_watching("viewer"),
            "search": lambda: client.search_media("Film").items,
            "recent": client.recent_media,
            "history": lambda: client.recently_played("viewer"),
            "libraries": client._libraries,
        }

    def test_invalid_envelopes_are_not_successful_empty_reads(self):
        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            for name, reader in self.readers(client).items():
                for payload in (
                    None,
                    False,
                    0,
                    "",
                    {"Items": None},
                    {"Items": False},
                    {"Items": 0},
                    {"Items": ""},
                    {"Items": {}},
                ):
                    with (
                        self.subTest(
                            server=cls.__name__, operation=name, payload=payload
                        ),
                        patch.object(client, "_request", return_value=payload),
                    ):
                        with self.assertRaises(ValueError):
                            reader()

    def test_valid_empty_responses_keep_existing_optional_items_contract(self):
        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            for name, reader in self.readers(client).items():
                for payload in ([], {}, {"Items": []}):
                    with (
                        self.subTest(
                            server=cls.__name__, operation=name, payload=payload
                        ),
                        patch.object(client, "_request", return_value=payload),
                    ):
                        self.assertEqual(reader(), [])

    def test_recent_and_library_reads_skip_non_object_rows_like_search(self):
        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            item = {
                "Id": "film",
                "Name": "Film",
                "Type": "Movie",
                "DateCreated": "2026-01-01T00:00:00Z",
            }
            with (
                self.subTest(server=cls.__name__),
                patch.object(
                    client, "_request", return_value={"Items": [None, "bad", item]}
                ),
                patch.object(client, "_library_count", return_value=1),
            ):
                self.assertEqual([row.id for row in client.recent_media()], ["film"])
                self.assertEqual([row.id for row in client._libraries()], ["film"])

    def test_history_bad_user_data_does_not_hide_valid_history(self):
        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            item = {
                "Id": "film",
                "Name": "Film",
                "Type": "Movie",
                "UserData": {"LastPlayedDate": "2026-01-01T00:00:00Z"},
            }
            payload = {"Items": [None, {"Id": "bad", "UserData": ["invalid"]}, item]}
            with (
                self.subTest(server=cls.__name__),
                patch.object(client, "_request", return_value=payload),
            ):
                self.assertEqual(
                    [row.id for row in client.recently_played("viewer")], ["film"]
                )

    def test_provider_reports_failure_and_releases_client_instead_of_empty_success(
        self,
    ):
        from app.modules.media_library import MediaLibraryError, recent
        from app.modules.media_server_profiles import MediaServerProfile

        for cls, name in ((EmbyClient, "emby"), (JellyfinClient, "jellyfin")):
            client = self.client(cls)
            profile = MediaServerProfile(
                f"configured:{name}",
                name,
                name,
                client.url,
                "synthetic",
                True,
                "viewer",
            )
            with (
                self.subTest(server=name),
                patch("app.modules.media_library.list_configured_profiles", return_value=[profile]),
                patch.object(MediaServerProfile, "create_client", return_value=client),
                patch.object(client, "_request", return_value={"Items": False}),
                patch.object(client, "close", wraps=client.close) as close,
            ):
                with self.assertRaises(MediaLibraryError) as error:
                    recent(name, limit=5)
                self.assertEqual(error.exception.code, "provider_unavailable")
                close.assert_called_once()
