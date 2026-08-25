"""电影目录、人工整理通知与 STRM 明细回归测试。"""
from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

from app.clients.guangya import GuangYaFile
from app.modules.naming import build_context, render_template
from app.modules.organize import OrganizePlan, OrganizeRules, Organizer
from app.modules.scraper import MatchResult
from tests.support import IsolatedDatabaseTestCase, release_parse_result


class _MovieScraper:
    def match(self, _filename):
        return MatchResult(
            tmdb_id="1155281",
            title="封神第二部：战火西岐",
            year="2025",
            media_type="movie",
            confidence=1.0,
        )

    def parse_media(self, filename, parent_path="", match=None):
        return release_parse_result(
            {"season": None, "episode": None, "type": "movie"},
            filename=filename, parent_path=parent_path,
        )

    def get_detail(self, _tmdb_id, _media_type):
        return {}


class _AnimeEpisodeScraper:
    def match(self, _filename):
        return MatchResult(
            tmdb_id="46004", title="约会大作战", year="2013",
            media_type="tv", confidence=1.0,
        )

    def parse_media(self, filename, parent_path="", match=None):
        return release_parse_result(
            {"season": 5, "episode": 12, "type": "tv"},
            filename=filename, parent_path=parent_path,
        )

    def get_detail(self, _tmdb_id, _media_type):
        return {
            "genres": [{"id": 16}],
            "origin_country": ["JP"],
            "first_air_date": "2013-04-06",
            "seasons": [{"season_number": 5, "episode_count": 12}],
        }


class MovieFolderNamingTests(unittest.TestCase):
    def setUp(self):
        probe = patch("app.modules.media_probe.probe_media_profile", return_value=None)
        probe.start()
        self.addCleanup(probe.stop)

    def test_media_info_dot_suffix_is_separator_safe(self):
        context = build_context(
            title="电影", year="2025", tmdb_id="1",
            media_info="1080p.SDR.H.265.AAC.2.0", ext="mkv",
        )
        self.assertEqual(
            render_template("${showTitle}.${showYear}${mediaInfoDotSuffix}.${ext}", context),
            "电影.2025.1080p.SDR.H.265.AAC.2.0.mkv",
        )
        empty = build_context(title="电影", year="2025", tmdb_id="1", ext="mkv")
        self.assertEqual(
            render_template("${showTitle}.${showYear}${mediaInfoDotSuffix}.${ext}", empty),
            "电影.2025.mkv",
        )
        self.assertEqual(render_template("${tmdbTag}", context), "{tmdb-1}")

    def test_filename_media_info_fallback_keeps_explicit_high_bit_depth(self):
        self.assertEqual(
            Organizer._extract_media_info(
                "Movie.2160p.SDR.HEVC.10bit.25fps.AAC.2.0.mkv"
            ),
            "2160p.SDR.H.265.10-bit.25fps.AAC.2.0",
        )
        self.assertNotIn(
            "8-bit",
            Organizer._extract_media_info("Movie.1080p.HEVC.8bit.AAC.mkv"),
        )
        self.assertEqual(
            Organizer._extract_media_info(
                "Show.S01E01.WEB-DL.1080p.H264.AAC.2.0.mkv"
            ),
            "WEB-DL.1080p.H.264.AAC.2.0",
        )

    def test_movie_uses_dedicated_tmdb_directory_and_media_suffix(self):
        rules = OrganizeRules(
            rename_enabled=True,
            movie_dir_template="${showTitle} (${showYear}) ${tmdbTag}",
            movie_template="${showTitle}.${showYear}${mediaInfoDotSuffix}.${ext}",
            media_info_enabled=True,
            naming_scope="guangya",
            region_split=False,
            year_split=False,
        )
        match = MatchResult("1155281", "封神第二部：战火西岐", "2025", "movie")
        file = GuangYaFile(
            "f1",
            "Creation.2025.1080p.SDR.H265.AAC.2.0.mkv",
            False,
            100,
            "etag",
            "parent",
        )
        organizer = Organizer(client=object(), scraper=object())
        self.assertEqual(
            organizer.build_media_dir(match, rules),
            "封神第二部：战火西岐 (2025) {tmdb-1155281}",
        )
        self.assertEqual(
            organizer.build_new_name(match, file, {"season": None, "episode": None}, rules),
            "封神第二部：战火西岐.2025.1080p.SDR.H.265.AAC.2.0.mkv",
        )

    def test_tv_plan_uses_unpadded_season_directory(self):
        rules = OrganizeRules(
            naming_scope="guangya", region_split=False, year_split=False,
        )
        organizer = Organizer(client=object(), scraper=_AnimeEpisodeScraper())
        file = GuangYaFile(
            "f1",
            "[ANi] 約會大作戰 DATE A LIVE S05E12 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4",
            False, 100, "etag", "parent",
        )

        plan = organizer._plan_one(file, "incoming", rules)

        self.assertEqual(
            plan.target_path,
            "动漫/约会大作战 (2013) {tmdb-46004}/Season 5",
        )
        self.assertEqual(plan.new_name, "约会大作战.2013.S05E12-WEB-DL.1080p.H.264.AAC.mp4")
        match = plan.match
        self.assertEqual(organizer.build_season_dir(match, {"season": 0}), "Specials")
        self.assertEqual(
            organizer.build_season_dir(match, {"season": None, "episode": 3}),
            "Season 1",
        )
        self.assertEqual(
            organizer.build_season_dir(match, {"season": 10, "episode": 1}),
            "Season 10",
        )

    def test_automatic_plan_places_movie_inside_movie_directory(self):
        rules = OrganizeRules(
            rename_enabled=True,
            media_info_enabled=True,
            movie_dir_template="${showTitle} (${showYear}) ${tmdbTag}",
            movie_template="${showTitle}.${showYear}${mediaInfoDotSuffix}.${ext}",
            naming_scope="guangya",
            region_split=False,
            year_split=False,
        )
        organizer = Organizer(client=object(), scraper=_MovieScraper())
        file = GuangYaFile(
            "f1", "Creation.2025.1080p.SDR.H265.AAC.2.0.mkv",
            False, 100, "etag", "parent",
        )
        plan = organizer._plan_one(file, "incoming", rules)
        self.assertEqual(
            plan.target_path,
            "电影/封神第二部：战火西岐 (2025) {tmdb-1155281}",
        )
        self.assertEqual(
            plan.new_name,
            "封神第二部：战火西岐.2025.1080p.SDR.H.265.AAC.2.0.mkv",
        )


