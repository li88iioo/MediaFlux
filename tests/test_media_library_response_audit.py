"""媒体库目录读取须与媒体列表共享响应合同，不把坏载荷伪装成空库。"""

from __future__ import annotations

import re
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient
from app.main import create_app
from app.modules import media_library
from app.modules.media_server_profiles import MediaServerProfile
from app.routes.media_libraries_api import _probe_profile
from tests.support import IsolatedDatabaseTestCase


class MediaLibraryResponseAuditTests(TestCase):
    @staticmethod
    def _profile() -> MediaServerProfile:
        return MediaServerProfile(
            source="configured:jellyfin",
            server_type="jellyfin",
            label="Jellyfin",
            url="http://synthetic.invalid",
            credential="synthetic",
            enabled=True,
        )

    def test_normal_array_query_result_and_compatible_empty_share_projection(self):
        row = {"ItemId": "movies", "Name": "电影", "Locations": ["/media/Movies", None]}
        expected = [
            {
                "id": "movies",
                "name": "电影",
                "locations": ["/media/Movies"],
                "collection_type": "",
            }
        ]
        for factory, kind in (
            (JellyfinClient, ""),
            (EmbyClient, "emby"),
            (EmbyClient, "jellyfin"),
        ):
            with factory("http://synthetic.invalid", "synthetic") as client:
                if isinstance(client, EmbyClient):
                    client.product_kind = kind
                for payload in ([None, row, "bad-row"], {"Items": [False, row]}):
                    with (
                        self.subTest(
                            factory=factory.__name__, kind=kind, payload=payload
                        ),
                        patch.object(client, "_request", return_value=payload),
                    ):
                        self.assertEqual(client.list_virtual_folders(), expected)
                for payload in ([], {}, {"Items": []}):
                    with (
                        self.subTest(empty=payload),
                        patch.object(client, "_request", return_value=payload),
                    ):
                        self.assertEqual(client.list_virtual_folders(), [])

    def test_malformed_envelope_is_not_a_successful_empty_library(self):
        for factory, kind in (
            (JellyfinClient, ""),
            (EmbyClient, "emby"),
            (EmbyClient, "jellyfin"),
        ):
            with factory("http://synthetic.invalid", "synthetic") as client:
                if isinstance(client, EmbyClient):
                    client.product_kind = kind
                for payload in (
                    None,
                    False,
                    "bad-response",
                    {"Items": None},
                    {"Items": {}},
                    {"Items": "bad"},
                ):
                    with (
                        self.subTest(
                            factory=factory.__name__, kind=kind, payload=payload
                        ),
                        patch.object(client, "_request", return_value=payload),
                    ):
                        with self.assertRaisesRegex(ValueError, "响应结构无效"):
                            client.list_virtual_folders()

    def test_control_center_and_public_library_report_unavailable_not_zero_libraries(self):
        profile = self._profile()
        with (
            JellyfinClient(profile.url, profile.credential) as client,
            patch.object(client, "_request", return_value={"Items": None}),
            patch.object(client, "close") as close,
        ):
            with patch(
                "app.routes.media_libraries_api._client_for", return_value=client
            ):
                provider, libraries, error = _probe_profile(profile)
            self.assertEqual(provider, "jellyfin")
            self.assertEqual(libraries, [])
            self.assertTrue(error)
            close.assert_called_once()
            with (
                patch.object(media_library, "list_configured_profiles", return_value=[profile]),
                patch.object(MediaServerProfile, "create_client", return_value=client),
            ):
                with self.assertRaises(media_library.MediaLibraryError) as raised:
                    media_library.libraries("jellyfin")
            self.assertEqual(raised.exception.code, "provider_unavailable")
            self.assertEqual(close.call_count, 2)

    def test_bad_library_read_remains_retryable_without_global_refresh(self):
        with (
            JellyfinClient("http://synthetic.invalid", "synthetic") as client,
            patch.object(client, "_request", return_value=False),
        ):
            outcome = client.refresh_for_paths(
                ["/media/Movies"], allow_global_fallback=False
            )
        self.assertFalse(outcome["ok"])
        self.assertTrue(outcome["retryable"])
        self.assertEqual(outcome["succeeded_target_ids"], [])
        self.assertIn("目录读取失败", outcome["fallback"])


class MediaLibraryHttpResponseAuditTests(IsolatedDatabaseTestCase):
    def test_bad_library_response_is_http_error_and_recovery_returns_real_match(self):
        from tests.support import initialized_web_runtime
        self.enterContext(initialized_web_runtime())
        def csrf(html):
            match = re.search(
                r'name="csrf[-_]token"\s+(?:value|content)="([^"]+)"', html
            )
            assert (match) is not None
            return match.group(1)

        profile = MediaLibraryResponseAuditTests._profile()
        with TestClient(create_app(start_background=False)) as web_client:
            login = web_client.post(
                "/login",
                data={
                    "username": "admin",
                    "password": "123456",
                    "csrf_token": csrf(web_client.get("/login").text),
                },
                follow_redirects=False,
            )
            self.assertEqual(login.status_code, 302)
            headers = {"X-CSRF-Token": csrf(web_client.get("/media-libraries").text)}
            payload = {
                "provider": "jellyfin",
                "local_path": "/media/Movies",
                "server_path": "/server/Movies",
                "sample_path": "/media/Movies/Film.mkv",
            }
            with (
                JellyfinClient(profile.url, profile.credential) as client,
                patch.object(
                    client, "_request", return_value={"Items": None}
                ) as request,
                patch(
                    "app.routes.media_libraries_api.list_configured_profiles",
                    return_value=[profile],
                ),
                patch(
                    "app.routes.media_libraries_api._client_for", return_value=client
                ),
            ):
                failed = web_client.post(
                    "/api/media-libraries/path-test", headers=headers, json=payload
                )
                self.assertEqual(failed.status_code, 502, failed.text)
                self.assertNotIn("unmatched", failed.text)
                request.return_value = [
                    {
                        "ItemId": "movies",
                        "Name": "电影",
                        "Locations": ["/server/Movies"],
                    }
                ]
                recovered = web_client.post(
                    "/api/media-libraries/path-test", headers=headers, json=payload
                )
                self.assertEqual(recovered.status_code, 200, recovered.text)
                self.assertEqual(recovered.json()["status"], "matched")
                self.assertEqual(recovered.json()["matches"][0]["id"], "movies")
