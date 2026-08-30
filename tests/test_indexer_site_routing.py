"""按媒体语义路由索引站点的回归测试。

规则：动漫（日文原产）优先动漫专站；国漫优先中文站并剔除 Mikan；
真人影视剔除动漫专站。订阅显式配置永远优先，路由永不返回空集合，
成人站点永不参与自动路由。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.indexers.config import plan_media_site_route, tmdb_detail_is_animation
from app.indexers.service import IndexerService
from app.modules.media_subscriptions import _resolve_search_sites

ALL_SITES = ("nyaa", "mikan", "btbtla", "1lou", "tpb")


class PlanMediaSiteRouteTests(unittest.TestCase):
    def test_japanese_anime_prefers_anime_trackers_and_drops_tpb(self):
        routed = plan_media_site_route(ALL_SITES, is_animation=True, original_language="ja")

        self.assertEqual(routed, ("mikan", "nyaa", "1lou", "btbtla"))

    def test_chinese_anime_prefers_cn_forums_and_drops_mikan(self):
        routed = plan_media_site_route(ALL_SITES, is_animation=True, original_language="zh")

        self.assertEqual(routed, ("1lou", "btbtla", "nyaa"))

    def test_live_action_drops_anime_only_sites(self):
        for language in ("zh", "en", ""):
            with self.subTest(language=language):
                routed = plan_media_site_route(
                    ALL_SITES, is_animation=False, original_language=language,
                )

                self.assertEqual(routed, ("1lou", "btbtla", "tpb"))

    def test_sukebei_never_joins_automatic_routing(self):
        routed = plan_media_site_route(
            (*ALL_SITES, "sukebei"), is_animation=True, original_language="ja",
        )

        self.assertNotIn("sukebei", routed)

    def test_routing_fails_open_when_everything_would_be_dropped(self):
        # 只启用了动漫专站却搜真人影视：宁可搜到噪声也不能一个站都不搜。
        routed = plan_media_site_route(("mikan",), is_animation=False)

        self.assertEqual(routed, ("mikan",))

    def test_respects_the_enabled_subset(self):
        routed = plan_media_site_route(("nyaa", "tpb"), is_animation=True, original_language="ja")

        self.assertEqual(routed, ("nyaa",))

    def test_blank_entries_are_ignored(self):
        routed = plan_media_site_route(("", " 1LOU ", "tpb"), is_animation=False)

        self.assertEqual(routed, ("1lou", "tpb"))


class TmdbAnimationDetectionTests(unittest.TestCase):
    def test_detects_genre_id_and_names(self):
        for genres in ([{"id": 16}], [{"name": "Animation"}], [{"name": "动画"}]):
            with self.subTest(genres=genres):
                self.assertTrue(tmdb_detail_is_animation({"genres": genres}))

    def test_non_animation_and_malformed_details_are_false(self):
        for detail in (
            {"genres": [{"id": 18, "name": "Drama"}]},
            {"genres": "not-a-list"},
            {"genres": [None, "text"]},
            {},
            None,
        ):
            with self.subTest(detail=detail):
                self.assertFalse(tmdb_detail_is_animation(detail))


class ServiceMediaSiteRouteTests(unittest.TestCase):
    def test_route_uses_registry_order_and_enabled_subset(self):
        stub = SimpleNamespace(
            registry=SimpleNamespace(ids=lambda: ("nyaa", "mikan", "btbtla", "1lou", "tpb", "sukebei")),
            enabled_site_ids=frozenset({"nyaa", "1lou", "tpb", "sukebei"}),
        )

        routed = IndexerService.media_site_route(
            stub, is_animation=False, original_language="zh",
        )

        self.assertEqual(routed, ("1lou", "tpb"))


class ResolveSearchSitesTests(unittest.TestCase):
    def test_explicit_subscription_sites_win_over_routing(self):
        service = SimpleNamespace(
            media_site_route=lambda **_kwargs: ("1lou",),
        )

        sites = _resolve_search_sites(["nyaa"], {"genres": [{"id": 16}]}, service)

        self.assertEqual(sites, ["nyaa"])

    def test_unconfigured_subscription_routes_by_detail(self):
        captured: dict = {}

        def route(**kwargs):
            captured.update(kwargs)
            return ("1lou", "btbtla")

        service = SimpleNamespace(media_site_route=route)
        detail = {"genres": [{"id": 16}], "original_language": "zh"}

        sites = _resolve_search_sites([], detail, service)

        self.assertEqual(sites, ["1lou", "btbtla"])
        self.assertTrue(captured["is_animation"])
        self.assertEqual(captured["original_language"], "zh")

    def test_routing_failure_falls_back_to_all_enabled(self):
        def broken(**_kwargs):
            raise RuntimeError("registry down")

        service = SimpleNamespace(media_site_route=broken)

        self.assertIsNone(_resolve_search_sites([], {}, service))


if __name__ == "__main__":
    unittest.main()
