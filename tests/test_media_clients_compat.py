"""Jellyfin 12 与 Emby/Jellyfin 10.x 兼容客户端契约。"""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from app.clients.base import DashboardData
from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient


class MediaRuntimeTests(unittest.TestCase):
    def test_client_context_manager_closes_underlying_session(self):
        client = JellyfinClient("http://jellyfin.local", "key")
        client._session = Mock()
        with client as active:
            self.assertIs(active, client)
        client._session.close.assert_called_once_with()

    def test_jellyfin_isolated_dashboard_part_closes_child_client(self):
        parent = JellyfinClient("http://jellyfin.local", "key")
        child = Mock()
        child._libraries.return_value = ["library"]
        with patch("app.clients.jellyfin.JellyfinClient", return_value=child):
            result = parent._isolated_part("_libraries", "user-id")
        self.assertEqual(result, ["library"])
        self.assertEqual(child._cached_user_id, "user-id")
        child.close.assert_called_once_with()

    def test_runtime_ticks_are_converted_to_rounded_minutes(self):
        payload = {"Id": "item", "Name": "示例", "Type": "Movie", "RunTimeTicks": 2_400_000_000}

        jellyfin = JellyfinClient("http://jellyfin.local", "key")._media_item(payload)
        legacy = EmbyClient("http://legacy.local", "token")._media_item(payload)

        self.assertEqual(jellyfin.runtime, 4)
        self.assertEqual(legacy.runtime, 4)

    def test_runtime_half_minute_rounds_up_instead_of_bankers_rounding(self):
        payload = {"Id": "item", "Name": "示例", "Type": "Movie", "RunTimeTicks": 1_500_000_000}
        self.assertEqual(JellyfinClient("http://jellyfin.local", "key")._media_item(payload).runtime, 3)

    def test_missing_runtime_remains_zero(self):
        payload = {"Id": "item", "Name": "示例", "Type": "Movie"}
        self.assertEqual(JellyfinClient("http://jellyfin.local", "key")._media_item(payload).runtime, 0)
        self.assertEqual(EmbyClient("http://legacy.local", "token")._media_item(payload).runtime, 0)


class DashboardMediaCountTests(unittest.TestCase):
    def test_total_items_counts_only_user_visible_playable_media(self):
        expected_params = {
            "Recursive": "true",
            "Limit": 0,
            "EnableImages": "false",
            "IncludeItemTypes": "Movie,Episode,Audio,MusicVideo,Book,Video",
        }
        for client in (
            JellyfinClient("http://jellyfin.local", "key"),
            EmbyClient("http://legacy.local", "token"),
        ):
            with self.subTest(client=type(client).__name__):
                client._cached_user_id = "user-id"
                client._request = Mock(return_value={"TotalRecordCount": 16})

                self.assertEqual(client._total_items(), 16)
                client._request.assert_called_once_with(
                    "/Users/user-id/Items",
                    params=expected_params,
                )

    def test_jellyfin_media_counts_are_scoped_to_dashboard_user(self):
        client = JellyfinClient("http://jellyfin.local", "key")
        client._cached_user_id = "user-id"
        client._request = Mock(return_value={
            "MovieCount": 3,
            "SeriesCount": 8,
            "EpisodeCount": 13,
            "ItemCount": 36,
        })

        self.assertEqual(client._media_counts(), {
            "movie_count": 3,
            "series_count": 8,
            "episode_count": 13,
        })
        client._request.assert_called_once_with(
            "/Items/Counts",
            params={"userId": "user-id"},
        )


class LegacyMediaClientTests(unittest.TestCase):
    def test_identifies_jellyfin_10_compatible_node(self):
        client = EmbyClient("http://legacy.local", "token")
        client._request = Mock(return_value={
            "ServerName": "备用媒体库",
            "ProductName": "Jellyfin Server",
            "Version": "10.11.11",
        })

        name, product, version = client._server_identity()

        self.assertEqual(name, "备用媒体库")
        self.assertEqual(product, "Jellyfin")
        self.assertEqual(version, "10.11.11")
        self.assertEqual(client.product_kind, "jellyfin")

    def test_user_lookup_prefers_authenticated_users_and_falls_back_to_public(self):
        client = EmbyClient("http://legacy.local", "token")
        calls: list[str] = []

        def request(path, params=None):
            calls.append(path)
            if path == "/Users":
                raise RuntimeError("legacy endpoint unavailable")
            if path == "/Users/Public":
                return [{"Id": "public-user", "Policy": {"IsAdministrator": True}}]
            raise AssertionError(path)

        client._request = request
        self.assertEqual(client._user_id(), "public-user")
        self.assertEqual(calls, ["/Users", "/Users/Public"])

    def test_resume_endpoint_drives_recent_and_total_playback(self):
        client = EmbyClient("http://legacy.local", "token")
        client._cached_user_id = "user-id"
        calls: list[tuple[str, dict]] = []

        def request(path, params=None):
            calls.append((path, params or {}))
            if (params or {}).get("Limit") == 0:
                return {"TotalRecordCount": 9}
            return {"Items": [{
                "Id": "episode", "Name": "第一集", "Type": "Episode",
                "SeriesId": "series", "SeriesName": "测试剧",
                "UserData": {"PlayedPercentage": 42, "LastPlayedDate": "2026-07-28T10:00:00Z"},
            }]}

        client._request = request
        recent = client._recent_played()
        total = client._total_plays()

        self.assertEqual(recent[0].display_name, "测试剧")
        self.assertEqual(recent[0].progress, 42)
        self.assertEqual(total, 9)
        self.assertEqual(calls[0][0], "/Users/user-id/Items/Resume")
        self.assertEqual(calls[1][0], "/Users/user-id/Items/Resume")

    def test_legacy_auth_sends_both_compatible_headers(self):
        client = EmbyClient("http://legacy.local", "token")
        headers = client._headers()
        self.assertEqual(headers["X-Emby-Token"], "token")
        self.assertIn('MediaBrowser Token="token"', headers["Authorization"])