if __name__ == "__main__":
    unittest.main()

class MediaProbeTests(unittest.TestCase):
    def test_media_profile_keeps_historical_positional_boolean_fields(self):
        from app.modules.media_probe import MediaProfile

        profile = MediaProfile("", "", "", "", "", "", "", "", True, False)

        self.assertIs(profile.dolby_vision, True)
        self.assertIs(profile.atmos, False)
        self.assertEqual(profile.video_bitrate_bps, 0)

    def test_cached_profile_recomputes_filename_derived_source(self):
        import json

        from app.modules.media_probe import MediaProfile, media_profile_from_cache

        payload = json.dumps({
            "resolution": "2160p",
            "source": "WEB-DL",
            "video_bitrate_bps": 14_800_000,
        })

        renamed = media_profile_from_cache(
            payload, source_hint="Movie.2026.BluRay.Remux.2160p.mkv",
        )
        unlabeled = media_profile_from_cache(payload, source_hint="Movie.2026.2160p.mkv")

        self.assertEqual(renamed.source, "Remux")
        self.assertEqual(renamed.video_bitrate_bps, 14_800_000)
        self.assertEqual(unlabeled.source, "")

    def test_ffprobe_payload_renders_trustworthy_profile(self):
        from app.modules.media_probe import parse_ffprobe_payload
        profile = parse_ffprobe_payload({"streams":[
            {"codec_type":"video","codec_name":"hevc","width":3840,"height":2160,"r_frame_rate":"25/1","color_transfer":"bt709","pix_fmt":"yuv420p10le"},
            {"codec_type":"audio","codec_name":"aac","channels":2,"channel_layout":"stereo"},
        ]})
        self.assertEqual(profile.render(), "2160p.SDR.H.265.10-bit.25fps.AAC.2.0")

    def test_ffprobe_payload_renders_inferred_source_and_real_video_bitrate(self):
        from app.modules.media_probe import parse_ffprobe_payload

        profile = parse_ffprobe_payload({
            "streams": [
                {
                    "codec_type": "video", "codec_name": "hevc",
                    "width": 3840, "height": 2160, "avg_frame_rate": "25/1",
                    "color_transfer": "bt709", "pix_fmt": "yuv420p10le",
                    "bit_rate": "14800000",
                },
                {
                    "codec_type": "audio", "codec_name": "aac",
                    "channels": 2, "channel_layout": "stereo",
                },
            ],
            "format": {"bit_rate": "16000000"},
        }, source_hint="Example.2026.2160p.WEB-DL.HEVC.mkv")

        self.assertEqual(profile.source, "WEB-DL")
        self.assertEqual(profile.video_bitrate_bps, 14_800_000)
        self.assertEqual(profile.overall_bitrate_bps, 16_000_000)
        self.assertEqual(profile.bitrate_source, "video_stream")
        self.assertEqual(
            profile.render(),
            "WEB-DL.2160p.SDR.H.265.10-bit.14.8Mbps.25fps.AAC.2.0",
        )

    def test_container_bitrate_is_recorded_but_not_mislabeled_as_video_bitrate(self):
        from app.modules.media_probe import parse_ffprobe_payload

        profile = parse_ffprobe_payload({
            "streams": [{
                "codec_type": "video", "codec_name": "h264",
                "width": 1920, "height": 1080,
            }],
            "format": {"bit_rate": "8000000"},
        }, source_hint="Example.1080p.WEBRip.mkv")

        self.assertEqual(profile.source, "WEBRip")
        self.assertEqual(profile.video_bitrate_bps, 0)
        self.assertEqual(profile.overall_bitrate_bps, 8_000_000)
        self.assertEqual(profile.bitrate_source, "container")
        self.assertEqual(profile.render(), "WEBRip.1080p.H.264.8Mbps")

    def test_ffprobe_profile_merges_missing_fields_from_filename_evidence(self):
        from app.modules.media_probe import parse_ffprobe_payload

        profile = parse_ffprobe_payload({
            "streams": [{
                "codec_type": "video", "codec_name": "hevc",
                "width": 3840, "height": 2160,
            }],
        }, source_hint="Movie.2026.Remux.DoVi.2160p.HEVC.10bit.mkv")

        self.assertEqual(profile.source, "Remux")
        self.assertEqual(profile.dynamic_range, "DoVi")
        self.assertEqual(profile.bit_depth, "10-bit")
        self.assertEqual(profile.video_codec, "H.265")
        self.assertEqual(profile.render(), "Remux.2160p.DoVi.H.265.10-bit")

    def test_ffprobe_payload_detects_explicit_dolby_vision_and_atmos(self):
        from app.modules.media_probe import parse_ffprobe_payload
        profile = parse_ffprobe_payload({"streams": [
            {
                "codec_type": "video", "codec_name": "hevc",
                "side_data_list": [{"side_data_type": "DOVI configuration record"}],
            },
            {
                "codec_type": "audio", "codec_name": "eac3",
                "profile": "Dolby Digital Plus + Dolby Atmos",
            },
        ]})
        self.assertIs(profile.dolby_vision, True)
        self.assertIs(profile.atmos, True)
        self.assertEqual(profile.dynamic_range, "DoVi")

    def test_ffprobe_payload_detects_dolby_vision_profile_and_hdr10_plus(self):
        from app.modules.media_probe import parse_ffprobe_payload

        dovi = parse_ffprobe_payload({"streams": [{
            "codec_type": "video", "codec_name": "hevc",
            "side_data_list": [{
                "side_data_type": "DOVI configuration record", "dv_profile": 8,
            }],
        }]})
        hdr10_plus = parse_ffprobe_payload({"streams": [{
            "codec_type": "video", "codec_name": "hevc",
            "color_transfer": "smpte2084",
            "side_data_list": [{"side_data_type": "HDR Dynamic Metadata SMPTE2094-40"}],
        }]})

        self.assertEqual(dovi.dynamic_range, "DoVi P8")
        self.assertEqual(hdr10_plus.dynamic_range, "HDR10+")

    def test_ffprobe_non_atmos_label_does_not_become_atmos(self):
        from app.modules.media_probe import parse_ffprobe_payload
        profile = parse_ffprobe_payload({"streams": [
            {"codec_type": "video", "codec_name": "hevc"},
            {
                "codec_type": "audio", "codec_name": "eac3",
                "tags": {"title": "EAC3 5.1 Non-Atmos"},
            },
        ]})
        self.assertIsNone(profile.atmos)

    def test_ffprobe_detects_atmos_on_later_audio_stream(self):
        from app.modules.media_probe import parse_ffprobe_payload
        profile = parse_ffprobe_payload({"streams": [
            {"codec_type": "video", "codec_name": "hevc"},
            {
                "codec_type": "audio", "codec_name": "eac3", "channels": 6,
                "disposition": {"default": 1},
            },
            {
                "codec_type": "audio", "codec_name": "truehd", "channels": 8,
                "profile": "Dolby TrueHD with Dolby Atmos",
            },
        ]})
        self.assertEqual(profile.audio_codec, "EAC3")
        self.assertIs(profile.atmos, True)

    def test_missing_color_metadata_does_not_invent_sdr(self):
        from app.modules.media_probe import parse_ffprobe_payload
        profile = parse_ffprobe_payload({"streams":[{"codec_type":"video","codec_name":"hevc","width":3840,"height":2160}]})
        self.assertEqual(profile.render(), "2160p.H.265")

