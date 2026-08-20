from __future__ import annotations

from unittest.mock import Mock, patch

from app.database import get_conn
from app.modules.media_aliases import (
    lookup_manual_alias,
    record_manual_confirmation,
)
from app.modules.scraper import MatchResult, TMDBScraper
from tests.support import IsolatedDatabaseTestCase


class MediaAliasPersistenceTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        # 正式 schema 初始化该表；同一个测试类共享临时数据库，逐用例清空。
        with get_conn() as conn:
            conn.execute("DELETE FROM media_title_aliases")

    def test_manual_confirmation_can_be_reused_by_title_and_year(self):
        written = record_manual_confirmation(
            ["Virgin Punk Clockwork Girl", "ヴァージン・パンク"],
            tmdb_id="1360829",
            title="Virgin Punk: Clockwork Girl",
            year="2025",
            media_type="movie",
        )

        match = lookup_manual_alias(
            ["Virgin Punk Clockwork Girl"], media_type="movie", year="2025"
        )

        self.assertEqual(written, 2)
        self.assertIsNotNone(match)
        self.assertEqual(match["tmdb_id"], "1360829")
        self.assertEqual(match["source"], "manual")

    def test_reconfirming_same_alias_blocks_old_same_year_mapping(self):
        record_manual_confirmation(
            ["Shared Title"], tmdb_id="100", title="Old", year="2025",
            media_type="movie",
        )
        record_manual_confirmation(
            ["Shared Title"], tmdb_id="200", title="New", year="2025",
            media_type="movie", rejected_tmdb_ids=["100"],
        )

        match = lookup_manual_alias(
            ["Shared Title"], media_type="movie", year="2025"
        )
        with get_conn() as conn:
            old = conn.execute(
                "SELECT blocked,source FROM media_title_aliases "
                "WHERE normalized_alias=? AND tmdb_id=?",
                ("shared title", "100"),
            ).fetchone()

        self.assertIsNotNone(match)
        self.assertEqual(match["tmdb_id"], "200")
        self.assertEqual(int(old["blocked"]), 1)
        self.assertEqual(old["source"], "manual_rejected")

    def test_year_keeps_remakes_disambiguated(self):
        record_manual_confirmation(
            ["Example"], tmdb_id="101", title="Example", year="1999",
            media_type="movie",
        )
        record_manual_confirmation(
            ["Example"], tmdb_id="202", title="Example", year="2025",
            media_type="movie",
        )

        old = lookup_manual_alias(["Example"], media_type="movie", year="1999")
        new = lookup_manual_alias(["Example"], media_type="movie", year="2025")
        ambiguous = lookup_manual_alias(["Example"], media_type="movie")

        self.assertEqual(old["tmdb_id"], "101")
        self.assertEqual(new["tmdb_id"], "202")
        self.assertIsNone(ambiguous)

    def test_year_bound_alias_requires_exact_year(self):
        record_manual_confirmation(
            ["Example"], tmdb_id="101", title="Example", year="2025",
            media_type="movie",
        )

        self.assertIsNone(
            lookup_manual_alias(["Example"], media_type="movie", year="2024")
        )
        self.assertEqual(
            lookup_manual_alias(["Example"], media_type="movie", year="2025")["tmdb_id"],
            "101",
        )

    def test_yearless_lookup_does_not_reuse_year_bound_alias(self):
        record_manual_confirmation(
            ["Example"], tmdb_id="101", title="Example", year="1999",
            media_type="movie",
        )

        match = lookup_manual_alias(["Example"], media_type="movie", year="")

        self.assertIsNone(match)

    def test_yearless_lookup_can_reuse_explicitly_yearless_alias(self):
        record_manual_confirmation(
            ["Example"], tmdb_id="101", title="Example", year="",
            media_type="movie",
        )

        match = lookup_manual_alias(["Example"], media_type="movie", year="")

        self.assertIsNotNone(match)
        self.assertEqual(match["tmdb_id"], "101")


class MediaAliasRecognitionTests(IsolatedDatabaseTestCase):
    def test_scraper_revalidates_manual_alias_through_tmdb_detail(self):
        scraper = TMDBScraper()
        scraper.match_from_tmdb = Mock(return_value=MatchResult(
            tmdb_id="1360829",
            title="Virgin Punk: Clockwork Girl",
            year="2025",
            media_type="movie",
            confidence=1.0,
            status="matched",
            matched_by="tmdb_id",
        ))

        with patch(
            "app.modules.media_aliases.lookup_manual_alias",
            return_value={
                "tmdb_id": "1360829",
                "alias": "Virgin Punk Clockwork Girl",
            },
        ):
            result = scraper.match(
                "[Studio GreenTea] Virgin Punk Clockwork Girl "
                "[Movie v2][BDRip][HEVC-10bit 1080p AAC][JPTC].mkv",
                media_type_hint="movie",
            )

        scraper.match_from_tmdb.assert_called_once_with("1360829", "movie")
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.matched_by, "manual_alias")
        self.assertTrue(result.locked)
        self.assertEqual(
            result.metadata["manual_alias"], "Virgin Punk Clockwork Girl"
        )
