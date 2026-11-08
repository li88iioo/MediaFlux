"""Media Agent 媒体更新核对的语义、路由与 API 安全测试。"""

from __future__ import annotations

import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from app.modules.episode_audit import EpisodeAuditResult, check_library_updates, normalize_library_update_arguments


def _identity(arguments):
    return dict(arguments)


def _audit_result(*, status: str = "updates_available") -> EpisodeAuditResult:
    return EpisodeAuditResult(
        ok=status in {"updates_available", "up_to_date"},
        status=status,
        summary="发现 1 集已播但本地尚未收录"
        if status == "updates_available"
        else "未找到",
        data={"query": "黑镜", "missing_count": 1},
        checked_at="2026-08-01T10:00:00+08:00",
    )


def _query_audit_result(query: str, *, status: str) -> EpisodeAuditResult:
    """为批量核对提供完整、可扁平化的单项审计 DTO。"""
    missing = (
        [{"season": 1, "episode": 2}]
        if status == "updates_available"
        else []
    )
    return EpisodeAuditResult(
        ok=status in {"updates_available", "up_to_date", "comparison_unavailable"},
        status=status,
        summary={
            "updates_available": "发现 1 集已播但本地尚未收录",
            "up_to_date": "截至指定日期，已播普通剧集均已收录",
            "ambiguous": "媒体库命中多个同名剧集，暂不做猜测",
            "unavailable": "媒体服务器暂时不可用，无法完成剧集审计",
            "comparison_unavailable": "本地条目存在，但版本比较不可用",
        }[status],
        data={
            "query": query,
            "title": query,
            "tmdb_id": "12345" if status in {"updates_available", "up_to_date"} else "",
            "local_episode_count": 2 if status != "unavailable" else None,
            "expected_aired": 3 if status != "unavailable" else None,
            "missing_count": len(missing) if status != "unavailable" else None,
            "latest_local": {"season": 1, "episode": 3}
            if status != "unavailable"
            else None,
            "latest_aired": {"season": 1, "episode": 3}
            if status != "unavailable"
            else None,
            "missing_sample": missing,
            "missing_sample_truncated": False,
        },
    )


class LibraryUpdateArgumentTests(unittest.TestCase):
    def test_single_or_large_batch_queries_are_normalized_without_changing_order(self):
        single = normalize_library_update_arguments(
            {
                "query": "  黑镜  ",
                "media_type": "tv",
                "as_of": "2026-08-01",
            }
        )
        self.assertEqual(single["query"], "黑镜")
        self.assertNotIn("queries", single)
        self.assertEqual(single["media_type"], "tv")
        self.assertEqual(single["as_of"], "2026-08-01")
        self.assertTrue(single["refresh"])

        queries = ["  合成剧一  ", "合成剧二", "合成剧一", "合成剧三"]
        batch = normalize_library_update_arguments(
            {
                "queries": queries,
                "media_type": "auto",
                "as_of": "2026-08-01",
            }
        )
        self.assertEqual(batch["queries"], ["合成剧一", "合成剧二", "合成剧三"])
        self.assertNotIn("query", batch)
        self.assertEqual(batch["media_type"], "auto")
        self.assertEqual(batch["as_of"], "2026-08-01")
        self.assertTrue(batch["refresh"])

        twenty = [f"边界剧集 {index:02d}" for index in range(31)]
        normalized = normalize_library_update_arguments(
            {"queries": twenty, "media_type": "tv", "as_of": "2026-08-01"}
        )
        self.assertEqual(normalized["queries"], twenty)

    def test_update_argument_boundaries_types_refresh_and_mutual_exclusion(self):
        self.assertFalse(
            normalize_library_update_arguments(
                {
                    "query": "黑镜",
                    "media_type": "tv",
                    "as_of": "2026-08-01",
                    "refresh": False,
                }
            )["refresh"]
        )

        invalid_arguments = [
            {"queries": []},
            {"queries": "不是列表"},
            {"queries": ["有效", 2]},
            {"queries": [""]},
            {"query": ""},
            {"query": ["不是字符串"]},
            {"query": "单项", "queries": ["批量"]},
            {"queries": ["批量"], "tmdb_id": "12345"},
            {"queries": ["批量"], "season": 1},
        ]
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                normalize_library_update_arguments(arguments)

        for invalid_refresh in (0, 1, "true", "false", None, []):
            with self.subTest(refresh=invalid_refresh), self.assertRaises(ValueError):
                normalize_library_update_arguments(
                    {"query": "黑镜", "refresh": invalid_refresh}
                )