class MediaSourceConsensusTests(unittest.TestCase):
    @staticmethod
    def _plan(file: GuangYaFile, source: str, episode: int, *, season: int = 1):
        match = MatchResult(
            tmdb_id="236000", title="筋肉人：完美超人始祖篇",
            year="2024", media_type="tv", confidence=1.0,
        )
        return OrganizePlan(
            file_id=file.file_id, original_name=file.name, original_path=source,
            original_parent_id=file.parent_id, size=file.size, etag=file.etag,
            match=match, season=season, episode=episode, action="move",
        )

    def _prepare(self, files):
        from app.modules.media_probe import MediaProfile

        organizer = Organizer(client=object(), scraper=object())
        rules = OrganizeRules(media_info_enabled=True)
        plans = [self._plan(file, "incoming", index + 1) for index, file in enumerate(files)]
        by_id = {file.file_id: file for file in files}
        for plan in plans:
            organizer._apply_media_profile_to_move_plan(
                plan, by_id[plan.file_id], rules, plan.match,
                {"season": plan.season, "episode": plan.episode},
                MediaProfile(resolution="1080p", video_codec="H.264"),
            )
        return organizer, rules, plans, by_id

    def test_consistent_same_parent_season_source_fills_unknown_episode(self):
        files = [
            GuangYaFile("1", "Show.S01E01.WEB-DL.mkv", False, 100, "e1", "parent"),
            GuangYaFile("2", "Show.S01E02.WEB-DL.mkv", False, 100, "e2", "parent"),
            GuangYaFile("3", "Show.S01E03.mkv", False, 100, "e3", "parent"),
        ]
        organizer, rules, plans, by_id = self._prepare(files)
        stats = {}

        organizer._apply_media_source_consensus(plans, by_id, rules=rules, stats=stats)

        self.assertEqual(plans[2].media_profile.source, "WEB-DL")
        self.assertIn("WEB-DL.1080p.H.264", plans[2].new_name)
        self.assertEqual(stats["media_source_consensus_groups"], 1)
        self.assertEqual(stats["media_source_consensus_items"], 1)

    def test_conflicting_sources_do_not_fill_unknown_episode(self):
        files = [
            GuangYaFile("1", "Show.S01E01.WEB-DL.mkv", False, 100, "e1", "parent"),
            GuangYaFile("2", "Show.S01E02.BluRay.mkv", False, 100, "e2", "parent"),
            GuangYaFile("3", "Show.S01E03.mkv", False, 100, "e3", "parent"),
        ]
        organizer, rules, plans, by_id = self._prepare(files)
        stats = {}

        organizer._apply_media_source_consensus(plans, by_id, rules=rules, stats=stats)

        self.assertEqual(plans[2].media_profile.source, "")
        self.assertEqual(stats["media_source_consensus_groups"], 0)
        self.assertEqual(stats["media_source_consensus_items"], 0)

    def test_disabled_probe_does_not_mark_background_completion_pending(self):
        file = GuangYaFile(
            "1", "Show.S01E01.WEB-DL.mkv", False, 100, "e1", "parent",
        )
        organizer, _rules, plans, by_id = self._prepare([file])

        organizer._probe_move_plan_profiles(
            plans,
            by_id,
            {},
            cache_prefetched=False,
            rules=OrganizeRules(media_info_enabled=True, media_probe_enabled=False),
            automatic=False,
            cache_only=False,
            stats={},
        )

        self.assertFalse(plans[0].media_probe_pending)


