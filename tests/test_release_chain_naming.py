"""默认长名称必须在真实临时媒体移动后保留可识别后缀和媒体位置。"""

from __future__ import annotations

from typing import cast
from app.clients.guangya import GuangYaClient
from app.modules.scraper import TMDBScraper

import tests  # noqa: F401
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import database as db
from app.modules.local_media_service import LocalMediaService
from app.modules.local_storage import LocalFilesystemAdapter
from app.modules.organize import OrganizeRules, Organizer
from app.modules.scraper import MatchResult
from app.clients.guangya import GuangYaFile
from tests.support import IsolatedDatabaseTestCase
from tests.test_local_media_service import FakeScraper


class ReleaseChainNamingTests(IsolatedDatabaseTestCase):
    def test_long_default_names_survive_local_movie_and_tv_moves(self):
        for media_type in ("movie", "tv"):
            for case, title in enumerate(("A" * 240, "很长的媒体标题" * 50, "倪亚‧利斯顿")):
                with (
                    self.subTest(media_type=media_type, case=case),
                    TemporaryDirectory() as raw,
                ):
                    root = Path(raw)
                    source = root / "incoming"
                    source.mkdir()
                    target = root / "library"
                    target.mkdir()
                    filename = (
                        "Show.S01E03.2025.mkv"
                        if media_type == "tv"
                        else "Movie.2025.mkv"
                    )
                    video = source / filename
                    video.write_bytes(b"isolated fake media")
                    (source / (video.stem + ".chs.srt")).write_bytes(
                        b"isolated subtitle"
                    )
                    source_id = db.create_local_media_source(
                        name=f"B-naming-{media_type}-{case}",
                        qb_profile="",
                        qb_path_prefix="",
                        local_root=str(source),
                        media_type=media_type,
                    )
                    db.upsert_local_library_target(source_id, media_type, str(target))
                    scraper = FakeScraper(
                        MatchResult(
                            tmdb_id="123",
                            title=title,
                            year="2025",
                            media_type=media_type,
                            confidence=1.0,
                        )
                    )
                    service = LocalMediaService(scraper=cast(TMDBScraper, scraper))
                    rules = OrganizeRules(
                        region_split=False,
                        year_split=False,
                        emby_refresh=False,
                        clean_empty=False,
                    )
                    try:
                        with (
                            patch(
                                "app.modules.local_media_service.OrganizeRules.from_config",
                                return_value=rules,
                            ),
                            patch(
                                "app.modules.local_media_service.probe_local_media_profile",
                                return_value=None,
                            ),
                        ):
                            inspection = service.inspect_source("admin", source_id, video)
                            preview = service.preview(
                                "admin", inspection["inspection_id"], "123", media_type,
                            )
                            self.assertEqual(preview["status"], "planned")
                            task_id = service.create_manual_task(
                                "admin", inspection["inspection_id"],
                                preview_digest=preview["preview_digest"],
                            )
                            self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))
                            result = service.execute_task("admin", task_id)
                        self.assertEqual(result["status"], "completed")
                        self.assertFalse(video.exists())
                        scanned = LocalFilesystemAdapter(target).scan(
                            target, include_non_media=True
                        )
                        videos = [item for item in scanned if item.role == "video"]
                        self.assertEqual(len(videos), 1)
                        moved = videos[0].path
                        self.assertEqual(moved.suffix, ".mkv")
                        self.assertEqual(moved.read_bytes(), b"isolated fake media")
                        self.assertNotIn("‧", str(moved.relative_to(target)))
                        if title == "倪亚‧利斯顿":
                            self.assertIn("倪亚·利斯顿", moved.name)
                        self.assertLessEqual(len(moved.name.encode()), 240)
                        self.assertIn("{tmdb-123}", str(moved.parent))
                        if media_type == "tv":
                            self.assertIn("S01E03", moved.name)
                        self.assertEqual(
                            len([item for item in scanned if item.role == "subtitle"]),
                            1,
                        )
                    finally:
                        service.close()

    def test_long_tv_name_with_variant_tags_keeps_season_and_extension(self):
        organizer = Organizer(client=cast(GuangYaClient, object()), scraper=cast(TMDBScraper, object()))
        match = MatchResult(
            tmdb_id="123", title="长标题" * 100, year="2025", media_type="tv"
        )
        rules = OrganizeRules(keep_multi_versions=True, keep_remux_variant=True)
        name = organizer.build_new_name(
            match,
            GuangYaFile("v", "Show.S00E03.1080p.Remux.mkv", False),
            {"season": 0, "episode": 3},
            rules,
        )
        self.assertTrue(name.endswith(".mkv"))
        self.assertIn("S00E03", name)
        self.assertLessEqual(len(name.encode()), 240)

    def test_media_template_accepts_case_insensitive_real_extension(self):
        from app.modules.naming import build_context, render_media_template

        context = build_context(
            title="Movie", year="2025", ext="mkv", original_name="Source.mkv"
        )
        self.assertEqual(
            render_media_template("${showTitle}.MKV", context), "Movie.MKV"
        )

    def test_long_original_name_templates_keep_extension_but_fixed_tail_cannot_shrink(
        self,
    ):
        from app.modules.naming import build_context, render_media_template

        source_name = "S" * 241 + ".mkv"
        self.assertLessEqual(len(source_name.encode()), 255)
        context = build_context(title="", year="", ext="mkv", original_name=source_name)
        for template in (
            "${original_name}.${ext}",
            "${originalName}",
            "${originalStem}.${ext}",
        ):
            with self.subTest(template=template):
                name = render_media_template(template, context)
                self.assertTrue(name.endswith(".mkv"))
                self.assertLessEqual(len(name.encode()), 240)
        fixed = build_context(
            title="Movie",
            year="2025",
            ext="mkv",
            original_name="Source.mkv",
            media_info="X" * 245,
        )
        with self.assertRaises(ValueError):
            render_media_template("${original_name}.${mediaInfo}.${ext}", fixed)