class JellyfinAuthenticationTests(unittest.TestCase):
    def test_requests_keep_api_key_out_of_query_string(self):
        client = JellyfinClient("http://jellyfin.local", "secret-token")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"Items": []}
        client._session.get = Mock(return_value=response)

        client._request("/Users", {"Limit": 1})

        call = client._session.get.call_args
        self.assertEqual(call.kwargs["params"], {"Limit": 1})
        self.assertNotIn("api_key", call.kwargs["params"])
        self.assertIn('MediaBrowser Token="secret-token"', call.kwargs["headers"]["Authorization"])


class JellyfinPlaybackHistoryTests(unittest.TestCase):
    def test_recent_played_uses_date_played_history_and_filters_unplayed_items(self):
        client = JellyfinClient("http://jellyfin.local", "key")
        client._cached_user_id = "user-id"
        calls: list[tuple[str, dict]] = []

        def request(path, params=None):
            calls.append((path, params or {}))
            if path == "/System/ActivityLog/Entries":
                raise RuntimeError("activity log unavailable")
            return {
                "Items": [
                    {
                        "Id": "partial",
                        "Name": "第二集",
                        "Type": "Episode",
                        "SeriesId": "series",
                        "SeriesName": "测试剧",
                        "IndexNumber": 2,
                        "UserData": {
                            "PlayedPercentage": 42,
                            "LastPlayedDate": "2026-08-03T18:00:00Z",
                        },
                    },
                    {
                        "Id": "completed",
                        "Name": "第一集",
                        "Type": "Episode",
                        "SeriesId": "series",
                        "SeriesName": "测试剧",
                        "IndexNumber": 1,
                        "UserData": {
                            "Played": True,
                            "LastPlayedDate": "2026-08-03T19:00:00Z",
                        },
                    },
                    {
                        "Id": "never-played",
                        "Name": "未播放",
                        "Type": "Movie",
                        "UserData": {},
                    },
                ]
            }

        client._request = request
        recent = client._recent_played()

        self.assertEqual([item.id for item in recent], ["completed", "partial"])
        self.assertEqual(recent[0].progress, 100)
        self.assertEqual(recent[1].progress, 42)
        self.assertEqual(calls[0][0], "/System/ActivityLog/Entries")
        self.assertEqual(calls[1][0], "/Items")
        params = calls[1][1]
        self.assertEqual(params["UserId"], "user-id")
        self.assertEqual(params["SortBy"], "DatePlayed")
        self.assertEqual(params["SortOrder"], "Descending")
        self.assertEqual(params["EnableUserData"], "true")
        self.assertEqual(params["IncludeItemTypes"], "Movie,Episode")

    def test_recent_played_prefers_actual_playback_activity_over_bulk_played_state(self):
        client = JellyfinClient("http://jellyfin.local", "key")
        client._cached_user_id = "user-id"
        calls: list[tuple[str, dict]] = []

        def request(path, params=None):
            calls.append((path, params or {}))
            if path == "/System/ActivityLog/Entries":
                return {
                    "Items": [
                        {
                            "Id": 3,
                            "Type": "VideoPlaybackStopped",
                            "UserId": "user-id",
                            "ItemId": "actual-1",
                            "Date": "2026-08-07T19:00:00Z",
                        },
                        {
                            "Id": 2,
                            "Type": "VideoPlayback",
                            "UserId": "user-id",
                            "ItemId": "actual-1",
                            "Date": "2026-08-07T18:50:00Z",
                        },
                        {
                            "Id": 1,
                            "Type": "VideoPlaybackStopped",
                            "UserId": "user-id",
                            "ItemId": "actual-2",
                            "Date": "2026-08-07T18:00:00Z",
                        },
                        {
                            "Id": 4,
                            "Type": "VideoPlaybackStopped",
                            "UserId": "other-user",
                            "ItemId": "other-item",
                            "Date": "2026-08-07T20:00:00Z",
                        },
                    ]
                }
            if path == "/Items":
                return {
                    "Items": [
                        {
                            "Id": "actual-2",
                            "Name": "第二集",
                            "Type": "Episode",
                            "SeriesId": "series",
                            "SeriesName": "测试剧",
                            "IndexNumber": 2,
                            "UserData": {"Played": True},
                        },
                        {
                            "Id": "actual-1",
                            "Name": "第一集",
                            "Type": "Episode",
                            "SeriesId": "series",
                            "SeriesName": "测试剧",
                            "IndexNumber": 1,
                            "UserData": {"Played": True},
                        },
                    ]
                }
            raise AssertionError(path)

        client._request = request
        recent = client._recent_played(limit=2)

        self.assertEqual([item.id for item in recent], ["actual-1", "actual-2"])
        expected_first = datetime.fromisoformat(
            "2026-08-07T19:00:00+00:00"
        ).astimezone().isoformat(timespec="seconds")
        expected_second = datetime.fromisoformat(
            "2026-08-07T18:00:00+00:00"
        ).astimezone().isoformat(timespec="seconds")
        self.assertEqual(recent[0].last_played, expected_first)
        self.assertEqual(recent[1].last_played, expected_second)
        self.assertEqual([call[0] for call in calls], [
            "/System/ActivityLog/Entries", "/Items",
        ])
        self.assertEqual(calls[1][1]["Ids"], "actual-1,actual-2")

    def test_recent_played_does_not_fall_back_when_activity_log_has_no_playback(self):
        client = JellyfinClient("http://jellyfin.local", "key")
        client._cached_user_id = "user-id"
        client._request = Mock(return_value={
            "Items": [{"Type": "SessionStarted", "UserId": "user-id"}],
        })

        self.assertEqual(client._recent_played(), [])
        client._request.assert_called_once()

    def test_user_lookup_prefers_administrator_and_caches_result(self):
        client = JellyfinClient("http://jellyfin.local", "key")
        client._request = Mock(
            return_value=[
                {"Id": "viewer", "Policy": {"IsAdministrator": False}},
                {"Id": "admin", "Policy": {"IsAdministrator": True}},
            ]
        )

        self.assertEqual(client._user_id(), "admin")
        self.assertEqual(client._user_id(), "admin")
        client._request.assert_called_once_with("/Users")