class ManualCorrectionNotificationTests(unittest.TestCase):
    def test_manual_result_sends_media_event_with_target_directory(self):
        from app.modules.organize_correction import CorrectionItem, OrganizeCorrectionService
        service = OrganizeCorrectionService(client=object(), scraper=object())
        preview = {
            "match":{"tmdb_id":"1155281","title":"封神第二部：战火西岐","year":"2025","media_type":"movie"},
            "target_path":"电影/封神第二部：战火西岐 (2025) {tmdb-1155281}",
            "file_name":"封神第二部：战火西岐.2025.1080p.SDR.H.265.AAC.2.0.mkv",
        }
        items=[CorrectionItem(1,"f1","video","p","old.mkv","p","old.mkv",10_000,"e")]
        with patch("app.notifier.send_event", return_value=True) as sent:
            warnings=service._notify_reorganize_result(preview,items)
        self.assertEqual(warnings,[])
        rendered=sent.call_args.args[0]
        self.assertIn("人工识别", str(rendered.fields))
        self.assertIn("电影 / 封神第二部", str(rendered.fields))

class StrmDetailNotificationTests(unittest.TestCase):
    def test_grouped_detail_message_matches_tgto_style(self):
        from app.modules.strm_notifications import build_strm_detail_messages
        changes=[{"action":"generated","directory":"电影/封神第二部：战火西岐 (2025) {tmdb-1155281}","filename":"封神第二部：战火西岐.2025.1080p.strm"}]
        messages=build_strm_detail_messages(changes)
        self.assertEqual(len(messages),1)
        self.assertIn("STRM 文件明细 1/1（1-1/1）",messages[0])
        self.assertIn("--- 🔗 电影/封神第二部",messages[0])
        self.assertIn("└── 封神第二部",messages[0])

    def test_168_changes_create_nine_logical_pages(self):
        from app.modules.strm_notifications import build_strm_detail_messages
        changes=[{"action":"generated","directory":"剧集/示例/Season 1","filename":f"E{i:03d}.strm"} for i in range(1,169)]
        messages=build_strm_detail_messages(changes,max_length=10000)
        self.assertEqual(len(messages),9)
        self.assertIn("1/9（1-20/168）",messages[0])
        self.assertIn("9/9（161-168/168）",messages[-1])

