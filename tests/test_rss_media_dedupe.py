from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import database as db
from app.clients.base import SeriesCandidate, SeriesEpisodeInventory, SeriesSearchResult
from app.modules.media_identity import build_media_key, parse_episode_label
from app.modules.rss import RSSEngine, RSSEntry, rss_subscription_refresh_revision
from app.modules.rss_subscription_config import (
    RSSSubscriptionConfigError,
    normalize_rss_subscription_create,
)
from app.services import inspect_series_episode_inventory_by_tmdb
from tests.support import IsolatedDatabaseTestCase


class RSSMediaDedupeTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM rss_entry_media")
            conn.execute("DELETE FROM rss_entries")
            conn.execute("DELETE FROM rss_media_bindings")
            conn.execute("DELETE FROM rss_items")

    def test_media_identity_only_accepts_structured_episode_labels(self) -> None:
        self.assertEqual(parse_episode_label("S02E03", default_season=1), (2, 3))
        self.assertEqual(parse_episode_label("E03", default_season=4), (4, 3))
        self.assertIsNone(parse_episode_label("第 3 集", default_season=1))
        self.assertEqual(
            build_media_key("00105556", "tv", 2, 3),
            "tmdb:105556:tv:S02E003",
        )

    def test_subscription_binding_round_trips_without_changing_legacy_rss_row(self) -> None:
        sid = db.add_rss_subscription(
            "Anime", "https://example.invalid/rss",
            media_tmdb_id="105556", media_default_season=2,
            skip_existing_episodes=1,
        )
        row = db.get_rss_subscription(sid)
        self.assertEqual(str(row["media_tmdb_id"]), "105556")
        self.assertEqual(int(row["media_default_season"]), 2)
        self.assertTrue(bool(row["skip_existing_episodes"]))

        db.update_rss_subscription(sid, {
            "media_tmdb_id": "", "skip_existing_episodes": 0,
        })
        cleared = db.get_rss_subscription(sid)
        self.assertEqual(str(cleared["media_tmdb_id"]), "")
        self.assertFalse(bool(cleared["skip_existing_episodes"]))

    def test_enabled_subscription_snapshot_includes_binding_revision_fields(self) -> None:
        sid = db.add_rss_subscription(
            "Enabled", "https://example.invalid/rss", media_tmdb_id="105556",
            media_default_season=0, skip_existing_episodes=1,
        )
        row = next(item for item in db.list_enabled_rss_subscriptions() if int(item["id"]) == sid)
        self.assertEqual(str(row["media_tmdb_id"]), "105556")
        self.assertEqual(int(row["media_default_season"]), 0)
        self.assertTrue(bool(row["skip_existing_episodes"]))

    def test_specials_season_zero_is_preserved_in_refresh_revision(self) -> None:
        zero_id = db.add_rss_subscription(
            "Specials", "https://example.invalid/specials",
            media_tmdb_id="105556", media_default_season=0,
            skip_existing_episodes=1,
        )
        one_id = db.add_rss_subscription(
            "Season One", "https://example.invalid/season-one",
            media_tmdb_id="105556", media_default_season=1,
            skip_existing_episodes=1,
        )

        zero = db.get_rss_subscription(zero_id)
        one = db.get_rss_subscription(one_id)
        self.assertEqual(int(zero["media_default_season"]), 0)
        self.assertNotEqual(
            rss_subscription_refresh_revision(zero),
            rss_subscription_refresh_revision(one),
        )

    def test_media_key_dedupes_across_rss_subscriptions(self) -> None:
        first_sub = db.add_rss_subscription("A", "https://a.invalid/rss")
        second_sub = db.add_rss_subscription("B", "https://b.invalid/rss")
        media_key = build_media_key("105556", "tv", 1, 7)
        first = db.add_rss_entry_with_media(
            first_sub, "first", "guid-a", media_key=media_key,
            tmdb_id="105556", season=1, episode=7,
        )
        second = db.add_rss_entry_with_media(
            second_sub, "second", "guid-b", media_key=media_key,
            tmdb_id="105556", season=1, episode=7,
        )

        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["status"], "skipped")
        rows = db.list_rss_entries(sub_id=second_sub)
        self.assertEqual(str(rows[0]["skip_reason"]), "相同 TMDB 剧集已在 RSS 队列或下载记录中")
        self.assertTrue(bool(rows[0]["processed"]))
        self.assertEqual(db.update_rss_entries_processed([int(rows[0]["id"])], False), 1)
        restored = db.list_rss_entries(sub_id=second_sub)[0]
        self.assertEqual(str(restored["status"]), "pending")
        self.assertEqual(str(restored["skip_reason"]), "")

    def test_media_key_dedupe_blocks_unresolved_backend_outcomes(self) -> None:
        media_key = build_media_key("105556", "tv", 1, 8)
        for failure_code in (
            "qb_outcome_unknown",
            "guangya_outcome_unknown",
            "submission_outcome_unknown",
        ):
            with self.subTest(failure_code=failure_code):
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM rss_entry_media")
                    conn.execute("DELETE FROM rss_entries")
                    conn.execute("DELETE FROM rss_items")
                first_sub = db.add_rss_subscription(
                    f"first-{failure_code}", f"https://first.invalid/{failure_code}"
                )
                second_sub = db.add_rss_subscription(
                    f"second-{failure_code}", f"https://second.invalid/{failure_code}"
                )
                first = db.add_rss_entry_with_media(
                    first_sub, "first", f"guid-first-{failure_code}",
                    media_key=media_key, tmdb_id="105556", season=1, episode=8,
                )
                first_id = int(first["id"])
                db.record_rss_entry_failure(first_id, failure_code, False)

                second = db.add_rss_entry_with_media(
                    second_sub, "second", f"guid-second-{failure_code}",
                    media_key=media_key, tmdb_id="105556", season=1, episode=8,
                )

                self.assertEqual(second["status"], "skipped")
                self.assertEqual(
                    second["skip_reason"],
                    "相同 TMDB 剧集已在 RSS 队列或下载记录中",
                )

    def test_refresh_skips_exact_episode_already_in_media_library(self) -> None:
        sid = db.add_rss_subscription(
            "Bound", "https://example.invalid/rss",
            media_tmdb_id="105556", media_default_season=1,
            skip_existing_episodes=1,
        )
        engine = RSSEngine()
        engine.parser.parse = Mock(return_value=[RSSEntry(
            title="Bound S01E02 1080p", guid="episode-2", episode="S01E02",
            series_title="Bound", torrent_url="magnet:?xt=urn:btih:" + "a" * 40,
        )])
        engine.parser.last_error_code = ""
        with patch.object(
            engine, "_bound_tv_title_keys", return_value=({"bound"}, True),
        ), patch.object(
            engine, "_existing_library_episodes",
            return_value=({(1, 2)}, {"sources": 1, "ready": 1, "unavailable": 0, "truncated": 0}),
        ):
            result = engine.refresh(sid)

        self.assertEqual(result["library_skipped"], 1)
        self.assertEqual(result["new"], 1)
        rows = db.list_rss_entries(sub_id=sid)
        self.assertEqual(str(rows[0]["status"]), "skipped")
        self.assertEqual(str(rows[0]["skip_reason"]), "媒体库已存在该剧集")

    def test_tmdb_alternative_title_allows_binding(self) -> None:
        client = SimpleNamespace(detail_with_alternative_titles=Mock(return_value={
            "id": 105556,
            "name": "Official Series",
            "original_name": "Original Series",
            "alternative_titles": {"results": [{"title": "RSS: Alias"}]},
            "translations": {"translations": [{"data": {"name": "Localized Series"}}]},
        }))
        engine = RSSEngine(tmdb_client=client)

        keys, verified = engine._bound_tv_title_keys("105556")

        self.assertTrue(verified)
        self.assertIn("rssalias", keys)
        self.assertIn("localizedseries", keys)
        client.detail_with_alternative_titles.assert_called_once_with("105556", "tv")

    def test_single_series_title_mismatch_bypasses_tmdb_binding(self) -> None:
        sid = db.add_rss_subscription(
            "Wrong binding", "https://example.invalid/rss",
            media_tmdb_id="105556", media_default_season=1,
            skip_existing_episodes=1,
        )
        engine = RSSEngine()
        engine.parser.parse = Mock(return_value=[RSSEntry(
            title="Other Series S01E02 1080p", guid="other-2", episode="S01E02",
            series_title="Other Series", torrent_url="magnet:?xt=urn:btih:" + "c" * 40,
        )])
        engine.parser.last_error_code = ""
        with patch.object(
            engine, "_bound_tv_title_keys", return_value=({"boundseries"}, True),
        ), patch.object(engine, "_existing_library_episodes") as library_lookup:
            result = engine.refresh(sid)

        library_lookup.assert_not_called()
        self.assertTrue(result["media_binding_bypassed"])
        self.assertEqual(result["binding_bypass_reason"], "tmdb_title_mismatch")
        rows = db.list_rss_entries(sub_id=sid)
        self.assertEqual(str(rows[0]["status"]), "pending")
        self.assertEqual(str(rows[0]["skip_reason"]), "")
        with db.get_conn() as conn:
            bound_rows = conn.execute(
                "SELECT COUNT(*) FROM rss_entry_media m "
                "JOIN rss_entries e ON e.id=m.rss_entry_id WHERE e.rss_item_id=?",
                (sid,),
            ).fetchone()[0]
        self.assertEqual(bound_rows, 0)

    def test_tmdb_title_lookup_failure_keeps_feed_entries_pending(self) -> None:
        sid = db.add_rss_subscription(
            "Unverified", "https://example.invalid/rss",
            media_tmdb_id="105556", media_default_season=1,
            skip_existing_episodes=1,
        )
        engine = RSSEngine()
        engine.parser.parse = Mock(return_value=[RSSEntry(
            title="Bound S01E03 1080p", guid="bound-3", episode="S01E03",
            series_title="Bound", torrent_url="magnet:?xt=urn:btih:" + "d" * 40,
        )])
        engine.parser.last_error_code = ""
        with patch.object(
            engine, "_bound_tv_title_keys", return_value=(set(), False),
        ), patch.object(engine, "_existing_library_episodes") as library_lookup:
            result = engine.refresh(sid)

        library_lookup.assert_not_called()
        self.assertTrue(result["media_binding_bypassed"])
        self.assertEqual(result["binding_bypass_reason"], "tmdb_title_unverified")
        rows = db.list_rss_entries(sub_id=sid)
        self.assertEqual(str(rows[0]["status"]), "pending")
        self.assertEqual(str(rows[0]["skip_reason"]), "")

    def test_normalized_official_title_enables_media_identity(self) -> None:
        sid = db.add_rss_subscription(
            "Normalized", "https://example.invalid/rss",
            media_tmdb_id="105556", media_default_season=1,
            skip_existing_episodes=0,
        )
        engine = RSSEngine()
        engine.parser.parse = Mock(return_value=[RSSEntry(
            title="Series A S01E04 1080p", guid="series-a-4", episode="S01E04",
            series_title="Series: A", torrent_url="magnet:?xt=urn:btih:" + "e" * 40,
        )])
        engine.parser.last_error_code = ""
        with patch.object(
            engine, "_bound_tv_title_keys", return_value=({"seriesa"}, True),
        ):
            result = engine.refresh(sid)

        self.assertFalse(result.get("media_binding_bypassed", False))
        with db.get_conn() as conn:
            media = conn.execute(
                "SELECT media_key,tmdb_id,season,episode FROM rss_entry_media"
            ).fetchone()
        self.assertEqual(media["media_key"], "tmdb:105556:tv:S01E004")
        self.assertEqual(media["tmdb_id"], "105556")

    def test_mixed_feed_bypasses_subscription_level_tmdb_binding(self) -> None:
        sid = db.add_rss_subscription(
            "Mixed", "https://example.invalid/rss",
            media_tmdb_id="105556", media_default_season=1,
            skip_existing_episodes=1,
        )
        engine = RSSEngine()
        engine.parser.parse = Mock(return_value=[
            RSSEntry(
                title="Series A S01E02 1080p", guid="series-a-2", episode="S01E02",
                series_title="Series A", torrent_url="magnet:?xt=urn:btih:" + "a" * 40,
            ),
            RSSEntry(
                title="Series B S01E02 1080p", guid="series-b-2", episode="S01E02",
                series_title="Series B", torrent_url="magnet:?xt=urn:btih:" + "b" * 40,
            ),
        ])
        engine.parser.last_error_code = ""
        with patch.object(engine, "_existing_library_episodes") as library_lookup:
            result = engine.refresh(sid)

        library_lookup.assert_not_called()
        self.assertTrue(result["mixed_feed_bypassed"])
        self.assertEqual(result["detected_series_count"], 2)
        self.assertEqual(result["library_skipped"], 0)
        self.assertEqual(result["semantic_duplicates"], 0)
        rows = db.list_rss_entries(sub_id=sid)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(str(row["status"]) == "pending" for row in rows))
        with db.get_conn() as conn:
            bound_rows = conn.execute(
                "SELECT COUNT(*) FROM rss_entry_media m "
                "JOIN rss_entries e ON e.id=m.rss_entry_id WHERE e.rss_item_id=?",
                (sid,),
            ).fetchone()[0]
        self.assertEqual(bound_rows, 0)
        self.assertTrue(all(str(row["skip_reason"] or "") == "" for row in rows))

    def test_unresolved_series_in_feed_bypasses_subscription_level_tmdb_binding(self) -> None:
        sid = db.add_rss_subscription(
            "Partially unresolved", "https://example.invalid/rss",
            media_tmdb_id="105556", media_default_season=1,
            skip_existing_episodes=1,
        )
        engine = RSSEngine()
        engine.parser.parse = Mock(return_value=[
            RSSEntry(
                title="Series A S01E02 1080p", guid="series-a-2", episode="S01E02",
                series_title="Series A", torrent_url="magnet:?xt=urn:btih:" + "a" * 40,
            ),
            RSSEntry(
                title="Unknown S01E03 1080p", guid="unknown-3", episode="S01E03",
                series_title="", torrent_url="magnet:?xt=urn:btih:" + "b" * 40,
            ),
        ])
        engine.parser.last_error_code = ""
        with patch.object(engine, "_existing_library_episodes") as library_lookup:
            result = engine.refresh(sid)

        library_lookup.assert_not_called()
        self.assertTrue(result["media_binding_bypassed"])
        self.assertFalse(result["mixed_feed_bypassed"])
        self.assertEqual(result["binding_bypass_reason"], "series_unresolved")
        self.assertEqual(result["detected_series_count"], 1)
        self.assertEqual(result["unresolved_series_count"], 1)
        rows = db.list_rss_entries(sub_id=sid)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(str(row["status"]) == "pending" for row in rows))
        with db.get_conn() as conn:
            bound_rows = conn.execute(
                "SELECT COUNT(*) FROM rss_entry_media m "
                "JOIN rss_entries e ON e.id=m.rss_entry_id WHERE e.rss_item_id=?",
                (sid,),
            ).fetchone()[0]
        self.assertEqual(bound_rows, 0)

    def test_strict_inventory_uses_provider_id_lookup_without_title_search(self) -> None:
        client = SimpleNamespace(
            find_series_candidates_by_tmdb=Mock(return_value=SeriesSearchResult(
                candidates=[SeriesCandidate(id="series-1", name="Bound", tmdb_id="105556")],
                total=1, truncated=False,
            )),
            list_series_episode_inventory=Mock(return_value=SeriesEpisodeInventory(
                episodes=[(1, 1), (1, 2)], total=2, truncated=False,
                ignored_specials=0, ignored_unknown=0,
            )),
        )
        with patch("app.services._configured_media_sources", return_value=[
            ("jellyfin", "Jellyfin", "http://jellyfin", client),
        ]):
            rows = inspect_series_episode_inventory_by_tmdb("105556")

        self.assertEqual(rows[0]["status"], "ready")
        self.assertEqual(rows[0]["episodes"], [(1, 1), (1, 2)])
        client.find_series_candidates_by_tmdb.assert_called_once_with("105556", limit=20)
        self.assertFalse(hasattr(client, "search_series_candidates"))

    def test_shared_binding_validation_requires_tmdb_for_library_filter(self) -> None:
        base = {"name": "Anime", "urls": "https://example.invalid/rss"}
        with self.assertRaisesRegex(RSSSubscriptionConfigError, "TMDB ID"):
            normalize_rss_subscription_create({
                **base,
                "skip_existing_episodes": True,
            })
        fields = normalize_rss_subscription_create({
            **base,
            "skip_existing_episodes": True,
            "media_tmdb_id": "00105556",
            "media_default_season": 2,
        })
        self.assertEqual(fields["media_tmdb_id"], "105556")
        self.assertEqual(fields["media_default_season"], 2)
        specials = normalize_rss_subscription_create({
            **base,
            "skip_existing_episodes": True,
            "media_tmdb_id": "105556",
            "media_default_season": 0,
        })
        self.assertEqual(specials["media_default_season"], 0)


class RSSTMDBClientLifecycleTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM rss_entry_media")
            conn.execute("DELETE FROM rss_entries")
            conn.execute("DELETE FROM rss_media_bindings")
            conn.execute("DELETE FROM rss_items")

    @staticmethod
    def _detail() -> dict:
        return {
            "id": 105556,
            "name": "Bound",
            "original_name": "Bound",
            "alternative_titles": {"results": []},
            "translations": {"translations": []},
        }

    def test_refresh_closes_lazily_created_tmdb_client(self) -> None:
        sid = db.add_rss_subscription(
            "Bound",
            "https://example.invalid/rss",
            media_tmdb_id="105556",
            media_default_season=1,
            skip_existing_episodes=0,
        )
        engine = RSSEngine()
        engine.parser.parse = Mock(return_value=[RSSEntry(
            title="Bound S01E01 1080p",
            guid="bound-1",
            episode="S01E01",
            series_title="Bound",
            torrent_url="magnet:?xt=urn:btih:" + "a" * 40,
        )])
        engine.parser.last_error_code = ""
        client = Mock()
        client.detail_with_alternative_titles.return_value = self._detail()

        with patch("app.clients.tmdb.TMDBClient", return_value=client):
            result = engine.refresh(sid)

        self.assertEqual(result["new"], 1)
        client.close.assert_called_once_with()
        self.assertIsNone(engine._tmdb_client)

    def test_injected_tmdb_client_remains_caller_owned(self) -> None:
        client = Mock()
        client.detail_with_alternative_titles.return_value = self._detail()
        engine = RSSEngine(tmdb_client=client)

        keys, verified = engine._bound_tv_title_keys("105556")
        engine.close()

        self.assertTrue(verified)
        self.assertIn("bound", keys)
        client.close.assert_not_called()
