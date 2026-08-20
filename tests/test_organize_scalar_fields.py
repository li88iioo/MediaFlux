from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.organize import OrganizePlan, OrganizeRules, Organizer
from app.modules.organize_postprocess import (
    media_notification_item,
    resolved_plan_position,
)
from app.modules.organize_correction import OrganizeCorrectionService


class _ListPositionScraper:
    @staticmethod
    def parse_media(_name: str, parent_path: str = "", match=None):
        return SimpleNamespace(
            title="Example", year="", media_type="tv", tmdb_id="",
            effective_season=[3, 4], effective_episode=[7],
        )


class _FailingPositionScraper:
    @staticmethod
    def parse_media(_name: str, parent_path: str = "", match=None):
        raise ValueError("invalid typed parse")


class _ExistingPositionScraper(_ListPositionScraper):
    @staticmethod
    def parse_existing_media(name: str):
        import re

        match = re.search(r"S(\d{2})E(\d{2})", name)
        if not match:
            return SimpleNamespace(
                title="", year="", media_type="tv", tmdb_id="",
                effective_season=None, effective_episode=None,
            )
        return SimpleNamespace(
            title="", year="", media_type="tv", tmdb_id="",
            effective_season=int(match.group(1)),
            effective_episode=int(match.group(2)),
        )


class OrganizeScalarFieldTests(unittest.TestCase):
    def test_parser_list_positions_are_normalized_before_logging(self):
        organizer = Organizer(client=MagicMock(), scraper=_ListPositionScraper())

        parsed = organizer._parse_media_fields("Example Season 3.mkv")

        self.assertEqual(parsed["season"], 3)
        self.assertEqual(parsed["episode"], 7)
        self.assertEqual(parsed["title"], "Example")

    def test_parser_failure_does_not_fall_back_to_parallel_filename_rules(self):
        organizer = Organizer(client=MagicMock(), scraper=_FailingPositionScraper())

        self.assertEqual(organizer._parse_media_fields("Show.S01E02.mkv"), {})

    def test_final_plan_position_overrides_raw_filename_for_logs_and_notifications(self):
        plan = OrganizePlan(
            file_id="special-1",
            original_name="NCED1.mkv",
            original_path="Show/Batch-B",
            season=0,
            episode=4,
        )
        parsed = {"season": 0, "episode": 1}

        self.assertEqual(
            resolved_plan_position(plan, parsed),
            (0, 4),
        )
        item = media_notification_item(plan, "Show.S00E04.mkv", parsed)
        self.assertEqual((item["season"], item["episode"]), (0, 4))

        patched = media_notification_item(
            plan,
            "Show.S08E09.mkv",
            parsed,
            resolved_position=(8, 9),
        )
        self.assertEqual((patched["season"], patched["episode"]), (8, 9))


    def test_season_inventory_uses_cached_target_files_without_counting_metadata(self):
        organizer = Organizer(client=MagicMock(), scraper=_ExistingPositionScraper())
        files = [
            GuangYaFile("1", "Show.2026.S02E01.mkv", False, 100, "", "season"),
            GuangYaFile("2", "Show.2026.S02E03.mp4", False, 100, "", "season"),
            GuangYaFile("3", "Show.2026.S02E03.srt", False, 1, "", "season"),
            GuangYaFile("4", "Show.2026.S01E09.mkv", False, 100, "", "season"),
        ]

        self.assertEqual(
            organizer._season_episode_inventory(files, OrganizeRules(), season=2),
            [1, 3],
        )

    def test_logs_ui_renders_escaped_skip_reasons_in_list_and_detail(self):
        template = (Path("app/templates/logs.html").read_text(encoding="utf-8") + Path("app/static/js/logs.js").read_text(encoding="utf-8"))
        styles = Path("app/static/css/main.css").read_text(encoding="utf-8")

        self.assertIn('class="organize-log-reason"', template)
        self.assertIn('跳过原因：${_esc(r.error)}', template)
        self.assertIn("...(data.error?[[", template)
        self.assertIn("item.status==='skipped'?'跳过原因':'错误'", template)
        self.assertIn(".organize-log-reason", styles)
        self.assertIn(".organize-item-reason", styles)
        self.assertIn(".organize-detail-summary > .is-warning { grid-column: 1 / -1", styles)
        self.assertIn("grid-template-columns: 86px minmax(0,1fr)", styles)
        self.assertIn('id="organizeReleaseParseSection" hidden', template)
        self.assertIn("_renderReleaseParse(data.release_parse)", template)
        self.assertIn(".organize-release-parse-grid", styles)

    def test_database_boundary_never_binds_position_lists(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE organize_log("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,source,original_path,new_path,file_id,status,tmdb_id,"
            "provider,external_id,"
            "operation_type,source_dir_id,original_parent_id,original_name,current_parent_id,current_name,"
            "target_parent_id,media_type,title,year,season,episode,error,release_parse_json,parent_log_id,operation_token,"
            "version,legacy_incomplete,created_at,updated_at)"
        )

        @contextmanager
        def memory_conn():
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        try:
            with patch.object(db, "get_conn", memory_conn):
                log_id = db.add_organize_log(
                    "guangya",
                    "source/Example.mkv",
                    "",
                    "file-1",
                    "skipped",
                    original_parent_id="source",
                    original_name="Example.mkv",
                    season=[3, 4],
                    episode=[7],
                    error="待确认",
                    release_parse={
                        "source_position": {"season": 3, "episode": 7},
                        "effective_position": {"season": 2, "episode": 1},
                        "evidence": [{"kind": "episode", "source": "explicit"}],
                    },
                    legacy_incomplete=False,
                )
            row = conn.execute(
                "SELECT season,episode,status,release_parse_json "
                "FROM organize_log WHERE id=?", (log_id,)
            ).fetchone()
            self.assertEqual(row["season"], 3)
            self.assertEqual(row["episode"], 7)
            self.assertEqual(row["status"], "skipped")
            stored_release_parse = json.loads(row["release_parse_json"])
            self.assertEqual(
                OrganizeCorrectionService._decode_release_parse(
                    row["release_parse_json"]
                ),
                stored_release_parse,
            )
            self.assertEqual(
                stored_release_parse["effective_position"],
                {"season": 2, "episode": 1},
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