class StrmChangeLedgerIntegrationTests(IsolatedDatabaseTestCase):
    def test_generated_stats_include_only_relative_change_path(self):
        import tempfile
        import uuid
        from pathlib import Path
        from app.modules.strm import STRM_SUBDIR, sync_strm

        source_id = f"detail-{uuid.uuid4().hex}"
        client = type("Client", (), {
            "list_dir": lambda self, file_id: [
                GuangYaFile("v1", "电影.mkv", False, 100, "etag", source_id)
            ]
        })()
        try:
            with tempfile.TemporaryDirectory() as root:
                stats = sync_strm(source_id, "http://media", root, client=client, clean_invalid=False)
                self.assertEqual(stats["changes"], [{
                    "action": "generated",
                    "directory": "根目录",
                    "filename": "电影.strm",
                    "error": "",
                }])
                self.assertNotIn(root, str(stats["changes"]))
                self.assertTrue((Path(root) / STRM_SUBDIR / "电影.strm").is_file())
        finally:
            from app import database as db
            rows = db.list_strm_index(f"guangya:{source_id}")
            db.delete_strm_index_ids(f"guangya:{source_id}", [row["file_id"] for row in rows])

    def test_cleanup_rejects_stale_index_path_outside_current_strm_root(self):
        import tempfile
        from pathlib import Path
        from app.modules.strm import clean_invalid_strm

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            (Path(root) / "光鸭云盘").mkdir(parents=True)
            external = Path(outside) / "unrelated.strm"
            external.write_text("keep", encoding="utf-8")
            rows = [{"file_id": "old", "strm_path": str(external)}]
            with patch("app.modules.strm.db.list_strm_index", return_value=rows), patch(
                "app.modules.strm.db.delete_strm_index_ids"
            ) as delete_rows:
                result = clean_invalid_strm(
                    root, source_key="guangya:source", valid_ids=set(), strm_only=True
                )
                self.assertTrue(result["skipped"])
                self.assertEqual(result["cleaned"], 0)
                self.assertEqual(result["unsafe_paths_count"], 1)
                self.assertTrue(external.exists())
                delete_rows.assert_not_called()

    def test_cleanup_returns_paths_that_were_actually_removed(self):
        import tempfile
        from pathlib import Path
        from app.modules.strm import STRM_SUBDIR, clean_invalid_strm

        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / STRM_SUBDIR / "电影" / "旧片.strm"
            target.parent.mkdir(parents=True)
            target.write_text("old", encoding="utf-8")
            rows = [{
                "file_id": "old", "strm_path": str(target),
                "content_fingerprint": f"sha256:{hashlib.sha256(b'old').hexdigest()}",
            }]
            with patch("app.modules.strm.db.list_strm_index", return_value=rows), patch(
                "app.modules.strm.db.delete_strm_index_ids"
            ):
                result = clean_invalid_strm(
                    root, source_key="guangya:source", valid_ids=set(), strm_only=True
                )
        self.assertEqual(result["removed_paths"], [str(target)])
        self.assertEqual(result["removed_dir_paths"], [str(target.parent)])


