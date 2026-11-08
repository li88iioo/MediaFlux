"""媒体分页不能因短页/空页而把尚未读取的上游总量当成完整清单。"""

from __future__ import annotations

import unittest
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient
from app.clients.base import MediaIdentityBatch
from app.modules.media_subscriptions import MediaSubscriptionService


class MediaInventoryCompletenessTests(unittest.TestCase):
    def test_one_batch_shares_identity_pages_across_service_calls_and_then_expires(self):
        from app.services import inspect_series_episode_sources

        rows = [self.item(n) for n in range(1, 2001)]
        def page(path, *, params, **kwargs):
            if params.get("HasTmdbId"):
                offset, limit = params["StartIndex"], params["Limit"]
                return {"Items": rows[offset:offset + limit], "TotalRecordCount": len(rows)}
            return {"Items": [], "TotalRecordCount": 0}
        def sources():
            return [("jellyfin", "Synthetic", "", self.client(JellyfinClient))]

        batch = MediaIdentityBatch(deadline_at=time.monotonic() + 30)
        with patch("app.services._configured_media_sources", side_effect=sources), \
                patch.object(JellyfinClient, "_request", side_effect=page) as request:
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda n: inspect_series_episode_sources(
                    "An alias", tmdb_id=str(n), identity_batch=batch,
                ), range(1, 15)))
            self.assertTrue(all(r[0]["status"] == "ready" and not r[0]["truncated"] for r in results))
            scans = [c for c in request.call_args_list if c.kwargs["params"].get("HasTmdbId")]
            self.assertEqual(len(scans), 4)  # 过去同样批次为 14 × 4。
            request.reset_mock()
            rows.clear()
            # 写前复查不携带批次快照；必须看见实时库存变化。
            fresh = inspect_series_episode_sources("An alias", tmdb_id="1")
            self.assertEqual(fresh[0]["status"], "not_found")
            self.assertTrue(any(c.kwargs["params"].get("HasTmdbId") for c in request.call_args_list))

    def test_identity_batch_separates_accounts_types_and_parent_scopes(self):
        batch = MediaIdentityBatch()
        client = self.client(JellyfinClient)
        with patch.object(client, "_request", return_value={"Items": [self.item(1)], "TotalRecordCount": 1}) as request:
            for token, kind, parent in (("a", "tv", ""), ("a", "movie", ""), ("a", "movie", "x"), ("b", "movie", "x")):
                client.token = token
                self.assertTrue(client.has_tmdb_media("1", kind, parent_id=parent, identity_batch=batch))
                self.assertFalse(client.has_tmdb_media("2", kind, parent_id=parent, identity_batch=batch))
            self.assertEqual(request.call_count, 4)

    def test_identity_batch_cancellation_and_deadline_apply_even_to_cached_results(self):
        client = self.client(JellyfinClient)
        stop = threading.Event()
        batch = MediaIdentityBatch(deadline_at=time.monotonic() + 30, cancelled=stop.is_set)
        with patch.object(client, "_request", return_value={"Items": [self.item(1)], "TotalRecordCount": 1}) as request:
            self.assertTrue(client.has_tmdb_media("1", "movie", identity_batch=batch))
            stop.set()
            with self.assertRaises(InterruptedError):
                client.has_tmdb_media("1", "movie", identity_batch=batch)
            stop.clear()
            batch.deadline_at = time.monotonic() - 1
            with self.assertRaises(TimeoutError):
                client.has_tmdb_media("1", "movie", identity_batch=batch)
            self.assertEqual(request.call_count, 1)

    def test_identity_batch_cancelled_between_pages_does_not_publish_partial_index(self):
        client = self.client(JellyfinClient)
        stop = threading.Event()
        batch = MediaIdentityBatch(cancelled=stop.is_set)
        def first_page(*args, **kwargs):
            stop.set()
            return {"Items": [self.item(n) for n in range(1, 501)], "TotalRecordCount": 1000}
        with patch.object(client, "_request", side_effect=first_page) as request:
            with self.assertRaises(InterruptedError):
                client.has_tmdb_media("1", "movie", identity_batch=batch)
            self.assertEqual(request.call_count, 1)
        stop.clear()
        with patch.object(client, "_request", return_value={"Items": [], "TotalRecordCount": 0}) as request:
            self.assertFalse(client.has_tmdb_media("1", "movie", identity_batch=batch))
            self.assertEqual(request.call_count, 1)

    @staticmethod
    def readers(client, *, cap=5):
        return {
            "series": lambda: client.list_library_series(max_series=cap, page_size=2),
            "identity": lambda: client.list_media_identity_inventory(
                "tv", max_items=cap, page_size=2
            ),
            "recommendations": lambda: client.list_recommendation_candidates(
                "viewer", max_items=cap, page_size=2
            ),
            "episodes": lambda: client.list_series_episode_inventory(
                "series", max_episodes=cap, page_size=2
            ),
        }

    def client(self, cls):
        client = cls("http://synthetic.invalid", "synthetic")
        client._cached_user_id = "viewer"
        self.addCleanup(client.close)
        return client

    @staticmethod
    def item(n):
        return {
            "Id": str(n),
            "Name": f"Show {n}",
            "Type": "Series",
            "ProviderIds": {"Tmdb": str(n)},
            "ParentIndexNumber": 1,
            "IndexNumber": n,
        }

    def test_short_empty_or_filtered_page_with_remaining_total_is_incomplete(self):
        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            for name, reader in self.readers(client).items():
                for items in ([], [self.item(1)], [self.item(1), None]):
                    with (
                        self.subTest(server=cls.__name__, operation=name, items=items),
                        patch.object(
                            client,
                            "_request",
                            return_value={"Items": items, "TotalRecordCount": 3},
                        ) as request,
                    ):
                        result = reader()
                        self.assertTrue(result.truncated)
                        self.assertEqual(result.total, 3)
                        self.assertEqual(request.call_count, 1)

    def test_later_empty_page_keeps_unread_total_visible(self):
        client = self.client(JellyfinClient)
        for name, reader in self.readers(client).items():
            pages = [
                {"Items": [self.item(1), self.item(2)], "TotalRecordCount": 3},
                {"Items": [], "TotalRecordCount": 3},
            ]
            with (
                self.subTest(operation=name),
                patch.object(client, "_request", side_effect=pages) as request,
            ):
                result = reader()
                self.assertTrue(result.truncated)
                self.assertEqual(
                    [
                        call.kwargs["params"]["StartIndex"]
                        for call in request.call_args_list
                    ],
                    [0, 2],
                )

    def test_valid_empty_complete_and_capped_results_keep_their_meaning(self):
        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            for cap, payload, truncated in (
                (5, {"Items": [], "TotalRecordCount": 0}, False),
                (
                    5,
                    {"Items": [self.item(1), self.item(2)], "TotalRecordCount": 2},
                    False,
                ),
                (1, {"Items": [self.item(1)], "TotalRecordCount": 2}, True),
            ):
                for name, reader in self.readers(client, cap=cap).items():
                    with (
                        self.subTest(server=cls.__name__, operation=name, cap=cap),
                        patch.object(client, "_request", return_value=payload),
                    ):
                        self.assertEqual(reader().truncated, truncated)

    def test_incomplete_episode_inventory_blocks_subscription_resource_search(self):
        client = self.client(JellyfinClient)
        from app.clients.base import SeriesCandidate
        from app.services import _series_inventory_source_result

        with patch.object(
            client,
            "_request",
            return_value={"Items": [self.item(1)], "TotalRecordCount": 3},
        ):
            source = _series_inventory_source_result(
                server_type="jellyfin",
                server_name="Synthetic",
                client=client,
                selected_items=[SeriesCandidate("series", "Synthetic", "2026", "1")],
                candidates=[],
                status="ready",
                mapping_status="mapped",
                max_episodes=2000,
                include_specials=False,
            )
        self.assertEqual(source["episodes"], [(1, 1)])
        self.assertEqual(source["local_total"], 3)
        ready, reason = MediaSubscriptionService._inventory_complete([source])
        self.assertFalse(ready)
        self.assertIn("未执行资源搜索", reason)

    def test_alias_fallback_preserves_partial_identity_scan_through_submission_guard(self):
        from app.services import _series_source_payload
        from app.modules.media_subscriptions import MediaSubscriptionError

        client = self.client(JellyfinClient)
        def request(path, *, params, **kwargs):
            if "SearchTerm" in params:
                return {"Items": [], "TotalRecordCount": 0}
            return {"Items": [self.item(2)], "TotalRecordCount": 3}

        with patch.object(client, "_request", side_effect=request):
            source = _series_source_payload("jellyfin", "Synthetic", client, "An alias", "1", 2000)
        self.assertEqual(source["status"], "not_found")
        self.assertTrue(source["truncated"])
        with patch("app.modules.media_subscriptions.inspect_series_episode_sources", return_value=[source]):
            with self.assertRaises(MediaSubscriptionError) as raised:
                asyncio.run(MediaSubscriptionService()._candidate_still_missing(
                    {"media_type": "tv", "tmdb_id": "1", "title": "An alias", "include_specials": False},
                    {"season": 1, "episode": 1},
                ))
        self.assertEqual(raised.exception.code, "inventory_unavailable")

    def test_repeated_item_ids_abort_inventory_before_becoming_complete(self):
        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            for name, reader in self.readers(client, cap=100).items():
                payload = {
                    "Items": [self.item(1), self.item(2)],
                    "TotalRecordCount": 100,
                }
                with (
                    self.subTest(server=cls.__name__, operation=name),
                    patch.object(client, "_request", return_value=payload) as request,
                ):
                    with self.assertRaisesRegex(ValueError, "重复"):
                        reader()
                    self.assertEqual(request.call_count, 2)

    def test_missing_or_invalid_total_continues_until_the_actual_last_page(self):
        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            for total in (None, True, -1, "invalid", 1):
                for name, reader in self.readers(client).items():
                    pages = [
                        {"Items": [self.item(1), self.item(2)], "TotalRecordCount": total},
                        {"Items": [self.item(3)]},
                    ]
                    with self.subTest(server=cls.__name__, operation=name, total=total), \
                            patch.object(client, "_request", side_effect=pages) as request:
                        result = reader()
                        self.assertEqual(result.total, 3)
                        self.assertFalse(result.truncated)
                        self.assertEqual(
                            [call.kwargs["params"]["StartIndex"] for call in request.call_args_list],
                            [0, 2],
                        )

    def test_unknown_total_at_cap_or_malformed_page_cannot_prove_completeness(self):
        client = self.client(JellyfinClient)
        for name, reader in self.readers(client, cap=2).items():
            for payload in (
                [self.item(1), self.item(2)],
                {"Items": [self.item(1), self.item(2)]},
                {"Items": [self.item(1), None]},
            ):
                with self.subTest(operation=name, payload=payload), \
                        patch.object(client, "_request", return_value=payload) as request:
                    self.assertTrue(reader().truncated)
                    self.assertEqual(request.call_count, 1)

    def test_batch_identity_check_finds_existing_media_beyond_unknown_first_page(self):
        from app.services import inspect_media_identity_batch

        client = self.client(JellyfinClient)
        pages = [
            {"Items": [self.item(n) for n in range(1, 201)]},
            {"Items": [self.item(201)]},
        ]
        with patch.object(client, "_request", side_effect=pages), patch(
            "app.services._configured_media_sources", return_value=[("jellyfin", "Synthetic", "", client)],
        ):
            result = inspect_media_identity_batch(
                [{"tmdb_id": "201", "media_type": "tv", "title": "Show 201"}],
            )[0]
        self.assertEqual(result["items"][0]["status"], "present")

    def test_inventory_ignores_collection_display_folding_on_every_page(self):
        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            def request(path, params, **kwargs):
                rows = [self.item(n) for n in range(1, 4)]
                if params.get("CollapseBoxSetItems") != "false":
                    rows = rows[:1]  # A complete display page is not complete inventory.
                start, limit = params["StartIndex"], params["Limit"]
                return {"Items": rows[start:start + limit], "TotalRecordCount": len(rows)}
            for name, reader in self.readers(client).items():
                with self.subTest(server=cls.__name__, operation=name), \
                        patch.object(client, "_request", side_effect=request) as calls:
                    result = reader()
                    self.assertEqual(result.total, 3)
                    self.assertFalse(result.truncated)
                    self.assertEqual([c.kwargs["params"]["StartIndex"] for c in calls.call_args_list], [0, 2])

    def test_batch_presence_does_not_recommend_a_hidden_collection_member_as_missing(self):
        from app.services import inspect_media_identity_batch

        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            def request(path, params, **kwargs):
                rows = [self.item(1)]
                if params.get("CollapseBoxSetItems") == "false":
                    rows.append({**self.item(2), "Type": "Movie"})
                return {"Items": rows, "TotalRecordCount": len(rows)}
            with self.subTest(server=cls.__name__), \
                    patch.object(client, "_request", side_effect=request), \
                    patch("app.services._configured_media_sources", return_value=[("synthetic", "Synthetic", "", client)]):
                source = inspect_media_identity_batch(
                    [{"tmdb_id": "2", "media_type": "movie", "title": "Show 2"}],
                )[0]
                self.assertEqual(source["items"][0]["status"], "present")

    def test_exact_identity_lookups_also_include_collection_members(self):
        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            def request(path, params, **kwargs):
                rows = [self.item(2)] if params.get("CollapseBoxSetItems") == "false" else []
                return {"Items": rows, "TotalRecordCount": len(rows)}
            with self.subTest(server=cls.__name__), patch.object(client, "_request", side_effect=request):
                self.assertTrue(client.has_tmdb_media("2", "movie", parent_id="movies"))
                result = client.find_series_candidates_by_tmdb("2")
                self.assertEqual([item.tmdb_id for item in result.candidates], ["2"])

    def test_exact_identity_does_not_trust_an_ignored_server_provider_filter(self):
        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            rows = [self.item(n) for n in range(1, 502)]
            def request(path, params, **kwargs):
                start, limit = params.get("StartIndex", 0), params["Limit"]
                return {"Items": rows[start:start + limit], "TotalRecordCount": len(rows)}
            with self.subTest(server=cls.__name__), patch.object(client, "_request", side_effect=request) as calls:
                self.assertTrue(client.has_tmdb_media("501", "movie"))
                self.assertEqual([c.kwargs["params"]["StartIndex"] for c in calls.call_args_list], [0, 500])
                calls.reset_mock()
                result = client.find_series_candidates_by_tmdb("501")
                self.assertEqual([c.tmdb_id for c in result.candidates], ["501"])
                self.assertEqual(result.total, 1)
                self.assertFalse(result.truncated)
                self.assertTrue(all("AnyProviderIdEquals" not in c.kwargs["params"] for c in calls.call_args_list))

    def test_incomplete_identity_inventory_cannot_prove_absence(self):
        client = self.client(JellyfinClient)
        with patch.object(client, "_inventory_items", return_value=([self.item(1)], 10001, True)), \
                patch.object(client, "_request", side_effect=AssertionError("must use shared inventory")):
            self.assertTrue(client.has_tmdb_media("1", "movie"))
            with self.assertRaisesRegex(ValueError, "不完整"):
                client.has_tmdb_media("2", "movie")
            result = client.find_series_candidates_by_tmdb("2")
            self.assertTrue(result.truncated)
            self.assertEqual(result.candidates, [])

    def test_unknown_short_search_page_keeps_filtered_candidate_incomplete(self):
        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            with self.subTest(server=cls.__name__), patch.object(
                client, "_request", return_value={"Items": [self.item(1), {"Id": "2"}]},
            ):
                result = client.search_series_candidates("Show", limit=6)
                self.assertEqual(len(result.candidates), 1)
                self.assertEqual(result.total, 2)
                self.assertTrue(result.truncated)

    def test_distinct_versions_of_the_same_episode_are_not_duplicate_items(self):
        client = self.client(JellyfinClient)
        first, second = self.item(1), self.item(2)
        second["IndexNumber"] = 1
        with patch.object(
            client,
            "_request",
            return_value={"Items": [first, second], "TotalRecordCount": 2},
        ):
            inventory = client.list_series_episode_inventory("series", page_size=2)
        self.assertEqual(inventory.episodes, [(1, 1)])
        self.assertEqual(inventory.total, 2)
        self.assertFalse(inventory.truncated)

    def test_multi_episode_files_expand_ranges_without_changing_record_pagination(self):
        for cls in (EmbyClient, JellyfinClient):
            client = self.client(cls)
            pages = [
                {"Items": [{**self.item(1), "IndexNumberEnd": 3},
                           {**self.item(2), "IndexNumber": 3, "IndexNumberEnd": 4}],
                 "TotalRecordCount": 3},
                {"Items": [{**self.item(3), "IndexNumber": 5, "IndexNumberEnd": None}],
                 "TotalRecordCount": 3},
            ]
            with self.subTest(server=cls.__name__), patch.object(client, "_request", side_effect=pages) as request:
                result = client.list_series_episode_inventory("series", page_size=2)
            self.assertEqual(result.episodes, [(1, n) for n in range(1, 6)])
            self.assertEqual(result.total, 3)  # 服务器文件记录数不等于逻辑集数。
            self.assertFalse(result.truncated)
            self.assertEqual(result.ignored_unknown, 0)
            self.assertEqual([call.kwargs["params"]["StartIndex"] for call in request.call_args_list], [0, 2])

    def test_multi_episode_specials_respect_explicit_scope(self):
        client = self.client(JellyfinClient)
        payload = {"Items": [{**self.item(1), "ParentIndexNumber": 0, "IndexNumberEnd": 3}],
                   "TotalRecordCount": 1}
        for include in (False, True):
            with self.subTest(include=include), patch.object(client, "_request", return_value=payload):
                result = client.list_series_episode_inventory("series", include_specials=include)
            self.assertEqual(result.episodes, [(0, n) for n in range(1, 4)] if include else [])
            self.assertEqual(result.ignored_specials, 0 if include else 1)

    def test_episode_ranges_are_bounded_without_claiming_full_inventory(self):
        client = self.client(JellyfinClient)
        for end in (3, 4, 10**20):
            with self.subTest(end=end), patch.object(client, "_request", return_value={
                "Items": [{**self.item(1), "IndexNumberEnd": end}], "TotalRecordCount": 1,
            }):
                result = client.list_series_episode_inventory("series", max_episodes=3)
            self.assertEqual(result.episodes, [(1, n) for n in range(1, 4)])
            self.assertEqual(result.truncated, end > 3)

    def test_excluded_specials_do_not_make_normal_episode_inventory_unknown(self):
        client = self.client(JellyfinClient)
        for end in (0, True, 2.5, "bad"):
            for include in (False, True):
                with self.subTest(end=end, include=include), patch.object(client, "_request", return_value={
                    "Items": [{**self.item(1), "ParentIndexNumber": 0, "IndexNumberEnd": end}],
                    "TotalRecordCount": 1,
                }):
                    result = client.list_series_episode_inventory("series", include_specials=include)
                self.assertEqual(result.episodes, [])
                self.assertEqual(result.ignored_unknown, int(include))
                self.assertEqual(result.ignored_specials, int(not include))

    def test_invalid_episode_ranges_are_unknown_not_coerced_into_present_episodes(self):
        client = self.client(JellyfinClient)
        for fields in ({"IndexNumberEnd": 0}, {"IndexNumberEnd": True}, {"IndexNumberEnd": 2.5},
                       {"IndexNumberEnd": "bad"}, {"IndexNumber": True}, {"ParentIndexNumber": 1.5}):
            with self.subTest(fields=fields), patch.object(client, "_request", return_value={
                "Items": [{**self.item(1), **fields}], "TotalRecordCount": 1,
            }):
                result = client.list_series_episode_inventory("series")
            self.assertEqual(result.episodes, [])
            self.assertEqual(result.ignored_unknown, 1)

    def test_episode_range_reaches_native_count_and_subscription_completeness(self):
        from app.sdk import library
        from app.clients.base import SeriesCandidate
        from app.services import _series_inventory_source_result

        client = self.client(JellyfinClient)
        for end in (3, "bad"):
            with self.subTest(end=end), patch.object(client, "_request", return_value={
                "Items": [{**self.item(1), "IndexNumberEnd": end}], "TotalRecordCount": 1,
            }):
                source = _series_inventory_source_result(
                    server_type="jellyfin", server_name="Synthetic", client=client,
                    selected_items=[SeriesCandidate("series", "Synthetic", "2026", "1")],
                    candidates=[], status="ready", mapping_status="mapped", max_episodes=2000,
                    include_specials=False,
                )
            with patch("app.services.inspect_series_episode_sources", return_value=[source]):
                result = library.episodes("Synthetic", tmdb_id="1")
            self.assertEqual(result["data"]["local_episode_count"], 3 if end == 3 else 0)
            self.assertEqual(result["data"]["inventory_complete"], end == 3)
            self.assertEqual(MediaSubscriptionService._inventory_complete([source])[0], end == 3)