class LibraryUpdateActionTests(unittest.TestCase):
    def test_movie_title_checks_library_presence_and_offers_safe_resource_followup(
        self,
    ):
        arguments = {
            "query": "沙丘2",
            "media_type": "movie",
            "tmdb_id": "",
            "season": None,
            "as_of": "2026-08-01",
        }
        sources = [
            {
                "server_type": "jellyfin",
                "server_name": "客厅媒体库" + "媒体资料" * 30,
                "web_url": "http://private.invalid",
                "items": [
                    SimpleNamespace(
                        type="Movie", name="沙丘 2", display_name="沙丘 2", year="2024"
                    ),
                    SimpleNamespace(
                        type="Episode", name="沙丘2", display_name="沙丘2", year="2024"
                    ),
                    SimpleNamespace(
                        type="Movie",
                        name="沙丘：第二部" + "电影长标题" * 40,
                        display_name="沙丘：第二部" + "电影长标题" * 40,
                        year="2024",
                    ),
                ],
                "error": "",
                "has_more": False,
            }
        ]
        with (
            patch("app.modules.episode_audit.audit_series_episodes") as audit,
            patch(
                "app.modules.episode_audit.search_media_servers", return_value=sources
            ) as search,
        ):
            result = check_library_updates(arguments)
        audit.assert_not_called()
        search.assert_called_once_with("沙丘2", limit=50, media_type="movie", search_by="title", cancelled=None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "comparison_unavailable")
        self.assertEqual(result["data"]["media_type"], "movie")
        self.assertEqual(result["data"]["local_match_status"], "found")
        self.assertEqual(result["data"]["exact_match_count"], 1)
        self.assertEqual(result["data"]["possible_match_count"], 1)
        self.assertNotIn("web_url", result["data"]["sources"][0])
        self.assertEqual(result["data"]["sources"][0]["server_name"], sources[0]["server_name"])
        self.assertEqual(result["data"]["sources"][0]["items"][1]["title"], "沙丘：第二部" + "电影长标题" * 40)
        self.assertFalse(result["data"]["comparison"]["available"])

    def test_movie_presence_is_honest_for_possible_missing_and_unavailable_sources(
        self,
    ):
        possible = [
            {
                "server_type": "emby",
                "server_name": "Emby",
                "items": [
                    SimpleNamespace(
                        type="Movie",
                        name="Dune Part Two",
                        display_name="Dune Part Two",
                        year="2024",
                    )
                ],
                "error": "",
                "has_more": False,
            }
        ]
        with patch(
            "app.modules.episode_audit.search_media_servers", return_value=possible
        ):
            result = check_library_updates(
                {
                    "query": "沙丘2",
                    "media_type": "movie",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["data"]["local_match_status"], "possible")
        self.assertEqual(result["data"]["exact_match_count"], 0)
        with patch(
            "app.modules.episode_audit.search_media_servers",
            return_value=[
                {
                    "server_type": "jellyfin",
                    "server_name": "Jellyfin",
                    "items": [],
                    "error": "",
                    "has_more": False,
                }
            ],
        ):
            result = check_library_updates(
                {
                    "query": "沙丘2",
                    "media_type": "movie",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["data"]["local_match_status"], "not_found")
        with patch(
            "app.modules.episode_audit.search_media_servers",
            side_effect=RuntimeError("secret"),
        ):
            result = check_library_updates(
                {
                    "query": "沙丘2",
                    "media_type": "movie",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("secret", result["summary"])

    def test_movie_presence_keeps_all_match_rows_across_all_servers(self):
        sources = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "items": [
                    SimpleNamespace(
                        type="Movie",
                        name=f"候选 {index}",
                        display_name=f"候选 {index}",
                        year="2024",
                    )
                    for index in range(8)
                ],
                "error": "",
                "has_more": False,
            },
            {
                "server_type": "emby",
                "server_name": "Emby",
                "items": [
                    SimpleNamespace(
                        type="Movie",
                        name="额外候选",
                        display_name="额外候选",
                        year="2025",
                    )
                ],
                "error": "",
                "has_more": False,
            },
        ]
        with patch(
            "app.modules.episode_audit.search_media_servers", return_value=sources
        ):
            result = check_library_updates(
                {
                    "query": "不存在的精确片名",
                    "media_type": "movie",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertEqual(result["data"]["possible_match_count"], 9)
        self.assertEqual(result["data"]["matches_truncated"], 0)
        self.assertEqual(
            sum(source["returned"] for source in result["data"]["sources"]), 9
        )
        self.assertEqual(result["data"]["sources"][1]["returned"], 1)

    def test_movie_presence_does_not_claim_not_found_when_source_hits_search_cap(self):
        sources = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "items": [
                    SimpleNamespace(
                        type="Episode",
                        name=f"同名剧集 {index}",
                        display_name=f"同名剧集 {index}",
                        year="2024",
                    )
                    for index in range(50)
                ],
                "error": "",
                "has_more": True,
            }
        ]
        with patch(
            "app.modules.episode_audit.search_media_servers", return_value=sources
        ):
            result = check_library_updates(
                {
                    "query": "目标电影",
                    "media_type": "movie",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["data"]["local_match_status"], "indeterminate")
        self.assertEqual(result["data"]["search_truncated_server_count"], 1)
        self.assertNotIn("未找到同名电影", result["summary"])

    def test_movie_presence_keeps_later_servers_exact_matches_without_clipping(self):
        sources = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "items": [
                    SimpleNamespace(
                        type="Movie",
                        name=f"候选 {index}",
                        display_name=f"候选 {index}",
                        year="2024",
                    )
                    for index in range(8)
                ],
                "error": "",
                "has_more": False,
            },
            {
                "server_type": "emby",
                "server_name": "Emby",
                "items": [
                    SimpleNamespace(
                        type="Movie", name="沙丘 2", display_name="沙丘 2", year="2024"
                    )
                ],
                "error": "",
                "has_more": False,
            },
        ]
        with patch(
            "app.modules.episode_audit.search_media_servers", return_value=sources
        ):
            result = check_library_updates(
                {
                    "query": "沙丘2",
                    "media_type": "movie",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertEqual(result["status"], "comparison_unavailable")
        self.assertEqual(result["data"]["local_match_status"], "found")
        self.assertEqual(result["data"]["exact_match_count"], 1)
        self.assertEqual(result["data"]["possible_match_count"], 8)
        self.assertEqual(
            sum(source["returned"] for source in result["data"]["sources"]), 9
        )
        self.assertEqual(result["data"]["sources"][1]["items"][0]["match"], "exact_title")

    def test_tv_delegates_to_episode_audit_and_labels_definition(self):
        with patch(
            "app.modules.episode_audit.audit_series_episodes",
            return_value=_audit_result(),
        ) as audit:
            result = check_library_updates(
                {
                    "query": "黑镜",
                    "media_type": "tv",
                    "tmdb_id": "42009",
                    "season": 7,
                    "as_of": "2026-08-01",
                }
            )
        audit.assert_called_once_with(
            {"query": "黑镜", "tmdb_id": "42009", "season": 7, "as_of": "2026-08-01"},
            refresh=True,
        )
        self.assertEqual(result["status"], "updates_available")
        self.assertEqual(result["data"]["media_type"], "tv")
        self.assertEqual(
            result["data"]["check_definition"],
            "aired_normal_episodes_missing_from_enabled_media_servers",
        )

    def test_tv_forwards_explicit_refresh_policy_to_shared_audit(self):
        arguments = {"query": "Fixture", "tmdb_id": "7", "season": 1, "as_of": "2020-02-01"}
        for refresh in (True, False):
            with self.subTest(refresh=refresh), patch(
                "app.modules.episode_audit.audit_series_episodes", return_value=_audit_result(),
            ) as audit:
                check_library_updates({**arguments, "media_type": "tv", "refresh": refresh})
                audit.assert_called_once_with(arguments, refresh=refresh)

    def test_auto_not_found_does_not_claim_movie_or_series_is_current(self):
        with patch(
            "app.modules.episode_audit.audit_series_episodes",
            return_value=_audit_result(status="not_found"),
        ):
            result = check_library_updates(
                {
                    "query": "未知标题",
                    "media_type": "auto",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "cannot_determine")
        self.assertIn("无法可靠判断", result["summary"])

    def test_batch_checks_ten_queries_in_order_with_bounded_concurrency_and_safe_errors(self):
        titles = [f"合成剧集 {index:02d}" for index in range(1, 11)]
        status_by_query = {
            titles[0]: "updates_available",
            titles[1]: "updates_available",
            titles[2]: "updates_available",
            titles[3]: "up_to_date",
            titles[4]: "up_to_date",
            titles[5]: "up_to_date",
            titles[6]: "ambiguous",
            titles[7]: "ambiguous",
            titles[8]: "unavailable",
        }
        state_lock = threading.Lock()
        seen_queries: list[str] = []
        active = 0
        max_active = 0

        def audit_side_effect(arguments, *, refresh):
            nonlocal active, max_active
            self.assertFalse(refresh)
            query = arguments["query"]
            with state_lock:
                seen_queries.append(query)
                active += 1
                max_active = max(max_active, active)
            try:
                # 给并发窗口留出确定的观测时间；不访问真实媒体库或资源站。
                time.sleep(0.01)
                if query == titles[-1]:
                    raise RuntimeError("secret token at http://user:pass@private.invalid")
                return _query_audit_result(query, status=status_by_query[query])
            finally:
                with state_lock:
                    active -= 1

        with patch(
            "app.modules.episode_audit.audit_series_episodes",
            side_effect=audit_side_effect,
        ) as audit:
            result = check_library_updates(
                {
                    "queries": titles,
                    "media_type": "tv",
                    "as_of": "2026-08-01",
                    "refresh": False,
                }
            )

        self.assertEqual(audit.call_count, 10)
        self.assertCountEqual(seen_queries, titles)
        self.assertGreaterEqual(max_active, 2)
        self.assertLessEqual(max_active, 3)
        self.assertTrue(result["ok"], "只要至少一个单项可判定，批量 ok 应取 any(single.ok)")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["as_of"], "2026-08-01")
        assert isinstance(result["data"]["checked_at"], str)
        self.assertTrue(result["data"]["checked_at"])
        self.assertEqual(
            result["data"]["check_definition"],
            "library_inventory_vs_tmdb_not_resource_release_availability",
        )
        self.assertEqual(
            result["data"]["counts"],
            {
                "requested": 10,
                "updates_available": 3,
                "up_to_date": 3,
                "uncertain": 4,
            },
        )

        items = result["data"]["items"]
        self.assertEqual([item["query"] for item in items], titles)
        self.assertEqual(
            [item["status"] for item in items[:-1]],
            [status_by_query[query] for query in titles[:-1]],
        )
        required_item_keys = {
            "query",
            "title",
            "status",
            "ok",
            "summary",
            "tmdb_id",
            "local_episode_count",
            "expected_aired",
            "missing_count",
            "latest_local",
            "latest_aired",
            "missing_sample",
        }
        for item in items:
            with self.subTest(query=item["query"]):
                self.assertTrue(required_item_keys <= item.keys())
        failed = items[-1]
        self.assertFalse(failed["ok"])
        self.assertIsNone(failed["missing_count"])
        self.assertIsNone(failed["local_episode_count"])
        self.assertNotIn(failed["status"], {"updates_available", "up_to_date"})
        public_result = str(result)
        self.assertNotIn("secret token", public_result)
        self.assertNotIn("private.invalid", public_result)
        self.assertNotIn("user:pass", public_result)

    def test_batch_status_is_success_when_every_item_is_determinable(self):
        titles = ["可判定剧一", "可判定剧二"]
        with patch(
            "app.modules.episode_audit.audit_series_episodes",
            side_effect=lambda arguments, *, refresh: _query_audit_result(
                arguments["query"], status="up_to_date"
            ),
        ):
            result = check_library_updates(
                {
                    "queries": titles,
                    "media_type": "tv",
                    "as_of": "2026-08-01",
                    "refresh": False,
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["counts"]["uncertain"], 0)
        self.assertEqual(
            [item["status"] for item in result["data"]["items"]],
            ["up_to_date", "up_to_date"],
        )

    def test_batch_preserves_full_missing_sample_and_default_projector_keeps_twenty_items(self):
        titles = [f"投影剧集 {index:02d}" for index in range(1, 21)]
        all_missing = [{"season": 1, "episode": episode} for episode in range(1, 8)]

        def audit_side_effect(arguments, *, refresh):
            self.assertFalse(refresh)
            result = _query_audit_result(
                arguments["query"], status="updates_available"
            )
            result.data.update(
                {
                    "local_episode_count": 0,
                    "expected_aired": 7,
                    "missing_count": 7,
                    "latest_local": None,
                    "latest_aired": {"season": 1, "episode": 7},
                    "missing_sample": list(all_missing),
                    "missing_sample_truncated": False,
                }
            )
            return result

        with patch(
            "app.modules.episode_audit.audit_series_episodes",
            side_effect=audit_side_effect,
        ):
            result = check_library_updates(
                {
                    "queries": titles,
                    "media_type": "tv",
                    "as_of": "2026-08-01",
                    "refresh": False,
                }
            )

        items = result["data"]["items"]
        self.assertEqual(len(items), 20)
        self.assertEqual([item["query"] for item in items], titles)
        for item in items:
            with self.subTest(query=item["query"]):
                self.assertEqual(item["missing_sample"], all_missing)
                self.assertEqual(len(item["missing_sample"]), item["missing_count"])
                self.assertFalse(item["missing_sample_truncated"])

        projected = json.loads(json.dumps(result))
        self.assertEqual(len(projected["data"]["items"]), 20)
        self.assertEqual([item["query"] for item in projected["data"]["items"]], titles)
        for item in projected["data"]["items"]:
            self.assertEqual(item["missing_sample"], all_missing)
            self.assertFalse(item["missing_sample_truncated"])

    def test_batch_preserves_upstream_missing_sample_and_truncation_flag(self):
        missing = [{"season": 1, "episode": episode} for episode in range(2, 24, 2)]
        for truncated in (False, True):
            with self.subTest(truncated=truncated):
                scalar = _query_audit_result("示例剧", status="updates_available")
                scalar.data.update(
                    missing_count=len(missing),
                    missing_sample=list(missing),
                    missing_sample_truncated=truncated,
                )
                with patch("app.modules.episode_audit.audit_series_episodes", return_value=scalar):
                    result = check_library_updates(normalize_library_update_arguments({
                        "queries": ["示例剧"], "as_of": "2026-08-01",
                    }))
                model = json.loads(json.dumps(result))
                for data in (result["data"], model["data"]):
                    item = data["items"][0]
                    self.assertEqual(item["missing_sample"], missing)
                    self.assertIs(item["missing_sample_truncated"], truncated)
                self.assertEqual(scalar.data["missing_sample"], missing)
                self.assertIs(scalar.data["missing_sample_truncated"], truncated)

    def test_long_titles_and_all_facts_survive_full_json_roundtrip(self):
        titles = [f"第{i:02d}部" + "长标题样本" * 23 for i in range(20)]
        sources = [{"server_type": "jellyfin", "server_name": f"服务器{i}" + "媒体资料" * 30,
                    "library_name": "电视剧", "status": "ready", "truncated": False,
                    "selected": {"tmdb_id": "12345", "name": "完整作品身份"},
                    "mapping": {"status": "provider_id"}, "ignored_unknown": 2,
                    "ignored_specials": 1, "local_episode_count": 3} for i in range(3)]
        def audit(arguments, *, refresh):
            self.assertTrue(refresh)
            audit_result = _query_audit_result(arguments["query"], status="updates_available")
            audit_result.summary = "存在已播缺集，请结合实际媒体库版本及季集编号核对。" * 6
            audit_result.data["title"] = arguments["query"] + "完整元数据标题" * 30
            audit_result.data["sources"] = sources
            audit_result.data["missing_sample"] = [{"season": 1, "episode": n} for n in range(3, 8)]
            return audit_result
        with patch("app.modules.episode_audit.audit_series_episodes", side_effect=audit):
            result = check_library_updates({"queries": titles, "as_of": "2026-08-01"})
        projected = json.dumps(result, ensure_ascii=False)
        self.assertGreater(len(projected), 24_000)
        model = json.loads(projected)
        self.assertEqual([item["query"] for item in model["data"]["items"]], titles)
        self.assertEqual([item["title"] for item in model["data"]["items"]],
                         [title + "完整元数据标题" * 30 for title in titles])
        self.assertTrue(all(item["latest_local"] for item in model["data"]["items"]))
        self.assertFalse(model.get("truncated"))
        for item in model["data"]["items"]:
            self.assertEqual(item["sources"], sources)

    def test_batch_preserves_unknown_metadata_counts_in_public_and_model_evidence(self):
        scalar = _query_audit_result("示例剧", status="up_to_date")
        scalar.data.update(unknown_air_date_count=1, ignored_unknown_local=2)
        with patch("app.modules.episode_audit.audit_series_episodes", return_value=scalar):
            result = check_library_updates(normalize_library_update_arguments({"queries": ["示例剧"], "as_of": "2026-08-01"}))
        model = json.loads(json.dumps(result))
        for data in (result["data"], model["data"]):
            self.assertEqual(data["items"][0]["unknown_air_date_count"], 1)
            self.assertEqual(data["items"][0]["ignored_unknown_local"], 2)
            self.assertEqual(data["items"][0]["status"], "up_to_date")
        self.assertIn("不能宣称全部最新", " ".join(result["suggestions"]))

    def test_cancelled_public_batch_keeps_completed_facts_and_stops_queued_reads(self):
        from concurrent.futures import ThreadPoolExecutor
        names = [f"取消测试作品{i}" for i in range(31)]
        started, release, stopped = threading.Event(), threading.Event(), threading.Event()
        reads, lock = [], threading.Lock()
        def audit(arguments, *, refresh):
            with lock:
                reads.append(arguments["query"])
                if len(reads) == 3:
                    started.set()
            assert release.wait(3)
            return _query_audit_result(arguments["query"], status="up_to_date")
        with patch("app.modules.episode_audit.audit_series_episodes", side_effect=audit), ThreadPoolExecutor(1) as pool:
            pending = pool.submit(check_library_updates, {"queries": names}, cancelled=stopped.is_set)
            try:
                self.assertTrue(started.wait(3))
                stopped.set()
            finally:
                release.set()
            result = pending.result(3)
        self.assertEqual(len(reads), 3)
        self.assertIn("已核对 3 部", result["summary"])
        rows = result["data"]["items"]
        self.assertEqual([row["query"] for row in rows], names)
        self.assertEqual(sum(row["status"] == "up_to_date" for row in rows), 3)
        self.assertEqual(sum(row["status"] == "cancelled" for row in rows), 28)
        self.assertTrue(all(row["missing_count"] is None for row in rows if row["status"] == "cancelled"))


    def test_movie_comparison_unavailable_counts_as_uncertain_in_batch(self):
        titles = ["电影甲", "电影乙"]

        def movie_sources(query, *, limit, media_type, search_by, cancelled):
            self.assertEqual(limit, 50)
            self.assertEqual(media_type, "movie")
            self.assertEqual(search_by, "title")
            return [
                {
                    "server_type": "jellyfin",
                    "server_name": "测试媒体库",
                    "items": [
                        SimpleNamespace(
                            type="Movie",
                            name=query,
                            display_name=query,
                            year="2024",
                        )
                    ],
                    "error": "",
                    "has_more": False,
                }
            ]

        with patch(
            "app.modules.episode_audit.search_media_servers",
            side_effect=movie_sources,
        ):
            result = check_library_updates(
                {
                    "queries": titles,
                    "media_type": "movie",
                    "as_of": "2026-08-01",
                    "refresh": False,
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["counts"]["uncertain"], 2)
        self.assertEqual(
            [item["status"] for item in result["data"]["items"]],
            ["comparison_unavailable", "comparison_unavailable"],
        )


def test_sdk_updates_uses_existing_cancellation_and_restores_nested_owner():
    from app import sdk

    stopped = threading.Event()
    stopped.set()
    with patch("app.modules.episode_audit.audit_series_episodes", return_value=_audit_result()) as audit:
        with sdk.call_owner("outer", cancelled=stopped.is_set):
            assert sdk.library.updates(query="示例剧")["status"] == "cancelled"
            audit.assert_not_called()
            with sdk.call_owner("inner", cancelled=lambda: False):
                assert sdk.library.updates(query="示例剧")["status"] == "updates_available"
            assert sdk.library.updates(query="示例剧")["status"] == "cancelled"
        assert sdk.library.updates(query="示例剧")["status"] == "updates_available"
        assert audit.call_count == 2


# 同一公共查询经真实工作区 RPC；host 无模板时跳过，root 验收必须实际运行。
import asyncio

import pytest

from app.agent.kernel import sandbox


@pytest.fixture
def native_updates_runtime(tmp_path, monkeypatch):
    if not sandbox.sandbox_supported():
        pytest.skip("需要已构建的 Linux root 工作区模板")
    from app.agent.kernel.capabilities import ToolCatalog
    from app.agent.kernel.pipeline import ToolPipeline
    from app.agent.kernel.state import InMemorySessionStateStore
    from app.agent.kernel.state_types import CancellationToken
    from app.agent.kernel.tool_calls import ToolCallContext

    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
    async def make():
        store = InMemorySessionStateStore()
        lease, _ = await store.begin_turn(owner="audit-native", session_id="updates", request_id="read")
        async def progress(_):
            pass
        context = ToolCallContext(owner=lease.owner, session_id=lease.session_id, request_id=lease.request_id,
                                  turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=progress)
        catalog = ToolCatalog()
        pipeline = ToolPipeline(catalog=catalog, state_store=store)
        catalog.register(sandbox.run_code_tool(pipeline))
        root, _ = sandbox._workspace_location(context.owner, context.session_id)
        return pipeline, context, root
    yield make
    sandbox.clear_workspace(owner="audit-native", session_id="updates")


def test_native_updates_movie_conditions_large_batch_tail_and_partial(native_updates_runtime, monkeypatch):
    from app.modules import episode_audit
    from tests.test_media_episode_audit import _FakeTMDB, _ready

    reads = []
    def inspect(query, **_):
        reads.append(query)
        sources = [_ready([(1, 1)])]
        if query == "Show30":
            sources.append({**_ready([], server="Emby"), "status": "unavailable", "selected": None})
        return sources
    monkeypatch.setattr(episode_audit, "inspect_series_episode_sources", inspect)
    monkeypatch.setattr(episode_audit, "TMDBClient", _FakeTMDB)
    movie_search = Mock(return_value=[
        {"server_type": "jellyfin", "server_name": "Jellyfin", "items": [
            SimpleNamespace(type="Movie", name="Movie Title", year="2026"),
            SimpleNamespace(type="Episode", name="Movie Title", year="2026"),
        ]},
        {"server_type": "emby", "server_name": "Emby", "items": [], "error": "private source error"},
    ])
    monkeypatch.setattr(episode_audit, "search_media_servers", movie_search)
    episode_audit.reset_episode_audit_cache_for_tests()
    async def run():
        pipeline, context, root = await native_updates_runtime()
        code = '''from pathlib import Path
import json
from mf import library
try:
    library.updates(queries=["Valid", 7])
    raise AssertionError("invalid input accepted")
except RuntimeError as exc:
    assert exc.code == "ValueError"
movie = library.updates(query="Movie Title", media_type="movie")
assert movie["status"] == "partial" and movie["data"]["exact_match_count"] == 1
assert movie["data"]["local_match_status"] == "found" and not movie["data"]["comparison"]["available"]
result = library.updates(queries=[f"Show{i}" for i in range(31)], media_type="tv", as_of="2026-08-01")
assert len(result["data"]["items"]) == 31 and result["data"]["items"][-1]["query"] == "Show30"
assert result["status"] == "partial" and result["data"]["items"][-1]["status"] == "inconclusive"
Path("full-updates.json").write_text(json.dumps(result))
print("31 complete rows saved")'''
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code}, context=context)
        assert result.outcome.public_content["data"]["exit_code"] == 0, result.outcome.public_content
        saved = json.loads((root / "work/full-updates.json").read_text())
        assert saved["data"]["counts"] == {"requested": 31, "updates_available": 30, "up_to_date": 0, "uncertain": 1}
        assert len(saved["data"]["items"][-1]["missing_sample"]) == 2
        assert "private source error" not in (root / "work/full-updates.json").read_text()
        archived = [json.loads(path.read_text()) for path in (root / "output").glob("*.sdk.json")]
        batch = next(row["result"] for row in archived if row.get("arguments", {}).get("queries") == [f"Show{i}" for i in range(31)])
        assert batch == saved and len(json.dumps(saved)) > 24_000
        later = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": 'import json\nr=json.load(open("full-updates.json"))\nassert r["data"]["items"][-1]["query"]=="Show30"\nprint(len(r["data"]["items"]))'}, context=context)
        assert later.outcome.public_content["data"]["exit_code"] == 0
    with patch("socket.socket.connect", side_effect=AssertionError("external network forbidden")):
        asyncio.run(run())
    movie_search.assert_called_once_with("Movie Title", limit=50, media_type="movie", search_by="title", cancelled=ANY)
    assert sorted(reads) == sorted(f"Show{i}" for i in range(31))


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_native_updates_cancel_archives_only_completed_batch_facts(native_updates_runtime, monkeypatch, stop):
    from app.modules import episode_audit
    from tests.test_media_episode_audit import _FakeTMDB, _ready

    checkpointed, release, finished = threading.Event(), threading.Event(), threading.Event()
    reads, lock = [], threading.Lock()
    def inspect(query, **_):
        with lock:
            reads.append(query)
        if query != "Fast":
            assert release.wait(5)
        return [_ready([(1, 1)])]
    original = episode_audit.check_library_updates
    def updates(arguments, *, cancelled=None, checkpoint=None):
        def observed(payload):
            assert checkpoint is not None
            checkpoint(payload)
            if payload["data"]["items"][0]["status"] == "updates_available":
                checkpointed.set()
        try:
            return original(arguments, cancelled=cancelled, checkpoint=observed)
        finally:
            finished.set()
    monkeypatch.setattr(episode_audit, "inspect_series_episode_sources", inspect)
    monkeypatch.setattr(episode_audit, "TMDBClient", _FakeTMDB)
    monkeypatch.setattr(episode_audit, "check_library_updates", updates)
    episode_audit.reset_episode_audit_cache_for_tests()
    async def run():
        pipeline, context, root = await native_updates_runtime()
        task = asyncio.create_task(pipeline.execute(sandbox.RUN_CODE_TOOL, {"timeout_seconds": 2, "code":
            'from mf import library\nlibrary.updates(queries=["Fast"]+[f"Slow{i}" for i in range(30)], media_type="tv", as_of="2026-08-01")'}, context=context))
        try:
            assert await asyncio.to_thread(checkpointed.wait, 3)
            if stop == "cancel":
                context.cancellation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
            else:
                result = await asyncio.wait_for(task, 4)
                assert result.outcome.public_content["data"]["timed_out"]
                assert not context.cancellation.cancelled
            archived = list((root / "output").glob("*.sdk.json"))
            assert len(archived) == 1
            snapshot = archived[0].read_bytes()
            data = json.loads(snapshot)
            assert data["partial"] and data["result"]["status"] == "partial"
            rows = data["result"]["data"]["items"]
            assert len(rows) == 31 and rows[0]["status"] == "updates_available"
            assert len(rows[0]["missing_sample"]) == 2
            assert all(row["status"] == "pending" for row in rows[1:])
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 3)
            if not task.done():
                task.cancel()
        assert len(reads) <= 4
        assert archived[0].read_bytes() == snapshot  # 取消后的在途读取不能覆写归档。
        cold, following, _ = await native_updates_runtime()
        code = 'import json\nfrom pathlib import Path\nr=json.load(next(Path("/output").glob("*.sdk.json")).open())\nassert r["result"]["data"]["items"][0]["missing_count"]==2\nprint("completed facts restored")'
        result = await cold.execute(sandbox.RUN_CODE_TOOL, {"code": code}, context=following)
        assert result.outcome.public_content["data"]["exit_code"] == 0
    with patch("socket.socket.connect", side_effect=AssertionError("external network forbidden")):
        asyncio.run(run())