class StrmSchedulerDetailNotificationTests(unittest.TestCase):
    def test_organize_detail_notification_is_independent_and_html_safe(self):
        from app.modules import scheduler

        stats = {"changes": [{
            "action": "generated",
            "directory": "电影/测试<&>",
            "filename": "测试<&>.strm",
        }]}
        sent = []
        with patch.object(scheduler, "get_bool", return_value=True), patch.object(
            scheduler, "send_text", side_effect=lambda text, chat_id=None: sent.append(text) or True
        ):
            scheduler.STRMScheduler._notify_details(stats, "organize")
        self.assertEqual(len(sent), 1)
        self.assertIn("STRM 文件明细 1/1", sent[0])
        self.assertIn("&lt;&amp;&gt;", sent[0])
        self.assertNotIn("测试<&>", sent[0])

    def test_non_organize_trigger_does_not_send_file_details(self):
        from app.modules import scheduler

        with patch.object(scheduler, "send_text") as sent:
            scheduler.STRMScheduler._notify_details(
                {"changes": [{"action": "generated", "directory": "电影", "filename": "a.strm"}]},
                "manual",
                enabled_override=True,
            )
        sent.assert_not_called()

class OrganizeNamingUiTests(unittest.TestCase):
    def test_organize_page_does_not_expose_fixed_naming_and_probe_controls(self):
        from pathlib import Path

        html = (Path("app/templates/organize.html").read_text(encoding="utf-8") + Path("app/static/js/organize.js").read_text(encoding="utf-8") + Path("app/static/css/organize.css").read_text(encoding="utf-8"))
        for key in (
            "MEDIA_MOVIE_DIR_TEMPLATE",
            "MEDIA_MOVIE_TEMPLATE",
            "MEDIA_SHOW_DIR_TEMPLATE",
            "MEDIA_TV_TEMPLATE",
            "MEDIA_NAMING_SCOPE",
            "GY_ORGANIZE_RENAME",
            "GY_ORGANIZE_MEDIAINFO",
            "GY_ORGANIZE_MEDIA_PROBE_ENABLED",
            "GY_ORGANIZE_MEDIA_PROBE_TIMEOUT_SECONDS",
        ):
            self.assertNotIn(f'data-key="{key}"', html)
        self.assertNotIn("目录与文件模板", html)
        self.assertNotIn("在线媒体探测", html)

    def test_settings_does_not_duplicate_or_link_organize_templates(self):
        from pathlib import Path

        html = (Path("app/templates/settings.html").read_text(encoding="utf-8") + Path("app/static/js/settings.js").read_text(encoding="utf-8"))
        self.assertNotIn('data-key="MEDIA_MOVIE_TEMPLATE"', html)
        self.assertNotIn('data-key="MEDIA_TV_TEMPLATE"', html)
        self.assertNotIn("前往整理规则", html)
        self.assertNotIn("pages.organize_rules", html)

    def test_manual_correction_explains_search_fields_and_full_target(self):
        from pathlib import Path

        html = (Path("app/templates/logs.html").read_text(encoding="utf-8") + Path("app/static/js/logs.js").read_text(encoding="utf-8"))
        for text in (
            "搜索年份（可选）",
            "剧集位置",
            "影片目录",
            "视频文件",
            "完整目标",
            "命名规则来自「整理规则 → 识别与命名」",
        ):
            self.assertIn(text, html)