class PartialDashboardTests(unittest.TestCase):
    def test_jellyfin_dashboard_exposes_partial_section_failures(self):
        client = JellyfinClient("http://jellyfin.local", "key")
        client._server_identity = Mock(return_value=("主节点", "Jellyfin", "12.0.0"))
        client._user_id = Mock(return_value="user-id")

        def isolated(method_name, user_id):
            if method_name == "_libraries":
                raise RuntimeError("library timeout")
            return {
                "_recent_added": [],
                "_recent_played": [],
                "_total_items": 16,
                "_media_counts": {
                    "movie_count": 3,
                    "series_count": 8,
                    "episode_count": 13,
                },
                "_total_plays": 5,
            }[method_name]

        client._isolated_part = isolated
        board = client.get_dashboard()

        self.assertTrue(board.online)
        self.assertEqual(board.server_product, "Jellyfin")
        self.assertEqual(board.server_version, "12.0.0")
        self.assertEqual(board.total_items, 16)
        self.assertEqual(
            (board.movie_count, board.series_count, board.episode_count),
            (3, 8, 13),
        )
        self.assertEqual(board.partial_errors, ["libraries"])


if __name__ == "__main__":
    unittest.main()


class ExplicitContinueWatchingTests(unittest.TestCase):
    def _assert_explicit_resume_contract(self, client) -> None:
        calls = []

        def request(path, params=None):
            calls.append((path, dict(params or {})))
            return {
                "Items": [{
                    "Id": "item-1",
                    "Name": "第 7 集",
                    "SeriesName": "示例动画",
                    "Type": "Episode",
                    "ParentIndexNumber": 2,
                    "IndexNumber": 7,
                    "UserData": {"PlayedPercentage": 37.5},
                }]
            }

        client._request = request
        client._user_id = Mock(side_effect=AssertionError("不得回退枚举管理员用户"))
        items = client.continue_watching("explicit-user", limit=5)
        self.assertEqual(calls[0][0], "/Users/explicit-user/Items/Resume")
        self.assertEqual(calls[0][1]["Limit"], 5)
        self.assertEqual(items[0].display_name, "示例动画")
        self.assertEqual(items[0].progress, 37.5)
        client._user_id.assert_not_called()

    def test_jellyfin_uses_only_explicit_user(self):
        self._assert_explicit_resume_contract(
            JellyfinClient("http://jellyfin.local", "key")
        )

    def test_emby_uses_only_explicit_user(self):
        self._assert_explicit_resume_contract(
            EmbyClient("http://emby.local", "token")
        )

    def test_explicit_user_rejects_path_like_identifiers(self):
        for client in (
            JellyfinClient("http://jellyfin.local", "key"),
            EmbyClient("http://emby.local", "token"),
        ):
            for invalid in ("../admin", "user?admin=true", "user#fragment", "user%2fadmin"):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    client.continue_watching(invalid)
