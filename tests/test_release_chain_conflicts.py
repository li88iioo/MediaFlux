"""冲突库存的批内复杂度及位置事实回归；不取消任何冲突安全门。"""

from __future__ import annotations

from typing import cast
from app.clients.guangya import GuangYaClient

import tests  # noqa: F401
from unittest.mock import patch

from app.clients.guangya import GuangYaFile
from app.modules.organize import Organizer, OrganizePlan, OrganizeRules
from app.modules.scraper import TMDBScraper, MatchResult
from tests.support import IsolatedDatabaseTestCase


class ReleaseChainConflictTests(IsolatedDatabaseTestCase):
    def _plan(self, file_id, episode, *, size=100, season=1):
        return OrganizePlan(
            file_id=file_id,
            original_name=f"Show.S{season:02}E{episode:03}.mkv",
            original_path="incoming",
            original_parent_id="incoming",
            match=MatchResult(
                tmdb_id="123", title="Show", year="2025", media_type="tv"
            ),
            season=season,
            episode=episode,
            size=size,
            new_name=f"Show.2025.S{season:02}E{episode:03}.mkv",
            target_path="Show/Season",
        )

    def test_empty_target_does_not_reparse_virtual_episode_inventory(self):
        for count in (80, 160):
            with self.subTest(count=count):
                scraper = TMDBScraper()
                organizer = Organizer(client=cast(GuangYaClient, object()), scraper=scraper)
                plans = [self._plan(f"new-{i}", i) for i in range(1, count + 1)]
                try:
                    with patch.object(
                        organizer,
                        "_parse_existing_media_fields",
                        wraps=organizer._parse_existing_media_fields,
                    ) as parse:
                        organizer._preview_conflicts_with_inventory(
                            plans, OrganizeRules(), lambda p: ("target", [], {})
                        )
                    self.assertEqual(parse.call_count, 0)
                    self.assertTrue(all(plan.action == "move" for plan in plans))
                finally:
                    scraper.close()

    def test_existing_names_are_parsed_once_per_inventory(self):
        scraper = TMDBScraper()
        organizer = Organizer(client=cast(GuangYaClient, object()), scraper=scraper)
        files = [
            GuangYaFile(
                f"old-{i}", f"Show.2025.S01E{i:03}.mkv", False, 100, "etag", "target"
            )
            for i in range(1, 81)
        ]
        plans = [self._plan(f"new-{i}", i) for i in range(1, 81)]
        try:
            with patch.object(
                organizer,
                "_parse_existing_media_fields",
                wraps=organizer._parse_existing_media_fields,
            ) as parse:
                organizer._preview_conflicts_with_inventory(
                    plans, OrganizeRules(conflict_strategy=1), lambda p: ("target", files, {})
                )
            self.assertEqual(parse.call_count, len(files))
            self.assertTrue(all(plan.action == "skip" for plan in plans))
        finally:
            scraper.close()

    def test_batch_winner_inherits_real_replacement_target(self):
        scraper = TMDBScraper()
        organizer = Organizer(client=cast(GuangYaClient, object()), scraper=scraper)
        old = GuangYaFile("old", "Show.2025.S01E003.mkv", False, 50, "etag", "target")
        first = self._plan("first", 3, size=100)
        winner = self._plan("winner", 3, size=200)
        try:
            organizer._preview_conflicts_with_inventory(
                [first, winner],
                OrganizeRules(conflict_strategy=2),
                lambda p: ("target", [old], {}),
            )
            self.assertEqual(first.action, "skip")
            self.assertEqual(winner.action, "move")
            self.assertEqual(winner.conflict_existing_id, "old")
        finally:
            scraper.close()
