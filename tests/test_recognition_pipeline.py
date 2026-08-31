"""Task 16 确定性 TMDB 识别管线的行为与诊断契约测试。"""
from __future__ import annotations

import inspect
import json
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support import release_parse_fields


def _parse_fields(parser, filename: str, parent_path: str = "") -> dict[str, object]:
    return release_parse_fields(parser.parse_media(filename, parent_path))



class RecognitionContractMixin:
    def recognition_module(self):
        from app.modules import scraper

        required = (
            "RecognitionContext",
            "ReleaseParseToken",
            "ReleaseParseEvidence",
            "ReleaseParseResult",
            "CandidateScoreBreakdown",
            "RecognitionResult",
            "extract_recognition_context",
            "generate_query_variants",
            "score_candidate",
            "decide_threshold",
            "deterministic_recognize",
        )
        missing = [name for name in required if not hasattr(scraper, name)]
        self.assertEqual(missing, [], f"缺少确定性识别接口: {missing}")
        return scraper


class RecognitionStageTests(RecognitionContractMixin, unittest.TestCase):
    def test_public_contract_keeps_parent_path_optional(self):
        scraper = self.recognition_module()

        signature = inspect.signature(scraper.deterministic_recognize)
        self.assertIn("filename", signature.parameters)
        self.assertEqual(signature.parameters["parent_path"].default, "")
        self.assertIn("cleaned_components", scraper.RecognitionContext.__dataclass_fields__)
        self.assertIn("final_score", scraper.CandidateScoreBreakdown.__dataclass_fields__)
        self.assertIn("threshold_decision", scraper.RecognitionResult.__dataclass_fields__)

    def test_release_parse_public_dataclass_and_method_signatures_are_stable(self):
        scraper = self.recognition_module()

        self.assertEqual(
            tuple(scraper.ReleaseParseResult.__dataclass_fields__),
            (
                "filename", "parent_path", "title", "year", "media_type",
                "tmdb_id", "source_season", "source_episode",
                "effective_season", "effective_episode", "context",
                "preprocess_rules", "tokens", "evidence",
            ),
        )
        for field_name in ("preprocess_rules", "tokens", "evidence"):
            self.assertEqual(
                scraper.ReleaseParseResult.__dataclass_fields__[field_name].default,
                (),
            )

        expected_signatures = {
            "parse_media": (
                ("self", "filename", "parent_path", "match"),
                {"parent_path": "", "match": None},
            ),
            "parse_source_position": (
                ("self", "filename", "parent_path"),
                {"parent_path": ""},
            ),
        }
        for method_name, (parameter_names, defaults) in expected_signatures.items():
            signature = inspect.signature(getattr(scraper.TMDBScraper, method_name))
            self.assertEqual(tuple(signature.parameters), parameter_names, method_name)
            for parameter_name, default in defaults.items():
                self.assertEqual(
                    signature.parameters[parameter_name].default,
                    default,
                    f"{method_name}.{parameter_name}",
                )

    def test_release_parse_diagnostic_keeps_source_and_effective_positions(self):
        scraper = self.recognition_module()
        context = scraper.extract_recognition_context(
            "Example.Show.S02E13.mkv", "/Example Show Second Season",
        )
        result = scraper.ReleaseParseResult(
            filename="Example.Show.S02E13.mkv",
            parent_path="/Example Show Second Season",
            title="Example Show",
            year="2024",
            media_type="tv",
            tmdb_id="",
            source_season=2,
            source_episode=13,
            effective_season=2,
            effective_episode=1,
            context=context,
        )

        self.assertEqual(result.effective_episode, 1)
        diagnostic = result.diagnostic_dict()
        self.assertEqual(diagnostic["source_position"], {"season": 2, "episode": 13})
        self.assertEqual(diagnostic["effective_position"], {"season": 2, "episode": 1})
        self.assertEqual(diagnostic["tokens"], [])
        self.assertEqual(diagnostic["evidence"], [])
        json.dumps(diagnostic, ensure_ascii=False)

    def test_release_position_parser_supports_ranges_and_specials_for_resource_sorting(self):
        scraper = self.recognition_module()

        ranged = scraper.parse_release_position("Demo.Show.S02E11-E13.1080p.WEB-DL.mkv")
        compact = scraper.parse_release_position("Demo.Show.2x03.1080p.WEB-DL.mkv")
        special = scraper.parse_release_position("Demo.Show.OVA.03.1080p.mkv")
        oav = scraper.parse_release_position("Demo.Show.OAV.02.1080p.mkv")
        named_special = scraper.parse_release_position("Demo.Show.Special.04.1080p.mkv")
        special_range = scraper.parse_release_position("Demo.Show.Specials.01-03.1080p.mkv")
        season_episode = scraper.parse_release_position(
            "Demo Show Season 3 - 08.mkv"
        )
        menu = scraper.parse_release_position("Menu 1-2.mkv")
        completed = scraper.parse_release_position("Demo Show 01-12 FIN")
        explicit = scraper.parse_release_position("Demo.Show.E01-E03.mkv")
        unknown = scraper.parse_release_position("Demo.Movie.2026.1080p.mkv")

        self.assertEqual(ranged, {"season": 2, "episode": 11, "episode_end": 13})
        self.assertEqual(compact, {"season": 2, "episode": 3, "episode_end": None})
        self.assertEqual(special, {"season": 0, "episode": 3, "episode_end": None})
        self.assertEqual(oav, {"season": 0, "episode": 2, "episode_end": None})
        self.assertEqual(named_special, {"season": 0, "episode": 4, "episode_end": None})
        self.assertEqual(special_range, {"season": 0, "episode": 1, "episode_end": 3})
        self.assertEqual(season_episode, {"season": 3, "episode": 8, "episode_end": None})
        self.assertEqual(menu, {"season": None, "episode": None, "episode_end": None})
        self.assertEqual(completed, {"season": None, "episode": 1, "episode_end": 12})
        self.assertEqual(explicit, {"season": None, "episode": 1, "episode_end": 3})
        self.assertEqual(unknown, {"season": None, "episode": None, "episode_end": None})

    def test_sensitive_source_blocks_network_recognition_without_explicit_identity(self):
        scraper_module = self.recognition_module()
        matcher = scraper_module.TMDBScraper()

        with patch(
            "app.modules.media_aliases.lookup_manual_alias", return_value=None
        ), patch.object(
            matcher, "_get_lock", return_value=None
        ), patch.object(matcher, "deterministic_recognize") as deterministic:
            result = matcher.match(
                "Example.api_key=abcdefgh123456.S01E01.mkv",
                media_type_hint="tv",
            )

        deterministic.assert_not_called()
        self.assertTrue(result.need_confirm)
        self.assertEqual(result.status, "low_confidence")
        self.assertEqual(result.matched_by, "sensitive_source")
        self.assertIn("已阻止联网标题识别", result.error)

    def test_release_parse_result_unifies_source_effective_position_and_diagnostics(self):
        scraper = self.recognition_module()
        match = scraper.MatchResult(
            title="示例剧",
            year="2024",
            tmdb_id="12345",
            media_type="tv",
            preprocess_evaluated=True,
            effective_season=2,
            effective_episode=1,
            metadata={
                "episode_mapping": {
                    "changed": True,
                    "source_season": 2,
                    "source_episode": 13,
                    "target_season": 2,
                    "target_episode": 1,
                }
            },
        )

        result = scraper.TMDBScraper().parse_media(
            "Example.Show.S02E13.2024.1080p.WEB-DL.mkv",
            "/Example Show Second Season",
            match,
        )

        self.assertIsInstance(result, scraper.ReleaseParseResult)
        self.assertEqual((result.source_season, result.source_episode), (2, 13))
        self.assertEqual((result.effective_season, result.effective_episode), (2, 1))
        self.assertEqual(result.effective_episode, 1)
        diagnostic = result.diagnostic_dict()
        self.assertEqual(diagnostic["source_position"], {"season": 2, "episode": 13})
        self.assertEqual(diagnostic["effective_position"], {"season": 2, "episode": 1})
        self.assertTrue(any(item["kind"] == "episode" for item in diagnostic["evidence"]))
        self.assertEqual(match.metadata["release_parse"], diagnostic)
        json.dumps(diagnostic, ensure_ascii=False)

    def test_chinese_numeral_episode_parser_is_strict_and_lossless(self):
        scraper = self.recognition_module()

        numbered = scraper.extract_recognition_context("三体 第十二集 [1080p].mkv", "")
        compound = scraper.extract_recognition_context("三体 第两季 第〇三集.mkv", "")
        topic = scraper.extract_recognition_context("三体 第十五话题.mkv", "")
        digest = scraper.extract_recognition_context("三体 第十二集锦.mkv", "")

        self.assertEqual((numbered.normalized_title, numbered.episode), ("三体", 12))
        self.assertEqual((compound.normalized_title, compound.season, compound.episode), ("三体", 2, 3))
        self.assertEqual((topic.normalized_title, topic.episode), ("三体 第十五话题", None))
        self.assertEqual((digest.normalized_title, digest.episode), ("三体 第十二集锦", None))

    def test_season_ranges_fail_closed_until_the_model_can_express_them(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "Demo.Show.S01-S03.1080p.WEB-DL.mkv", ""
        )
        parsed = _parse_fields(scraper.TMDBScraper(),
            "Demo.Show.S01-S03.1080p.WEB-DL.mkv"
        )

        self.assertEqual(context.normalized_title, "Demo Show")
        self.assertIsNone(context.season)
        self.assertIsNone(context.episode)
        self.assertIsNone(parsed["season"])
        self.assertIsNone(parsed["episode"])

    def test_cjk_tail_release_group_is_candidate_only_and_does_not_pollute_title(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "葬送的芙莉莲 S01E01 - 桜都字幕组.mkv", ""
        )

        self.assertEqual(context.normalized_title, "葬送的芙莉莲")
        self.assertEqual((context.season, context.episode), (1, 1))
        self.assertEqual(
            context.cleaned_components.get("candidate_release_groups"),
            ["桜都字幕组"],
        )

    def test_normalization_removes_release_prefix_checksum_group_and_quality_noise(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "[HDSky]www.MovieSite.com@繁花.Shanghai.Blank.2023.2160p.WEB-DL.H265-DreamHD[A1B2C3D4].mkv",
            "",
        )

        self.assertEqual(context.filename_year, "2023")
        self.assertNotIn("HDSky", context.normalized_title)
        self.assertNotIn("MovieSite", context.normalized_title)
        self.assertNotIn("2160p", context.normalized_title)
        self.assertNotIn("DreamHD", context.normalized_title)
        self.assertNotIn("A1B2C3D4", context.normalized_title)
        self.assertIn("繁花", context.title_variants)
        self.assertIn("Shanghai Blank", context.title_variants)
        self.assertIn("[HDSky]", context.cleaned_components["release_prefixes"])
        self.assertIn("A1B2C3D4", context.cleaned_components["checksums"])
        self.assertIn("DreamHD", context.cleaned_components["release_groups"])

    def test_normalization_removes_parenthesized_trailing_checksum(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "CONAN - 124 [1080p][CHT_JP](CBAF871F).mp4",
            "",
        )

        self.assertEqual(context.normalized_title, "CONAN")
        self.assertEqual(context.episode, 124)
        self.assertNotIn("CBAF871F", context.normalized_title)
        self.assertIn("CBAF871F", context.cleaned_components["checksums"])

    def test_bracketed_release_date_is_removed_only_when_calendar_valid(self):
        scraper = self.recognition_module()

        valid = scraper.extract_recognition_context(
            "Chibi Maruko-chan - 1540 [2026.07.26] [1080p].mp4",
            "",
        )
        invalid = scraper.extract_recognition_context(
            "Example Show [2026.19.40] - 01.mkv",
            "",
        )

        self.assertEqual(valid.normalized_title, "Chibi Maruko chan")
        self.assertEqual(valid.episode, 1540)
        self.assertIn("2026.07.26", valid.cleaned_components["noise_tokens"])
        self.assertNotIn("07 26", valid.normalized_title)
        self.assertFalse(scraper._is_bracket_noise("2026.19.40"))
        self.assertIn("19 40", invalid.normalized_title)

    def test_multilingual_separator_and_unbracketed_release_date_are_preserved_safely(self):
        scraper = self.recognition_module()

        valid = scraper.extract_recognition_context(
            "繁花｜Blossoms Shanghai.2026-08-13.1080p.mkv", ""
        )
        invalid = scraper.extract_recognition_context(
            "Daily.Show.2026.13.40.1080p.mkv", ""
        )

        self.assertIn("繁花", valid.title_variants)
        self.assertIn("Blossoms Shanghai", valid.title_variants)
        self.assertIn("2026-08-13", valid.cleaned_components["noise_tokens"])
        self.assertNotIn("08 13", valid.normalized_title)
        self.assertIn("13 40", invalid.normalized_title)
        self.assertNotIn("2026.13.40", invalid.cleaned_components["noise_tokens"])

    def test_explicit_subtitle_language_bracket_is_noise_but_title_phrase_is_preserved(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "Kimetsu no Yaiba - 01 [简日双语内嵌] [1080p].mp4",
            "",
        )

        self.assertEqual(context.normalized_title, "Kimetsu no Yaiba")
        self.assertEqual(context.episode, 1)
        self.assertIn("简日双语内嵌", context.cleaned_components["noise_tokens"])
        self.assertTrue(scraper._is_bracket_noise("繁日双语MP4"))
        self.assertFalse(scraper._is_bracket_noise("双语人生"))

    def test_dotted_title_ending_in_me_is_not_mistaken_for_site_prefix(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "LIAR.GAME.S01E16.1080p.WEB-DL.mkv",
            "",
        )

        self.assertEqual(context.normalized_title, "LIAR GAME")
        self.assertEqual((context.season, context.episode), (1, 16))
        self.assertNotIn("LIAR.GAME", context.cleaned_components["release_prefixes"])

    def test_real_me_hostname_prefix_is_still_removed(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "foo.me@Example.Show.S01E01.1080p.WEB-DL.mkv",
            "",
        )

        self.assertEqual(context.normalized_title, "Example Show")
        self.assertEqual((context.season, context.episode), (1, 1))
        self.assertIn("foo.me", context.cleaned_components["release_prefixes"])

    def test_cr_source_marker_before_webrip_is_not_part_of_title(self):
        scraper = self.recognition_module()

        compact = scraper.extract_recognition_context(
            "平凡职业造就世界最强 第三季.2024.1080p.CR.WEBRip.x264.AAC.CHS-LxyLab.mkv",
            "",
        )
        spaced = scraper.extract_recognition_context(
            "平凡职业造就世界最强 第三季.2024.1080p.C R WEBRip.x264.AAC.CHS-LxyLab.mkv",
            "",
        )
        underscored = scraper.extract_recognition_context(
            "平凡职业造就世界最强 第三季.2024.1080p.CR_WEBRip.x264.AAC.CHS-LxyLab.mkv",
            "",
        )
        split_underscored = scraper.extract_recognition_context(
            "平凡职业造就世界最强 第三季.2024.1080p.C_R_WEBRip.x264.AAC.CHS-LxyLab.mkv",
            "",
        )

        self.assertNotIn(" CR ", f" {compact.normalized_title} ")
        self.assertNotIn(" C R ", f" {spaced.normalized_title} ")
        self.assertIn("平凡职业造就世界最强", compact.normalized_title)
        self.assertIn("平凡职业造就世界最强", spaced.normalized_title)
        self.assertNotIn("WEBRip", underscored.normalized_title)
        self.assertNotIn("WEBRip", split_underscored.normalized_title)

    def test_numbered_remaster_suffix_is_primary_release_noise(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "Yu-Gi-Oh! Duel Monsters GX 20th Remaster - 069.mkv", "",
        )

        self.assertEqual(context.normalized_title, "Yu Gi Oh! Duel Monsters GX")
        self.assertEqual((context.season, context.episode), (None, 69))
        self.assertIn(
            "Yu Gi Oh! Duel Monsters GX 20th Remaster",
            context.title_variants,
        )
        self.assertEqual(
            scraper.generate_query_variants(context)[0],
            "Yu Gi Oh! Duel Monsters GX",
        )

    def test_bare_remaster_word_remains_part_of_possible_official_title(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "The Remaster.2024.mkv", "",
        )

        self.assertEqual(context.normalized_title, "The Remaster")

    def test_legacy_vhs_technical_tail_removes_only_release_multi_token(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "Tottoi.トトイ.1992.VHSrip.480p.x264.AAC.MULTi-Tinosoft.mkv",
            "",
        )

        self.assertEqual(context.normalized_title, "Tottoi トトイ")
        self.assertEqual(context.filename_year, "1992")
        self.assertEqual(context.media_type, "movie")
        self.assertEqual(context.cleaned_components["release_groups"], ["Tinosoft"])
        self.assertTrue(
            {"VHSrip", "MULTi"}.issubset(
                set(context.cleaned_components["noise_tokens"])
            )
        )

    def test_multi_word_without_vhs_technical_tail_is_not_globally_removed(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context("The Multi.2024.mkv", "")

        self.assertEqual(context.normalized_title, "The Multi")

    def test_unknown_release_group_and_structural_brackets_create_clean_title_candidate(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "[Studio GreenTea] Virgin Punk Clockwork Girl "
            "[Movie v2][BDRip][HEVC-10bit 1080p AAC][JPTC].mkv"
        )

        self.assertEqual(context.normalized_title, "Virgin Punk Clockwork Girl")
        self.assertEqual(context.filename_title, "Virgin Punk Clockwork Girl")
        self.assertIn("Virgin Punk Clockwork Girl", context.title_variants)
        self.assertEqual(
            context.cleaned_components["candidate_release_groups"],
            ["Studio GreenTea"],
        )
        self.assertEqual(context.cleaned_components["media_kinds"], ["Movie v2"])
        self.assertEqual(context.cleaned_components["release_versions"], ["v2"])
        self.assertEqual(context.cleaned_components["language_tags"], ["JPTC"])

    def test_broadcaster_technical_brackets_create_clean_release_candidate(self):
        scraper = self.recognition_module()
        cases = (
            (
                "[shincaps] Hidarikiki no Eren - 06 "
                "(BS-NTV 1440x1080 MPEG2 AAC).ts",
                "Hidarikiki no Eren",
                ["shincaps"],
            ),
            (
                "[shincaps] BLEACH Sennen Kessen-hen ~Kashin-tan~ - 02 "
                "(AT-X 1440x1080 MPEG2 AAC).ts",
                "BLEACH Sennen Kessen hen ~Kashin tan~",
                ["shincaps"],
            ),
            (
                "[shincaps] Azur Lane Bisoku Zenshin! Ni!! - 06 "
                "(BS11 1920x1080 MPEG2 AAC).ts",
                "Azur Lane Bisoku Zenshin!",
                ["shincaps"],
            ),
            (
                "[shincaps] Sample Broadcast Show - 01 "
                "(NBN TV 1080p HEVC AAC).ts",
                "Sample Broadcast Show",
                ["shincaps"],
            ),
            (
                "[Dynamis One] Ever Night - 06 "
                "(B-Global Donghua 1920x832 HEVC AAC MKV) [B2088D0F].mkv",
                "Ever Night",
                [],
            ),
        )

        for filename, expected_title, expected_candidate_groups in cases:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename)
                self.assertEqual(context.normalized_title, expected_title)
                self.assertEqual(
                    context.cleaned_components["candidate_release_groups"],
                    expected_candidate_groups,
                )
                self.assertNotIn("MPEG2", context.normalized_title)

    def test_b_global_donghua_release_keeps_title_and_absolute_episode_clean(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()
        files = (
            (6, "B2088D0F"),
            (7, "4FD19EAF"),
            (18, "314B281E"),
            (19, "6CE52CDF"),
        )

        for episode, checksum in files:
            filename = (
                f"[Dynamis One] Ever Night - {episode:02d} "
                f"(B-Global Donghua 1920x832 HEVC AAC MKV) [{checksum}].mkv"
            )
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "")
                parsed = _parse_fields(parser, filename)

                self.assertEqual(context.normalized_title, "Ever Night")
                self.assertEqual((context.season, context.episode), (None, episode))
                self.assertEqual(parsed["title"], "Ever Night")
                self.assertEqual((parsed["season"], parsed["episode"]), (None, episode))
                self.assertEqual(parser.clean_title(filename), "Ever Night")
                self.assertEqual(context.filename_year, "")
                self.assertIn(checksum, context.cleaned_components["checksums"])
                self.assertIn(
                    "[Dynamis One]", context.cleaned_components["release_prefixes"]
                )

        self.assertFalse(scraper._is_bracket_noise("B-Global Donghua"))
        self.assertFalse(scraper._is_bracket_noise("B-Global Donghua 1920x832"))
        self.assertFalse(scraper._is_bracket_noise("B-Global Donghua HEVC AAC MKV"))

        source_context = scraper.extract_recognition_context(
            "[Dynamis One] Ever Night - 06 "
            "(B-Global Donghua 1920x832 HEVC AAC MKV) [B2088D0F].mkv"
        )
        processed_context = scraper.extract_recognition_context("Ever Night - 06.mkv")
        scraper._inherit_source_query_provenance(processed_context, source_context)
        self.assertEqual(
            scraper._explicit_animation_source_marker(processed_context), "Donghua",
        )

    def test_stylized_romaji_ni_second_season_is_fail_closed_and_tmdb_verifiable(self):
        scraper = self.recognition_module()
        filename = (
            "[shincaps] Azur Lane Bisoku Zenshin! Ni!! - 06 "
            "(BS11 1920x1080 MPEG2 AAC).ts"
        )

        context = scraper.extract_recognition_context(filename)

        self.assertEqual(context.normalized_title, "Azur Lane Bisoku Zenshin!")
        self.assertEqual((context.season, context.episode), (2, 6))
        self.assertEqual(
            scraper.parse_release_position(filename),
            {"season": 2, "episode": 6, "episode_end": None},
        )
        # 普通罗马字助词、单感叹号和没有标题尾部感叹号的写法都不能
        # 被解释成第二季，避免把正式片名中的 ``ni`` 误删。
        for rejected in (
            "Boku wa Kimi ni - 06 [1080p].mkv",
            "Example Show Ni! - 06 [1080p].mkv",
            "Example Show Ni!! - 06 [1080p].mkv",
        ):
            with self.subTest(rejected=rejected):
                rejected_context = scraper.extract_recognition_context(rejected)
                self.assertIsNone(rejected_context.season)

    def test_punctuated_numeric_sequel_marker_maps_to_verified_season(self):
        scraper = self.recognition_module()
        filename = (
            "[shincaps] 碧藍航線 微速前進!2!! - 06 "
            "(BS11 1920x1080 MPEG2 AAC).ts"
        )

        context = scraper.extract_recognition_context(filename)

        self.assertEqual(context.normalized_title, "碧藍航線 微速前進!")
        self.assertEqual((context.season, context.episode), (2, 6))
        self.assertEqual(
            scraper.parse_release_position(filename),
            {"season": 2, "episode": 6, "episode_end": None},
        )

    def test_punctuated_numeric_sequel_marker_requires_strict_release_shape(self):
        scraper = self.recognition_module()

        for rejected in (
            "Example Show!2! - 06 [1080p].mkv",
            "Example Show 2!! - 06 [1080p].mkv",
            "Example Show!2!! trailer [1080p].mkv",
            "86!2!! - 06 [1080p].mkv",
        ):
            with self.subTest(rejected=rejected):
                context = scraper.extract_recognition_context(rejected)
                self.assertIsNone(context.season)

    def test_broadcaster_words_without_full_technical_evidence_are_preserved(self):
        scraper = self.recognition_module()

        for content in (
            "NBN TV",
            "BS-NTV Documentary",
            "Dual Hearts 1080p",
        ):
            with self.subTest(content=content):
                self.assertFalse(scraper._is_bracket_noise(content))

    def test_exact_dubbing_bracket_is_noise_but_descriptive_title_is_preserved(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "[台配] 博人传 火影次世代 Boruto Naruto Next Generations - 01.mkv",
            "",
        )

        self.assertNotIn("台配", context.normalized_title)
        self.assertIn("博人传", context.normalized_title)
        self.assertTrue(scraper._is_bracket_noise("台配"))
        self.assertTrue(scraper._is_bracket_noise("Korean Audio"))
        self.assertFalse(scraper._is_bracket_noise("台配特别篇"))
        self.assertFalse(scraper._is_bracket_noise("Korean Audio Commentary"))

        korean_audio = scraper.extract_recognition_context(
            "Toukutsu Ou - 01 [Korean Audio] [1080p][WEB-DL].mkv",
            "",
        )
        self.assertEqual(korean_audio.normalized_title, "Toukutsu Ou")
        self.assertIn(
            "Korean Audio", korean_audio.cleaned_components["noise_tokens"],
        )

    def test_combined_dub_and_subtitle_bracket_is_removed_from_release_folder(self):
        scraper = self.recognition_module()
        folder = (
            "【高清剧集网发布 www.BPHDTV.com】飞出个未来.第十二季"
            "[全10集][国语配音+中文字幕].Futurama.S12.2024.1080p."
            "DSNP.WEB-DL.DDP5.1.H264-ZeroTV"
        )

        self.assertTrue(scraper._is_bracket_noise("国语配音+中文字幕"))
        self.assertFalse(scraper._is_bracket_noise("国语配音的故事"))
        self.assertFalse(scraper._is_bracket_noise("中文配音"))

        root_context = scraper.extract_recognition_context(folder, "/")
        self.assertEqual(root_context.normalized_title, "飞出个未来 Futurama")
        self.assertEqual(root_context.season, 12)
        self.assertEqual(root_context.media_type, "tv")
        self.assertIn(
            "国语配音+中文字幕",
            root_context.cleaned_components["noise_tokens"],
        )

        for category in ("电视剧", "动漫"):
            with self.subTest(category=category):
                child_context = scraper.extract_recognition_context(
                    "01.mkv", f"/{category}/{folder}",
                )
                self.assertEqual(
                    child_context.normalized_title, "飞出个未来 Futurama",
                )
                self.assertEqual(
                    (child_context.media_type, child_context.season, child_context.episode),
                    ("tv", 12, 1),
                )
                self.assertNotIn("国语配音", child_context.normalized_title)
                self.assertNotIn("中文字幕", child_context.normalized_title)

    def test_real_guangya_release_folder_metadata_does_not_pollute_titles(self):
        scraper = self.recognition_module()
        samples = (
            (
                "电视剧",
                "【高清剧集网发布 www.PTHDTV.com】师兄太稳健[60帧率版本]"
                "[高码版][第16-17集][国语配音+中文字幕].2026.2160p.HQ."
                "WEB-DL.H265.HDR.60fps.AAC-BlackTV",
                "师兄太稳健",
            ),
            (
                "动漫",
                "【高清剧集网发布 www.DDHDTV.com】辛普森一家 第十季[全23集]"
                "[粤英多音轨+简繁英字幕].The.Simpsons.S10.1998.1080p."
                "DSNP.WEB-DL.H264.DDP.5.1-ZeroTV",
                "辛普森一家 The Simpsons",
            ),
            (
                "电视剧",
                "【高清剧集网发布 www.BBEGGE.com】入侵.第三季[全10集]"
                "[简繁英字幕].Invasion.S03.2160p.Apple.TV+.WEB-DL.DDP.5.1."
                "Atmos.HDR10+.H.265-BlackTV",
                "入侵 Invasion",
            ),
            (
                "电视剧",
                "【高清剧集网发布 www.BBHDTV.com】地狱来的芳邻[全8集]"
                "[简繁英字幕].The.Burbs.S01.1080p.HBOMax.WEB-DL.DDP.5.1."
                "H.264-BlackTV",
                "地狱来的芳邻 The Burbs",
            ),
            (
                "动漫",
                "【高清剧集网发布 www.DDHDTV.com】银河英雄传说 Die Neue These "
                "邂逅[全12集][中文字幕].2018.1080p.BluRay.x264.DTS.3.1-ZeroTV",
                "银河英雄传说 Die Neue These 邂逅",
            ),
            (
                "动漫",
                "【高清剧集网发布 www.DDHDTV.com】辛普森一家 第二十五季"
                "[第01-22集][简繁英字幕].The.Simpsons.S25.2013.1080p."
                "DSNP.WEB-DL.H264.DDP.5.1-ZeroTV",
                "辛普森一家 The Simpsons",
            ),
            (
                "电视剧",
                "明日传奇.第一季全集.DCs.Legends.of.Tomorrow.S01E01-16."
                "2016.HD1080P.X264.AAC.English.CHS-ENG.Mp4Ba",
                "明日传奇 DCs Legends of Tomorrow",
            ),
            (
                "电视剧",
                "DCs.Legends.of.Tomorrow.S04.1080p.AMZN.WEBRip.DDP5.1."
                "x264-QOQ[rartv]",
                "DCs Legends of Tomorrow",
            ),
        )

        for category, folder, expected_title in samples:
            with self.subTest(folder=folder):
                directory_context = scraper.extract_recognition_context(
                    folder, f"/{category}",
                )
                child_context = scraper.extract_recognition_context(
                    "01.mkv", f"/{category}/{folder}",
                )
                self.assertEqual(directory_context.normalized_title, expected_title)
                self.assertEqual(child_context.normalized_title, expected_title)
                self.assertEqual(child_context.media_type, "tv")

    def test_release_language_name_requires_technical_and_language_code_context(self):
        scraper = self.recognition_module()

        technical = scraper.extract_recognition_context(
            "Example.Show.S01E01.1080p.AAC.English.CHS-ENG.mkv", "",
        )
        formal_title = scraper.extract_recognition_context(
            "The.English.S01E01.2022.1080p.WEB-DL.CHS.mkv", "",
        )

        self.assertEqual(technical.normalized_title, "Example Show")
        self.assertEqual(formal_title.normalized_title, "The English")

    def test_explicit_quoted_release_wrapper_is_strict_source_title_evidence(self):
        scraper = self.recognition_module()

        verified = scraper._verify_source_title_anchor(
            ["Animatica「北斗之拳 拳王軍雜兵們的輓歌」"],
            ["北斗之拳 拳王軍雜兵們的輓歌"],
            season=1,
        )
        derivative = scraper._verify_source_title_anchor(
            ["Animatica「北斗之拳 新作」"],
            ["北斗之拳"],
            season=1,
        )
        generic_wrapper = scraper._verify_source_title_anchor(
            ["Official Channel「北斗之拳」"],
            ["北斗之拳"],
            season=1,
        )

        self.assertTrue(verified[0])
        self.assertEqual(verified[4], "verified")
        self.assertFalse(derivative[0])
        self.assertEqual(derivative[4], "distinctive_source_title_remainder")
        self.assertFalse(generic_wrapper[0])
        self.assertEqual(
            generic_wrapper[4], "distinctive_source_title_remainder",
        )

    def test_streaming_platform_bracket_requires_companion_technical_evidence(self):
        scraper = self.recognition_module()

        self.assertTrue(scraper._is_bracket_noise("WETV.WEB-DL 1080P AVC AAC"))
        self.assertTrue(scraper._is_bracket_noise("TVING Web-DL"))
        self.assertFalse(scraper._is_bracket_noise("WETV"))
        self.assertFalse(scraper._is_bracket_noise("WETV Original"))

        context = scraper.extract_recognition_context(
            "[Gecko] APPLES (2026) - S01E01 "
            "[WETV.WEB-DL 1080P AVC AAC].mkv",
            "",
        )
        self.assertEqual(context.normalized_title, "APPLES")
        self.assertNotIn("WETV", context.title_variants)

    def test_streaming_platform_technical_bracket_can_include_release_year(self):
        scraper = self.recognition_module()

        self.assertTrue(
            scraper._is_bracket_noise("2026 WETV WEB-DL 1080P AVC AAC")
        )
        self.assertFalse(scraper._is_bracket_noise("2026"))
        self.assertFalse(scraper._is_bracket_noise("2026 WETV Original"))
        self.assertFalse(
            scraper._is_bracket_noise("APPLES 2026 WEB-DL 1080P")
        )

        context = scraper.extract_recognition_context(
            "[Gecko] APPLES (2026) - S01E01 "
            "[2026 WETV WEB-DL 1080P AVC AAC].mkv",
            "",
        )
        self.assertEqual(context.filename_year, "2026")
        self.assertEqual(context.normalized_title, "APPLES")
        self.assertEqual(scraper.generate_query_variants(context)[0], "APPLES")
        self.assertNotIn("WETV", context.normalized_title)
        self.assertNotIn("Gecko", context.normalized_title)

    def test_cjk_episode_word_and_domain_suffix_inside_technical_bracket_are_cleaned(self):
        scraper = self.recognition_module()
        filename = (
            "[OpaqueGroup] - 百日成王 - 第15话 - "
            "[1080p BILIBILI COM WEB-DL].mp4"
        )

        context = scraper.extract_recognition_context(filename)
        position = scraper.parse_release_position(filename)

        self.assertEqual(context.normalized_title, "百日成王")
        self.assertEqual((context.season, context.episode), (None, 15))
        self.assertEqual(position, {
            "season": None,
            "episode": 15,
            "episode_end": None,
        })
        self.assertEqual(
            context.cleaned_components["candidate_release_groups"],
            ["OpaqueGroup"],
        )
        self.assertNotIn("第15话", context.normalized_title)
        self.assertNotIn("BILIBILI", context.normalized_title)
        self.assertNotIn("COM", context.normalized_title)

    def test_domain_suffix_is_not_bracket_noise_without_technical_evidence(self):
        scraper = self.recognition_module()

        self.assertFalse(scraper._is_bracket_noise("BILIBILI COM"))
        self.assertFalse(scraper._is_bracket_noise("APPLES COM WEB-DL 1080p"))

    def test_cjk_episode_word_does_not_consume_longer_title_word(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "作品 第15话题 WEB-DL 1080p.mkv"
        )

        self.assertIsNone(context.episode)
        self.assertIn("第15话题", context.normalized_title)

    def test_structured_broadcaster_noise_does_not_turn_title_words_into_groups(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()
        cases = (
            "[Part] Long Normal Series Title - 01 "
            "(BS-NTV 1440x1080 MPEG2 AAC).ts",
            "[Cour] Long Normal Series Title - 01 "
            "(AT-X 1440x1080 MPEG2 AAC).ts",
            "[Final] Long Normal Series Title - 01 "
            "(BS11 1920x1080 MPEG2 AAC).ts",
        )

        for filename in cases:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename)
                self.assertEqual(
                    context.cleaned_components["candidate_release_groups"], []
                )
                self.assertIsNone(parser._unknown_release_group_candidate(filename))

    def test_bracketed_real_title_is_not_removed_by_unknown_group_projection(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context("[Oshi no Ko] - 01.mkv")

        self.assertIn("Oshi no Ko", context.normalized_title)
        self.assertEqual(
            context.cleaned_components["candidate_release_groups"], []
        )

    def test_title_like_multiword_prefix_is_not_learned_as_release_group(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()
        cases = (
            "[The Movie] Long Normal Series Title - 01 [1080p].mkv",
            "[The 86] Long Normal Series Title - 01 [1080p].mkv",
            "[Part 2] Long Normal Series Title - 01 [1080p].mkv",
            "[Cour II] Long Normal Series Title - 01 [1080p].mkv",
            "[Final Fantasy VII] Long Normal Series Title - 01 [1080p].mkv",
        )

        for filename in cases:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename)
                self.assertEqual(
                    context.cleaned_components["candidate_release_groups"], []
                )
                self.assertIsNone(parser._unknown_release_group_candidate(filename))

    def test_single_token_unknown_release_group_keeps_clean_candidate(self):
        scraper = self.recognition_module()
        context = scraper.extract_recognition_context(
            "[Doomdos] - Grand Blue Dreaming 3 - 6 [1080p IQ WEB-DL].mkv"
        )

        self.assertEqual(context.normalized_title, "Grand Blue Dreaming")
        self.assertEqual((context.season, context.episode), (3, 6))
        self.assertEqual(
            context.cleaned_components["candidate_release_groups"], ["Doomdos"]
        )

    def test_common_release_groups_and_source_prefixes_are_removed_safely(self):
        scraper = self.recognition_module()
        cases = [
            (
                "[DBD-Raws] Example Show - 01 [1080p][WEB-DL].mkv",
                "Example Show",
                "[DBD-Raws]",
            ),
            (
                "[三明治摆烂组] Example Show - 02 [1080p][WEB-DL].mkv",
                "Example Show",
                "[三明治摆烂组]",
            ),
            (
                "[Dynamis One] Example Show - 03 [1080p][WEB-DL].mkv",
                "Example Show",
                "[Dynamis One]",
            ),
            (
                "【高清影视之家发布 www.HDBTHD.com】Example.Movie.2026.2160p.WEB-DL-DreamHD.mkv",
                "Example Movie",
                "【高清影视之家发布 www.HDBTHD.com】",
            ),
        ]

        for filename, title, prefix in cases:
            with self.subTest(filename=filename):
                context = self.recognition_module().extract_recognition_context(filename, "")
                self.assertEqual(context.normalized_title, title)
                self.assertIn(prefix, context.cleaned_components["release_prefixes"])

    def test_simplified_traditional_and_compound_chinese_release_groups_are_removed(self):
        scraper = self.recognition_module()
        cases = [
            ("[绿茶字幕组] Example Show - 01 [1080p][WEB-DL].mkv", 1),
            ("[綠茶字幕組] Example Show - 02 [1080p][WEB-DL].mkv", 2),
            ("[爱恋字幕社] Example Show - 03 [1080p][WEB-DL].mkv", 3),
            ("[北宇治字幕组] Example Show - 04 [1080p][WEB-DL].mkv", 4),
            ("[猎户发布组] Example Show - 05 [1080p][WEB-DL].mkv", 5),
            ("[沸班亚马制作组] Example Show - 06 [1080p][WEB-DL].mkv", 6),
            ("[獵戶壓制部] Example Show - 07 [1080p][WEB-DL].mkv", 7),
            ("[风之圣殿] Example Show - 08 [1080p][WEB-DL].mkv", 8),
            ("[離譜Sub] Example Show - 09 [1080p][WEB-DL].mkv", 9),
            ("[喵萌奶茶屋] Example Show - 10 [1080p][WEB-DL].mkv", 10),
            ("[学院部＆不鸽] Example Show - 11 [1080p][WEB-DL].mkv", 11),
            ("[天月动漫&发布组] Example Show - 12 [1080p][WEB-DL].mkv", 12),
            ("[夜鶯家族&YYQ字幕組] Example Show - 13 [1080p][WEB-DL].mkv", 13),
            ("[Prejudice-Studio] Example Show - 14 [1080p][WEB-DL].mkv", 14),
        ]

        for filename, episode in cases:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "")
                self.assertEqual(context.normalized_title, "Example Show")
                self.assertEqual(context.episode, episode)
                self.assertEqual(len(context.cleaned_components["release_prefixes"]), 1)

    def test_unknown_short_bracketed_title_prefix_is_preserved(self):
        scraper = self.recognition_module()
        cases = [
            ("[The] Last of Us S01E01 1080p WEB-DL.mkv", "The Last of Us"),
            ("[86] Eighty Six S01E01 1080p WEB-DL.mkv", "86 Eighty Six"),
        ]

        for filename, title in cases:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "")
                self.assertEqual(context.normalized_title, title)
                self.assertEqual(context.cleaned_components["release_prefixes"], [])

    def test_known_compound_and_spaced_release_group_variants_are_removed(self):
        scraper = self.recognition_module()
        cases = [
            "[Nekomoe kissaten & LoliHouse] Maou Gakuin no Futekigousha - 13 [1080p].mkv",
            "[Nekomoe kissaten＆LoliHouse] Maou Gakuin no Futekigousha - 13 [1080p].mkv",
            "[Nekomoe kissaten / LoliHouse] Maou Gakuin no Futekigousha - 13 [1080p].mkv",
            "[Nekomoe kissaten、LoliHouse] Maou Gakuin no Futekigousha - 13 [1080p].mkv",
            "[Loli House] Maou Gakuin no Futekigousha - 13 [1080p].mkv",
            "[H Enc] Maou Gakuin no Futekigousha - 13 [1080p].mkv",
        ]

        for filename in cases:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "")
                self.assertEqual(context.normalized_title, "Maou Gakuin no Futekigousha")
                self.assertEqual(context.episode, 13)
                self.assertTrue(context.cleaned_components["release_prefixes"])

    def test_tail_release_group_does_not_consume_normalized_media_tokens(self):
        scraper = self.recognition_module()
        context = scraper.extract_recognition_context(
            "Example.Show.2026.S01E06-Web-DL.1080p.h264.AAC.mkv",
            "",
        )
        tagged = scraper.TMDBScraper.parse_resource_tags(
            "Example.Show.2026.S01E06-Web-DL.1080p.h264.AAC.mkv"
        )

        self.assertEqual(context.normalized_title, "Example Show")
        self.assertEqual(context.cleaned_components["release_groups"], [])
        self.assertEqual(tagged["release_group"], "")

    def test_parent_folder_supplies_title_year_media_type_and_season(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "E03.1080p.WEB-DL.mkv",
            "/剧集/幕府将军 Shogun (2024)/Season 02",
        )

        self.assertEqual(context.folder_title, "幕府将军 Shogun")
        self.assertEqual(context.folder_year, "2024")
        self.assertEqual(context.filename_year, "")
        self.assertEqual(context.media_type, "tv")
        self.assertEqual(context.season, 2)
        self.assertEqual(context.episode, 3)
        self.assertEqual(context.normalized_title, "幕府将军 Shogun")

    def test_bare_dash_episode_inherits_ordinal_parent_season(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "Arifureta Shokugyou de Sekai Saikyou - 01.mkv",
            "/动漫/[H-Enc] Arifureta Shokugyou de Sekai Saikyou 3rd Season",
        )

        self.assertEqual(context.media_type, "tv")
        self.assertEqual(context.season, 3)
        self.assertEqual(context.episode, 1)
        self.assertEqual(context.folder_title, "Arifureta Shokugyou de Sekai Saikyou")
        self.assertNotIn("3rd Season", context.folder_title)
        self.assertIn("Arifureta Shokugyou de Sekai Saikyou", context.title_variants)

    def test_ani_region_release_keeps_full_title_and_drops_guessit_fragment(self):
        scraper = self.recognition_module()
        filename = (
            "[ANi] 不要欺负我，长瀞同学 2nd Attack（仅限港澳台地区） - 04 "
            "[1080P][Bilibili][WEB-DL][AAC AVC][CHT CHS][MP4].mp4"
        )
        context = scraper.extract_recognition_context(
            filename, f"/动漫/{filename.rsplit('.', 1)[0]}"
        )
        queries = scraper.generate_query_variants(context)

        self.assertEqual(context.media_type, "tv")
        self.assertEqual(context.season, 2)
        self.assertEqual(context.episode, 4)
        self.assertEqual(context.normalized_title, "不要欺负我，长瀞同学 2nd Attack")
        self.assertEqual(context.folder_title, "不要欺负我，长瀞同学 2nd Attack")
        self.assertNotIn("Attack", queries)
        self.assertTrue(all("仅限港澳台地区" not in query for query in queries))
        self.assertTrue(all(not query.endswith(" 04") for query in queries))

    def test_ordinal_attack_season_requires_episode_and_title_tail_context(self):
        scraper = self.recognition_module()

        movie = scraper.extract_recognition_context("普通电影 2nd Attack (2024).mkv")
        attack_on = scraper.extract_recognition_context(
            "2nd Attack on Something - 04.mkv"
        )

        self.assertIsNone(movie.season)
        self.assertIsNone(movie.episode)
        self.assertIsNone(attack_on.season)
        self.assertEqual(attack_on.episode, 4)

    def test_standalone_bracket_release_revision_is_not_part_of_title(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "[Lilith-Raws] Kimisen [v2] - 02 [Baha][WEB-DL].mp4",
            "/动漫/Kimisen",
        )
        naked = scraper.extract_recognition_context("Project V2 - 01.mkv")

        self.assertEqual(context.normalized_title, "Kimisen")
        self.assertEqual(context.episode, 2)
        self.assertEqual(context.cleaned_components["release_versions"], ["v2"])
        self.assertIn("V2", naked.normalized_title)

    def test_clean_title_preserves_official_parenthesis_and_removes_short_region_tag(self):
        scraper = self.recognition_module().TMDBScraper()
        cleaned = scraper.clean_title(
            "[ANi] 被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生"
            "（仅限港澳台） - 01 [1080P][Bilibili][WEB-DL][AAC AVC][CHT CHS][MP4].mp4"
        )

        self.assertEqual(
            cleaned,
            "被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生",
        )

    def test_long_cjk_title_is_not_collapsed_to_short_suffix(self):
        scraper = self.recognition_module().TMDBScraper()
        filename = (
            "[ANi] 想當冒險者前往都市的女兒成為 S 級 - 01 "
            "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        )

        parsed = _parse_fields(scraper, filename)
        context = self.recognition_module().extract_recognition_context(filename, "")

        self.assertEqual(parsed["title"], "想當冒險者前往都市的女兒成為 S 級")
        self.assertEqual(context.normalized_title, "想當冒險者前往都市的女兒成為 S 級")
        self.assertEqual((parsed["season"], parsed["episode"]), (None, 1))

    def test_orion_origin_and_japanese_audio_tags_are_removed_from_title(self):
        scraper = self.recognition_module().TMDBScraper()

        self.assertEqual(
            scraper.clean_title(
                "[orion origin] Undead Unluck [01-24] [BDRip] [1080p] "
                "[H265 10bit_FLAC] [CHS＆JPN]"
            ),
            "Undead Unluck",
        )
        self.assertEqual(
            scraper.clean_title(
                "[orion origin] Shangri-La Frontier - Kusogee Hunter, "
                "Kamige ni Idoman to Su [01-25] [V2] [1080p] "
                "[H265 AAC] [CHS＆JPN]"
            ),
            "Shangri La Frontier Kusogee Hunter, Kamige ni Idoman to Su",
        )

    def test_japanese_word_in_real_title_is_preserved(self):
        scraper = self.recognition_module().TMDBScraper()

        self.assertEqual(
            scraper.clean_title("Japanese Story.2003.1080p.WEB-DL.H264.AAC.mkv"),
            "Japanese Story",
        )
        self.assertEqual(
            scraper.clean_title("[Japanese] Story.2003.1080p.WEB-DL.H264.AAC.mkv"),
            "Japanese Story",
        )
        self.assertEqual(
            scraper.clean_title("Story [CHS＆Japanese] [1080p].mkv"),
            "Story",
        )

    def test_trailing_bracket_complete_is_release_noise_but_title_word_is_preserved(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()

        context = scraper.extract_recognition_context(
            "[RUBaDUB] Please Twins! (Complete) (1080p) (Dual Audio)",
            "",
        )

        self.assertEqual(context.normalized_title, "Please Twins!")
        self.assertIn("Complete", context.cleaned_components["noise_tokens"])
        self.assertEqual(
            parser.clean_title("The Completed Works (2024).mkv"),
            "The Completed Works",
        )
        self.assertEqual(
            parser.clean_title("Completeish (2024).mkv"),
            "Completeish",
        )

    def test_complete_episode_range_is_removed_from_title_and_parent_context(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()
        directory = (
            "[Sakurato] Kage no Jitsuryokusha ni Naritakute! "
            "[01-20 FIN][AVC-8bit 1080P AAC][CHS]"
        )

        self.assertEqual(
            parser.clean_title(directory),
            "Kage no Jitsuryokusha ni Naritakute!",
        )
        context = scraper.extract_recognition_context(
            "[Sakurato] Kage no Jitsuryokusha ni Naritakute! - 01 "
            "[AVC-8bit 1080P AAC][CHS].mkv",
            f"/动漫/{directory}",
        )
        self.assertEqual(context.normalized_title, "Kage no Jitsuryokusha ni Naritakute!")
        self.assertEqual(context.episode, 1)

    def test_compact_season_episode_and_chinese_numeral_season_are_consistent(self):
        scraper = self.recognition_module()

        compact = scraper.extract_recognition_context(
            "Example.Show.2x03.1080p.WEB-DL.mkv", ""
        )
        parsed = _parse_fields(scraper.TMDBScraper(),
            "Example.Show.2x03.1080p.WEB-DL.mkv"
        )
        chinese = scraper.extract_recognition_context(
            "Example Show - 04.mkv", "/动漫/Example Show/第二季"
        )

        self.assertEqual((compact.season, compact.episode), (2, 3))
        self.assertEqual(compact.normalized_title, "Example Show")
        self.assertEqual((parsed["season"], parsed["episode"]), (2, 3))
        self.assertEqual(parsed["title"], "Example Show")
        self.assertEqual((chinese.season, chinese.episode), (2, 4))
        self.assertEqual(chinese.folder_title, "Example Show")

    def test_bracket_tv_season_marker_is_consistent_and_fail_closed(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()

        for marker in ("[TV-3]", "[ТВ-3]"):
            filename = f"Example Show - 01 {marker} [2026].mkv"
            with self.subTest(marker=marker):
                context = scraper.extract_recognition_context(filename, "")
                parsed = _parse_fields(parser, filename)
                position = scraper.parse_release_position(filename)
                self.assertEqual((context.season, context.episode), (3, 1))
                self.assertEqual(context.normalized_title, "Example Show")
                self.assertEqual((parsed["season"], parsed["episode"]), (3, 1))
                self.assertEqual(parsed["title"], "Example Show")
                self.assertEqual(
                    (position["season"], position["episode"]), (3, 1)
                )
                self.assertEqual(parser.clean_title(filename), "Example Show")

        technical = "Example Show - 01 [TV-1080] [2026].mkv"
        context = scraper.extract_recognition_context(technical, "")
        parsed = _parse_fields(parser, technical)
        position = scraper.parse_release_position(technical)
        self.assertIsNone(context.season)
        self.assertIsNone(parsed["season"])
        self.assertIsNone(position["season"])

    def test_bracket_tv_season_directory_supplies_season_without_becoming_title(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "01.mkv", "/动漫/Example Show (2026)/[TV-3]"
        )

        self.assertEqual(context.normalized_title, "Example Show")
        self.assertEqual(context.folder_title, "Example Show")
        self.assertEqual((context.season, context.episode), (3, 1))

    def test_compact_release_position_is_consistent_and_fails_closed(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()

        positives = (
            ("Demo.Show.1x02.1080p.mkv", 1, 2),
            ("Demo.Show.01x002.1080p.mkv", 1, 2),
            ("Demo.Show.[1x02].1080p.mkv", 1, 2),
            ("Demo.Show.4x03.1080p.mkv", 4, 3),
        )
        for filename, season, episode in positives:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "")
                parsed = _parse_fields(parser, filename)
                position = scraper.parse_release_position(filename)
                self.assertEqual((context.season, context.episode), (season, episode))
                self.assertEqual((parsed["season"], parsed["episode"]), (season, episode))
                self.assertEqual((position["season"], position["episode"]), (season, episode))
                self.assertEqual(context.normalized_title, "Demo Show")
                self.assertEqual(parsed["title"], "Demo Show")
                self.assertEqual(parser.clean_title(filename), "Demo Show")

        standard = "Demo.Show.S02E03.1x02.1080p.mkv"
        self.assertEqual(parser.parse_source_position(standard), (2, 3))
        parsed_standard = _parse_fields(parser, standard)
        self.assertEqual(
            (parsed_standard["season"], parsed_standard["episode"]),
            (2, 3),
        )

        negatives = (
            "Demo.Show.1920x1080.mkv",
            "Demo.Show.2024x01.mkv",
            "Demo.Show.001x02.mkv",
            "DemoTitle1x02.1080p.mkv",
            "Demo.Show.16x9.mkv",
        )
        for filename in negatives:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "")
                parsed = _parse_fields(parser, filename)
                position = scraper.parse_release_position(filename)
                self.assertIsNone(context.season)
                self.assertIsNone(context.episode)
                self.assertIsNone(parsed["season"])
                self.assertIsNone(parsed["episode"])
                self.assertIsNone(position["season"])
                self.assertIsNone(position["episode"])

    def test_parent_ordinal_attack_alias_supplies_season_for_bare_episode(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "04.mkv", "/动漫/不要欺负我，长瀞同学 2nd Attack"
        )

        self.assertEqual(context.normalized_title, "不要欺负我，长瀞同学 2nd Attack")
        self.assertEqual((context.season, context.episode), (2, 4))

    def test_english_word_ordinal_season_is_parsed_before_loose_episode_number(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "Example Show - 04.mkv", "/动漫/Example Show Second Season"
        )

        self.assertEqual(context.normalized_title, "Example Show")
        self.assertEqual((context.season, context.episode), (2, 4))

    def test_romaji_particle_before_ordinal_season_is_not_an_episode_token(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()
        filename = (
            "[Dynamis One] Hanazakari no Kimitachi e 2nd Season - 08 "
            "(CR 1920x1080 AVC AAC MKV) [1696F765].mkv"
        )

        context = scraper.extract_recognition_context(filename, "")
        parsed = _parse_fields(parser, filename)
        position = scraper.parse_release_position(filename)

        self.assertEqual(context.normalized_title, "Hanazakari no Kimitachi e")
        self.assertEqual((context.season, context.episode), (2, 8))
        self.assertEqual((parsed["season"], parsed["episode"]), (2, 8))
        self.assertEqual((position["season"], position["episode"]), (2, 8))

    def test_explicit_episode_tokens_still_accept_release_delimiters(self):
        scraper = self.recognition_module()
        for filename in (
            "Example Show E03.mkv",
            "Example Show EP03 [1080p].mkv",
            "Example Show Episode-03_WEB-DL.mkv",
            "Example Show S02E03v2.1080p.mkv",
        ):
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "")
                self.assertEqual(context.episode, 3)

    def test_specials_use_season_zero_in_recognition_context(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "Example Show OVA 03.mkv", "/动漫/Example Show/Season 01"
        )

        self.assertEqual(context.media_type, "tv")
        self.assertEqual((context.season, context.episode), (0, 3))
        self.assertEqual(context.normalized_title, "Example Show")

    def test_zero_episode_and_prologue_use_specials_in_recognition_context(self):
        scraper = self.recognition_module()

        for filename in (
            "Example Show E00 1080p.mkv",
            "Example Show S02E00 WEB-DL.mkv",
            "Example Show - Prologue.mkv",
        ):
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "/动漫/Example Show")
                self.assertEqual(context.media_type, "tv")
                self.assertEqual((context.season, context.episode), (0, 1))
                self.assertEqual(context.normalized_title, "Example Show")

    def test_numeric_movie_title_keeps_title_and_uses_release_year(self):
        scraper = self.recognition_module()

        context = scraper.extract_recognition_context(
            "1917.2019.1080p.BluRay.mkv", ""
        )

        self.assertEqual(context.normalized_title, "1917")
        self.assertEqual(context.filename_year, "2019")
        self.assertEqual(context.media_type, "movie")

    def test_real_release_names_drop_platform_codec_and_group_noise(self):
        scraper = self.recognition_module()

        diligence = scraper.extract_recognition_context(
            "Dilig.2024.1080p.CATCHPLAY.WEB-DL.H264.AAC-QuickIO.mkv", ""
        )
        furious = scraper.extract_recognition_context(
            "火遮眼.The.Furious.2026.2160p.iTunes.WEB-DL.DDP.5.1.Atmos.HDR10+.H.265-LINMENG@CHDBits.mkv",
            "",
        )

        self.assertEqual(diligence.filename_year, "2024")
        self.assertIn("Dilig", diligence.title_variants)
        self.assertEqual(furious.filename_year, "2026")
        self.assertIn("火遮眼", furious.title_variants)
        self.assertTrue(any("The Furious" in item for item in furious.title_variants))
        self.assertTrue(all("iTunes" not in item for item in furious.title_variants))

    def test_cjk_title_does_not_emit_numeric_or_short_latin_query(self):
        scraper = self.recognition_module()

        soldier = scraper.extract_recognition_context(
            "[ANi] 被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生"
            "（仅限港澳台地区） - 01 [1080P][Bilibili][WEB-DL][AAC AVC]"
            "[CHT CHS].mp4",
            "",
        )
        arifureta = scraper.extract_recognition_context(
            "平凡职业造就世界最强 第三季.EP16.1080p.CR.WEBRip.x264.AAC.CHS-LxyLab.mkv",
            "/动漫/平凡职业造就世界最强 第三季.2024.1080p.CR.WEBRip.x264.AAC.CHS-LxyLab",
        )

        soldier_queries = scraper.generate_query_variants(soldier)
        arifureta_queries = scraper.generate_query_variants(arifureta)
        self.assertNotIn("30", soldier_queries)
        self.assertNotIn("CR", arifureta_queries)
        self.assertIn("平凡职业造就世界最强", arifureta_queries)
        self.assertEqual((arifureta.season, arifureta.episode), (3, 16))

    def test_filename_season_marker_plus_bare_episode_keeps_episode(self):
        scraper = self.recognition_module()
        filename = (
            "[H-Enc] Arifureta Shokugyou de Sekai Saikyou "
            "Season 3 - 16.mkv"
        )

        context = scraper.extract_recognition_context(
            filename,
            "/动漫/[H-Enc] Arifureta Shokugyou de Sekai Saikyou "
            "Season 3 (BDRip 1080p HEVC FLAC)",
        )
        parsed = _parse_fields(scraper.TMDBScraper(), filename)

        self.assertEqual((context.season, context.episode), (3, 16))
        self.assertEqual((parsed["season"], parsed["episode"]), (3, 16))
        self.assertEqual(
            context.normalized_title,
            "Arifureta Shokugyou de Sekai Saikyou",
        )

    def test_implicit_sequel_season_is_parsed_only_with_episode_context(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()
        cases = [
            (
                "[Doomdos] - Grand Blue Dreaming 3 - 6 "
                "[1080p IQ WEB-DL].mkv",
                "",
                "Grand Blue Dreaming",
                3,
                6,
            ),
            (
                "[Erai-raws] Gaikotsu Kishi-sama, Tadaima Isekai e "
                "Odekakechuu II - 06 [1080p].mkv",
                "",
                "Gaikotsu Kishi sama, Tadaima Isekai e Odekakechuu",
                2,
                6,
            ),
            (
                "Grand Example Animation Ⅱ - 05 [1080p][WEB-DL].mkv",
                "",
                "Grand Example Animation",
                2,
                5,
            ),
            (
                "[SFSub] Otonari no Tenshi-sama 2 - 01 [1080p].mkv",
                "/动漫/[SFSub] Otonari no Tenshi-sama 2 Vol1",
                "Otonari no Tenshi sama",
                2,
                1,
            ),
        ]

        for filename, parent_path, title, season, episode in cases:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, parent_path)
                parsed = _parse_fields(parser, filename)
                self.assertEqual(context.normalized_title, title)
                self.assertEqual((context.season, context.episode), (season, episode))
                self.assertEqual(parsed["title"], title)
                self.assertEqual((parsed["season"], parsed["episode"]), (season, episode))

    def test_unicode_roman_season_before_cjk_subtitle_is_shared_position_evidence(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()
        filename = (
            "[ANi] Clevatess Ⅱ－魔獸之王與虛假的勇者傳承－ - 08 "
            "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        )

        context = scraper.extract_recognition_context(
            filename, "/media/downloads/下载"
        )
        parsed = _parse_fields(parser, filename)

        self.assertEqual(context.normalized_title, "Clevatess")
        self.assertEqual((context.season, context.episode), (2, 8))
        self.assertIn(
            "Clevatess Ⅱ－魔獸之王與虛假的勇者傳承－",
            context.title_variants,
        )
        self.assertEqual((parsed["season"], parsed["episode"]), (2, 8))
        self.assertEqual(
            scraper.parse_release_position(filename),
            {"season": 2, "episode": 8, "episode_end": None},
        )

        identity = scraper.extract_recognition_context(
            "Lupin Ⅲ - 08 [1080P][WEB-DL].mkv", ""
        )
        self.assertEqual(identity.normalized_title, "Lupin Ⅲ")
        self.assertEqual((identity.season, identity.episode), (None, 8))
        subtitled_identity = scraper.extract_recognition_context(
            "Lupin Ⅲ－峰不二子的谎言－ - 08 [1080P][WEB-DL].mkv", ""
        )
        self.assertEqual(subtitled_identity.season, None)
        self.assertEqual(subtitled_identity.episode, 8)

    def test_bracket_episode_keeps_title_identity_x_but_parses_roman_second_season(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()

        identity = scraper.extract_recognition_context(
            "假面骑士 X [01] [1080p].mkv", ""
        )
        parsed_identity = _parse_fields(parser, "假面骑士 X [01] [1080p].mkv")
        self.assertEqual(identity.normalized_title, "假面骑士 X")
        self.assertEqual((identity.season, identity.episode), (None, 1))
        self.assertEqual(parsed_identity["title"], "假面骑士 X")
        self.assertEqual(
            (parsed_identity["season"], parsed_identity["episode"]),
            (None, 1),
        )

        sequel = scraper.extract_recognition_context(
            "Grand Example Animation II [05] [1080p].mkv", ""
        )
        parsed_sequel = _parse_fields(parser,
            "Grand Example Animation II [05] [1080p].mkv"
        )
        self.assertEqual(sequel.normalized_title, "Grand Example Animation")
        self.assertEqual((sequel.season, sequel.episode), (2, 5))
        self.assertEqual(parsed_sequel["title"], "Grand Example Animation")
        self.assertEqual(
            (parsed_sequel["season"], parsed_sequel["episode"]),
            (2, 5),
        )

    def test_enclosed_high_number_remains_ambiguous_without_tmdb_proof(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()
        filename = "[Yami Shibai 17][06][x264 1080p][CHS].mp4"

        context = scraper.extract_recognition_context(filename, "")
        parsed = _parse_fields(parser, filename)

        self.assertEqual(context.normalized_title, "Yami Shibai 17")
        self.assertEqual((context.season, context.episode), (None, 6))
        self.assertEqual(parsed["title"], "Yami Shibai 17")
        self.assertEqual((parsed["season"], parsed["episode"]), (None, 6))
        self.assertEqual(
            scraper.parse_release_position(filename),
            {"season": None, "episode": 6, "episode_end": None},
        )

    def test_implicit_sequel_season_keeps_numeric_and_part_titles_safe(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()
        cases = (
            ("86 - 01.mkv", "86"),
            ("3-gatsu no Lion - 01.mkv", "3 gatsu no Lion"),
            ("Example Show Part 2 - 01.mkv", "Example Show Part 2"),
            ("Final Fantasy VII - 01.mkv", "Final Fantasy VII"),
            ("The Evil Dead II - 01.mkv", "The Evil Dead II"),
            ("The Lord of the Rings II - 01.mkv", "The Lord of the Rings II"),
            ("The Fantastic Four 2 - 01.mkv", "The Fantastic Four 2"),
        )

        for filename, title in cases:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "")
                parsed = _parse_fields(parser, filename)
                self.assertEqual(context.normalized_title, title)
                self.assertEqual((context.season, context.episode), (None, 1))
                self.assertEqual((parsed["season"], parsed["episode"]), (None, 1))

        self.assertTrue(
            scraper.has_unresolved_season_hint("Example Show Part 2 - 01.mkv")
        )
        self.assertFalse(scraper.has_unresolved_season_hint("86 - 01.mkv"))
        self.assertFalse(
            scraper.has_unresolved_season_hint(
                "Normal Show - 01.mkv", "/downloads/Part 2/Normal Show"
            )
        )

    def test_final_title_word_survives_completion_tag_cleanup(self):
        scraper = self.recognition_module()
        parser = scraper.TMDBScraper()
        for filename in (
            "Final Fantasy VII [FINAL].mkv",
            "Final Fantasy VII FINAL.mkv",
        ):
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename)
                self.assertEqual(context.normalized_title, "Final Fantasy VII")
                self.assertEqual(parser.clean_title(filename), "Final Fantasy VII")
                self.assertTrue(
                    any(
                        str(token).upper() == "FINAL"
                        for token in context.cleaned_components["noise_tokens"]
                    )
                )

    def test_structured_episode_position_projects_title_before_episode_subtitle(self):
        scraper = self.recognition_module()
        context = scraper.extract_recognition_context(
            "You.and.I.Are.Polar.Opposites.S02E06.Threshold.of.Spring."
            "1080p.CR.JPN.AAC2.0.MSubs.mkv",
            "",
        )

        self.assertEqual(context.normalized_title, "You and I Are Polar Opposites")
        self.assertEqual((context.season, context.episode), (2, 6))
        self.assertEqual(
            scraper.generate_query_variants(context)[0],
            "You and I Are Polar Opposites",
        )

    def test_bracket_release_metadata_is_removed_without_global_title_loss(self):
        scraper = self.recognition_module()
        context = scraper.extract_recognition_context(
            "[Erai-raws] Sekai Saikyou no Kouei - 06 "
            "[720p CR WEB-DL AVC AAC][MultiSub].mkv",
            "",
        )

        self.assertEqual(context.normalized_title, "Sekai Saikyou no Kouei")
        self.assertEqual((context.season, context.episode), (None, 6))
        self.assertNotIn("CR", scraper.generate_query_variants(context))
        self.assertNotIn("MultiSub", scraper.generate_query_variants(context))

    def test_pure_technical_dual_audio_bracket_is_removed(self):
        scraper = self.recognition_module()
        context = scraper.extract_recognition_context(
            "[RUBaDUB][1080p] Please Twins! - 01 "
            "[BD x265 10bit Dual Audio AC3][207CD92E].mkv",
            "",
        )

        self.assertEqual(context.normalized_title, "Please Twins!")
        self.assertEqual((context.season, context.episode), (None, 1))
        self.assertIn(
            "BD x265 10bit Dual Audio AC3",
            context.cleaned_components["noise_tokens"],
        )

    def test_mixed_title_bracket_is_not_erased_as_technical_noise(self):
        scraper = self.recognition_module()

        self.assertFalse(scraper._is_bracket_noise("Dual Hearts 1080p"))
        context = scraper.extract_recognition_context(
            "[Dual Hearts 1080p] - 01.mkv",
            "",
        )

        self.assertEqual(context.normalized_title, "Dual Hearts")
        self.assertEqual(context.episode, 1)

    def test_release_folder_uses_structural_episode_and_clean_title_candidate(self):
        scraper = self.recognition_module()
        context = scraper.extract_recognition_context(
            "[Doomdos] - Grand Blue Dreaming 3 - 6 "
            "[1080p IQ WEB-DL].mkv",
            "/[Doomdos] - Grand Blue Dreaming 3 - 6 "
            "[1080p IQ WEB-DL]",
        )

        self.assertEqual(context.normalized_title, "Grand Blue Dreaming")
        self.assertEqual(context.folder_title, "Grand Blue Dreaming")
        self.assertEqual((context.season, context.episode), (3, 6))

    def test_parent_media_filename_does_not_pollute_folder_title_with_crc(self):
        scraper = self.recognition_module()
        context = scraper.extract_recognition_context(
            "[SubsPlease] Grand Blue S3 - 06 (480p) [D73B045B].mkv",
            "/[SubsPlease] Grand Blue S3 - 06 (480p) [D73B045B].mkv",
        )

        self.assertEqual(context.normalized_title, "Grand Blue")
        self.assertEqual(context.folder_title, "Grand Blue")
        self.assertEqual((context.season, context.episode), (3, 6))

    def test_release_root_for_extras_drops_volume_but_keeps_sequel_number(self):
        scraper = self.recognition_module()
        title, year, media_type, season = scraper._folder_context(
            "[SFSub] Otonari no Tenshi-sama 2 Vol1 "
            "[BDRip 1080p x264 10bit FLAC]/Extra",
            episode_context=False,
        )

        self.assertEqual(title, "Otonari no Tenshi sama 2")
        self.assertEqual(year, "")
        self.assertEqual(media_type, "")
        self.assertIsNone(season)

    def test_multilingual_second_season_folder_keeps_sequel_identity_anchor(self):
        scraper = self.recognition_module()
        context = scraper.extract_recognition_context(
            "[SFSub] Otonari no Tenshi-sama 2 - 01 [1080p].mkv",
            "/动漫/[SFSub] 關於我在無意間被隔壁的天使變成廢柴這件事 2 / "
            "关于我在无意间被隔壁的天使变成废柴这件事 2 / "
            "Otonari no Tenshi-sama ni Itsunomanika Dame Ningen ni "
            "Sareteita Ken 2 Vol.1 [1080p]",
        )

        self.assertEqual(context.normalized_title, "Otonari no Tenshi sama")
        self.assertEqual(
            context.folder_title,
            "Otonari no Tenshi sama ni Itsunomanika Dame Ningen ni "
            "Sareteita Ken 2",
        )
        self.assertEqual((context.season, context.episode), (2, 1))

    def test_bracket_wrapped_title_and_episode_survive_release_cleanup(self):
        scraper = self.recognition_module()
        cases = [
            (
                "[Arifureta Shokugyou de Sekai Saikyou S2][01][BIG5][1080P].mp4",
                "Arifureta Shokugyou de Sekai Saikyou",
                2,
                1,
            ),
            (
                "[KTXP][Isekai_Maou_to_Shoukan_Shoujo_no_Dorei_Majutsu]"
                "[01][GB][1080p][BDrip][HEVC].mkv",
                "Isekai Maou to Shoukan Shoujo no Dorei Majutsu",
                None,
                1,
            ),
            (
                "[Nekomoe kissaten][Kimisen][01][1080p][CHT].mp4",
                "Kimisen",
                None,
                1,
            ),
            (
                "[Nekomoe kissaten&LoliHouse] Maou Gakuin no Futekigousha "
                "- 13 [WebRip 1080P HEVC-10bit AAC ASSx2].mkv",
                "Maou Gakuin no Futekigousha",
                None,
                13,
            ),
        ]

        parser = scraper.TMDBScraper()
        for filename, expected_title, expected_season, expected_episode in cases:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "")
                parsed = _parse_fields(parser, filename)
                queries = scraper.generate_query_variants(context)
                self.assertEqual(context.normalized_title, expected_title)
                self.assertEqual(
                    (context.season, context.episode),
                    (expected_season, expected_episode),
                )
                self.assertEqual(parsed["title"], expected_title)
                self.assertEqual(
                    (parsed["season"], parsed["episode"]),
                    (expected_season, expected_episode),
                )
                self.assertEqual(queries[0], expected_title)
                self.assertNotIn("KTXP", queries)
                self.assertNotIn("Nekomoe kissaten", queries)

    def test_bracketed_release_year_is_not_treated_as_season(self):
        scraper = self.recognition_module()
        filename = (
            "[GM-Team][国漫][东大高武学院][Oriental Martial Academy]"
            "[2026][04][GB][4K HEVC 10Bit].mp4"
        )

        context = scraper.extract_recognition_context(filename, "")
        parsed = _parse_fields(scraper.TMDBScraper(), filename)

        self.assertEqual(context.filename_year, "2026")
        self.assertEqual((context.season, context.episode), (None, 4))
        self.assertEqual(parsed["year"], "2026")
        self.assertEqual((parsed["season"], parsed["episode"]), (None, 4))

    def test_long_running_episode_numbers_and_revisions_are_parsed_safely(self):
        scraper = self.recognition_module()
        cases = [
            (
                "[SubsMix] Bocchi the Rock! - S01E01v4 "
                "(BD 1080p HEVC Opus 2.0).mkv",
                "Bocchi the Rock!",
                1,
                1,
            ),
            (
                "[NanakoRaws] Doraemon - S01E01 [1080p].mkv",
                "Doraemon",
                1,
                1,
            ),
            (
                "[Shridhuu] Renegade Immortal - S01E01 [1080p].mkv",
                "Renegade Immortal",
                1,
                1,
            ),
            (
                "One.Piece.S01E1173.1080p.CR.WEBRip.x264.AAC.mkv",
                "One Piece",
                1,
                1173,
            ),
            (
                "[Kaerizaki-Fansub]_One_Piece_1173_"
                "[VERSION_LIGHT][VOSTFR][FHD_1920x1080].mp4",
                "One Piece",
                None,
                1173,
            ),
            ("One Piece - 1173 [1080p].mkv", "One Piece", None, 1173),
            ("One Piece [1173][1080p].mkv", "One Piece", None, 1173),
        ]

        for filename, expected_title, expected_season, expected_episode in cases:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "")
                parsed = _parse_fields(scraper.TMDBScraper(), filename)
                position = scraper.parse_release_position(filename)

                self.assertEqual(context.normalized_title, expected_title)
                self.assertEqual(
                    (context.season, context.episode),
                    (expected_season, expected_episode),
                )
                self.assertEqual(parsed["title"], expected_title)
                self.assertEqual(
                    (parsed["season"], parsed["episode"]),
                    (expected_season, expected_episode),
                )
                self.assertEqual(
                    (position["season"], position["episode"]),
                    (expected_season, expected_episode),
                )

        expected_groups = {
            cases[0][0]: "SubsMix",
            cases[1][0]: "NanakoRaws",
        }
        for filename, expected_group in expected_groups.items():
            context = scraper.extract_recognition_context(filename, "")
            self.assertEqual(
                context.cleaned_components.get("candidate_release_groups"),
                [expected_group],
            )
            self.assertEqual(
                scraper.generate_query_variants(context)[0],
                context.filename_title,
            )

        shridhuu = scraper.extract_recognition_context(cases[2][0], "")
        self.assertEqual(
            shridhuu.cleaned_components.get("candidate_release_groups"), []
        )
        self.assertIn(
            "Shridhuu",
            " ".join(shridhuu.cleaned_components.get("release_prefixes", [])),
        )

        bocchi = scraper.extract_recognition_context(cases[0][0], "")
        self.assertIn("Bocchi the Rock!", scraper.generate_query_variants(bocchi))

    def test_unlabeled_four_digit_years_and_resolutions_are_not_episodes(self):
        scraper = self.recognition_module()
        cases = [
            ("Show - 2026.mkv", "2026"),
            ("Demo.Movie.2026.1080p.mkv", "2026"),
            ("Demo Show - 1080 [HEVC].mkv", ""),
            ("Demo Show [2160][HEVC].mkv", ""),
        ]

        for filename, expected_year in cases:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "")
                position = scraper.parse_release_position(filename)

                self.assertIsNone(context.episode)
                self.assertIsNone(position["episode"])
                self.assertEqual(context.filename_year, expected_year)

        explicit_cases = [
            ("Demo Show - E1080.mkv", None, 1080),
            ("Demo Show - S01E01.mkv", 1, 1),
        ]
        for filename, expected_season, expected_episode in explicit_cases:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "")
                position = scraper.parse_release_position(filename)

                self.assertEqual(context.season, expected_season)
                self.assertEqual(context.episode, expected_episode)
                self.assertEqual(position["season"], expected_season)
                self.assertEqual(position["episode"], expected_episode)

    def test_query_variants_include_dual_titles_and_folder_title_without_duplicates(self):
        scraper = self.recognition_module()
        context = scraper.extract_recognition_context(
            "繁花.Shanghai.Blank.2023.S01E01.mkv",
            "/剧集/繁花 Shanghai Blossoms (2023)/Season 01",
        )

        queries = scraper.generate_query_variants(context)

        self.assertEqual(queries[0], "繁花 Shanghai Blank")
        self.assertIn("繁花", queries)
        self.assertIn("Shanghai Blank", queries)
        self.assertIn("繁花 Shanghai Blossoms", queries)
        self.assertEqual(len(queries), len(dict.fromkeys(queries)))


class CandidateScoringTests(RecognitionContractMixin, unittest.TestCase):
    def setUp(self):
        scraper = self.recognition_module()
        self.scraper_module = scraper
        self.context = scraper.extract_recognition_context(
            "幕府将军.Shogun.2024.S01E01.mkv",
            "/剧集/幕府将军 Shogun (2024)/Season 01",
        )

    def test_original_title_and_alias_scores_are_structured_numeric_fields(self):
        breakdown = self.scraper_module.score_candidate(self.context, {
            "id": 126308,
            "name": "幕府将军",
            "original_name": "Shōgun",
            "aliases": ["Shogun", "将军"],
            "first_air_date": "2024-02-27",
            "media_type": "tv",
        })

        for field in (
            "title_score", "original_title_score", "alias_score", "year_score",
            "year_penalty", "media_type_score", "constraint_penalty", "final_score",
        ):
            self.assertIsInstance(getattr(breakdown, field), float, field)
        self.assertGreater(breakdown.original_title_score, 0.8)
        self.assertGreater(breakdown.alias_score, 0.8)
        self.assertEqual(breakdown.rejected_constraints, [])
        self.assertIn(breakdown.matched_title, {"幕府将军", "Shōgun", "Shogun", "将军"})

    def test_japanese_kana_remains_distinguishing_signal(self):
        scraper = self.scraper_module
        context = scraper.extract_recognition_context(
            "僕のヒーローアカデミア.S07E01.mkv", ""
        )
        wrong = scraper.score_candidate(context, {
            "id": 64196,
            "name": "僕だけがいない街",
            "original_name": "僕だけがいない街",
            "first_air_date": "2016-01-08",
            "media_type": "tv",
        })

        self.assertLess(wrong.final_score, 0.6)

    def test_short_identity_suffix_is_not_dropped_by_base_title_candidate(self):
        scraper = self.scraper_module
        cases = (
            ("凸变英雄X.S01E01.mkv", "凸变英雄"),
            ("Kamen Rider X.S01E01.mkv", "Kamen Rider"),
            ("Title II.S01E01.mkv", "Title"),
        )
        for filename, candidate_title in cases:
            with self.subTest(filename=filename):
                context = scraper.extract_recognition_context(filename, "")
                breakdown = scraper.score_candidate(context, {
                    "id": 1,
                    "name": candidate_title,
                    "first_air_date": "2024-01-01",
                    "media_type": "tv",
                })
                self.assertIn(
                    "protected_title_suffix_missing",
                    breakdown.rejected_constraints,
                )

        exact_context = scraper.extract_recognition_context(
            "凸变英雄X.S01E01.mkv", ""
        )
        exact = scraper.score_candidate(exact_context, {
            "id": 2,
            "name": "凸变英雄X",
            "first_air_date": "2024-01-01",
            "media_type": "tv",
        })
        self.assertNotIn(
            "protected_title_suffix_missing", exact.rejected_constraints
        )

    def test_tmdb_detail_force_refresh_bypasses_success_and_failure_cache(self):
        scraper = self.scraper_module
        client = Mock()
        client.api_key = "test-key"
        client.base_url = "https://tmdb.test/3"
        client.session = None
        client.detail.side_effect = [
            {"id": 9001, "seasons": [{"season_number": 1, "episode_count": 12}]},
            {"id": 9001, "seasons": [{"season_number": 1, "episode_count": 13}]},
        ]
        matcher = scraper.TMDBScraper(client=client)

        first = matcher.get_detail("9001", "tv")
        cached = matcher.get_detail("9001", "tv")
        refreshed = matcher.get_detail("9001", "tv", force_refresh=True)

        self.assertEqual(first["seasons"][0]["episode_count"], 12)
        self.assertEqual(cached["seasons"][0]["episode_count"], 12)
        self.assertEqual(refreshed["seasons"][0]["episode_count"], 13)
        self.assertEqual(client.detail.call_count, 2)

    def test_year_mismatch_applies_a_numeric_penalty(self):
        exact = self.scraper_module.score_candidate(self.context, {
            "id": 1, "name": "幕府将军", "first_air_date": "2024-01-01", "media_type": "tv",
        })
        stale = self.scraper_module.score_candidate(self.context, {
            "id": 2, "name": "幕府将军", "first_air_date": "1980-01-01", "media_type": "tv",
        })

        self.assertEqual(exact.year_penalty, 0.0)
        self.assertLess(stale.year_penalty, 0.0)
        self.assertGreater(exact.final_score, stale.final_score)
        self.assertFalse(self.scraper_module.decide_threshold(stale.final_score, 0.6)["passed"])

    def test_target_season_air_year_counts_as_exact_year_only_with_bound_evidence(self):
        scraper = self.scraper_module
        context = scraper.extract_recognition_context(
            "Example.Show.2024.S03E02.1080p.mkv", ""
        )
        detail = {
            "id": 9001,
            "seasons": [{
                "season_number": 3,
                "episode_count": 12,
                "air_date": "2024-01-05",
            }],
        }
        mapping = scraper.infer_episode_mapping(
            source_season=3,
            source_episode=2,
            parent_path="",
            detail=detail,
            mode="auto",
        )
        evidence = scraper._build_target_season_year_evidence(
            detail=detail,
            tmdb_id="9001",
            context=context,
            expected_year="2024",
            mapping=mapping,
        )
        candidate = {
            "id": 9001,
            "name": "Example Show",
            "first_air_date": "2020-01-01",
            "media_type": "tv",
            "_verified_target_season_year": evidence,
        }

        breakdown = scraper.score_candidate(context, candidate)

        self.assertIsNotNone(evidence)
        self.assertEqual(breakdown.year_score, 1.0)
        self.assertEqual(breakdown.year_penalty, 0.0)
        self.assertGreaterEqual(breakdown.final_score, 0.9)

    def test_movie_tv_constraint_rejects_wrong_media_type(self):
        breakdown = self.scraper_module.score_candidate(self.context, {
            "id": 3, "title": "幕府将军", "release_date": "2024-01-01", "media_type": "movie",
        })

        self.assertEqual(breakdown.media_type_score, 0.0)
        self.assertLess(breakdown.constraint_penalty, 0.0)
        self.assertEqual(breakdown.final_score, 0.0)
        self.assertIn("media_type_mismatch", breakdown.rejected_constraints)


    def test_weak_cjk_side_tokens_cannot_make_unrelated_candidate_pass(self):
        scraper = self.recognition_module()
        soldier = scraper.extract_recognition_context(
            "[ANi] 被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生 - 01.mp4",
            "",
        )
        arifureta = scraper.extract_recognition_context(
            "平凡职业造就世界最强 第三季.EP16.1080p.CR.WEBRip.mkv",
            "/动漫/平凡职业造就世界最强 第三季 (2024)",
        )

        thirty_days = scraper.score_candidate(soldier, {
            "id": 1543, "name": "30天", "original_name": "30 Days",
            "first_air_date": "2005-01-01", "media_type": "tv",
        })
        cross = scraper.score_candidate(arifureta, {
            "id": 213306, "name": "神探追缉令", "original_name": "Cross",
            "first_air_date": "2024-01-01", "media_type": "tv",
        })

        self.assertLess(thirty_days.final_score, 0.6)
        self.assertLess(cross.final_score, 0.6)


class _DeterministicClient:
    api_key = "test-key"
    base_url = "https://tmdb.test/3"
    config_error = ""
    session = None

    def __init__(self, candidates, details=None):
        self.candidates = candidates
        self.details = details or {}
        self.search_calls = []
        self.detail_calls = []

    def search(self, title, year, media_type):
        self.search_calls.append((title, year, media_type))
        return list(self.candidates)

    def detail(self, tmdb_id, media_type):
        self.detail_calls.append((str(tmdb_id), media_type))
        return dict(self.details.get(str(tmdb_id), {}))

    def detail_with_alternative_titles(self, tmdb_id, media_type):
        return self.detail(tmdb_id, media_type)




class _QueryDeterministicClient(_DeterministicClient):
    def __init__(self, candidates_by_query, details=None):
        super().__init__([], details)
        self.candidates_by_query = candidates_by_query

    def search(self, title, year, media_type):
        self.search_calls.append((title, year, media_type))
        return list(self.candidates_by_query.get(str(title), []))


class DeterministicPipelineTests(RecognitionContractMixin, unittest.TestCase):
    def test_explicit_donghua_marker_selects_animation_homonym_for_all_samples(self):
        scraper_module = self.recognition_module()
        shared_alias = "Ever Night"
        client = _DeterministicClient(
            [
                {
                    "id": 83612,
                    "name": "将夜",
                    "original_name": "将夜",
                    "first_air_date": "2018-10-31",
                    "media_type": "tv",
                    "genre_ids": [10759, 18, 10765],
                },
                {
                    "id": 282136,
                    "name": "将夜",
                    "original_name": "将夜",
                    "first_air_date": "2026-04-23",
                    "media_type": "tv",
                    "genre_ids": [16, 10759, 18],
                },
            ],
            {
                "83612": {
                    "id": 83612,
                    "name": "将夜",
                    "original_name": "将夜",
                    "first_air_date": "2018-10-31",
                    "genres": [{"id": 18, "name": "剧情"}],
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [{"season_number": 1, "episode_count": 60}],
                },
                "282136": {
                    "id": 282136,
                    "name": "将夜",
                    "original_name": "将夜",
                    "first_air_date": "2026-04-23",
                    "genres": [{"id": 16, "name": "动画"}],
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [{"season_number": 1, "episode_count": 20}],
                },
            },
        )
        tmdb = scraper_module.TMDBScraper(client=client)
        files = (
            (6, "B2088D0F"),
            (7, "4FD19EAF"),
            (18, "314B281E"),
            (19, "6CE52CDF"),
        )

        for episode, checksum in files:
            filename = (
                f"[Dynamis One] Ever Night - {episode:02d} "
                f"(B-Global Donghua 1920x832 HEVC AAC MKV) [{checksum}].mkv"
            )
            with self.subTest(filename=filename):
                result = tmdb.deterministic_recognize(filename, "1")

                self.assertEqual(result.status, "matched")
                self.assertFalse(result.need_confirm)
                self.assertEqual(result.tmdb_id, "282136")
                self.assertEqual({item.tmdb_id for item in result.candidates}, {"282136"})
                evidence = result.metadata["content_kind_evidence"]
                self.assertTrue(evidence["verified"])
                self.assertEqual(evidence["required_genre_id"], 16)
                self.assertEqual(evidence["filtered_non_animation_candidates"], 1)

    def test_explicit_donghua_marker_fails_closed_without_animation_candidate(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 83612,
            "name": "Ever Night",
            "original_name": "Ever Night",
            "first_air_date": "2018-10-31",
            "media_type": "tv",
            "genre_ids": [10759, 18, 10765],
        }])

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            "[Dynamis One] Ever Night - 06 "
            "(B-Global Donghua 1920x832 HEVC AAC MKV) [B2088D0F].mkv",
            "1",
        )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertEqual(
            result.threshold_decision["reason"], "animation_evidence_mismatch",
        )
        self.assertIn("Donghua 动画标记", result.error)
        self.assertFalse(result.metadata["content_kind_evidence"]["verified"])

    def test_virgin_punk_matches_tmdb_translation_alias(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 1360829,
            "title": "处女朋克：发条女孩",
            "original_title": "ヴァージン・パンク Clockwork Girl",
            "release_date": "2025-06-27",
            "media_type": "movie",
        }], {
            "1360829": {
                "translations": {"translations": [{
                    "iso_639_1": "en",
                    "data": {"title": "Virgin Punk: Clockwork Girl"},
                }]},
            },
        })
        tmdb = scraper_module.TMDBScraper(client=client)

        result = tmdb.deterministic_recognize(
            "[Studio GreenTea] Virgin Punk Clockwork Girl "
            "[Movie v2][BDRip][HEVC-10bit 1080p AAC][JPTC].mkv"
        )

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.tmdb_id, "1360829")
        self.assertGreaterEqual(result.confidence, 0.9)
        self.assertIn(
            ("Virgin Punk Clockwork Girl", "", "movie"),
            client.search_calls,
        )
        self.assertEqual(client.detail_calls, [("1360829", "movie")])

    def test_compound_release_groups_do_not_block_exact_romaji_alias(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 97617,
            "name": "魔王学院的不适任者",
            "original_name": "魔王学院の不適合者",
            "aliases": ["Maou Gakuin no Futekigousha"],
            "first_air_date": "2020-07-04",
            "media_type": "tv",
        }], {
            "97617": {"seasons": [{"season_number": 1, "episode_count": 13}]},
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        filename = (
            "[Nekomoe kissaten&LoliHouse] Maou Gakuin no Futekigousha "
            "- 13 [WebRip 1080P HEVC-10bit AAC ASSx2].mkv"
        )

        result = tmdb.deterministic_recognize(
            filename,
            "/动漫/[Nekomoe kissaten&LoliHouse] Maou Gakuin no Futekigousha "
            "[WebRip 1080P HEVC-10bit AAC ASSx2]",
        )

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.tmdb_id, "97617")
        self.assertEqual((result.context.season, result.context.episode), (None, 13))
        self.assertEqual(result.context.normalized_title, "Maou Gakuin no Futekigousha")
        self.assertNotIn("distinctive_title_tokens_missing", result.rejected_constraints)

    def test_search_error_redacts_api_credentials_from_recognition_diagnostics(self):
        scraper_module = self.recognition_module()
        secret = "task16-super-secret"

        class FailingClient(_DeterministicClient):
            def search(self, title, year, media_type):
                raise RuntimeError(
                    "request failed "
                    f"https://tmdb.invalid/search?api_key={secret}&query={title}"
                )

        tmdb = scraper_module.TMDBScraper(client=FailingClient([]))

        result = tmdb.deterministic_recognize("Safe.Movie.2024.mkv")

        self.assertEqual(result.status, "request_error")
        self.assertNotIn(secret, result.error)
        self.assertIn("api_key=********", result.error)

    def test_exact_short_title_requires_manual_confirmation(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 999,
            "name": "Q",
            "first_air_date": "2024-01-01",
            "media_type": "tv",
        }])
        tmdb = scraper_module.TMDBScraper(client=client)

        result = tmdb.deterministic_recognize("[NC-Raws] Q - 01 [1080p].mkv")

        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertEqual(result.threshold_decision["reason"], "low_information_title")
        self.assertIn("信息量不足", result.error)

    def test_low_information_parent_cannot_override_informative_filename(self):
        scraper_module = self.recognition_module()
        wrong_candidate = {
            "id": 294418,
            "name": "1",
            "first_air_date": "2006-01-01",
            "media_type": "tv",
        }
        filenames = (
            (
                "[ANi] Animatica「北斗之拳 拳王軍雜兵們的輓歌」 - 19 "
                "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
            ),
            (
                "[ANi] 麵包超人電影版：小水滴的英雄！ [電影] [中文配音] - 01 "
                "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
            ),
        )

        for filename in filenames:
            with self.subTest(filename=filename):
                context = scraper_module.extract_recognition_context(filename, "1")
                self.assertEqual(context.folder_title, "1")
                self.assertNotIn(
                    "1", scraper_module.generate_query_variants(context)
                )

                breakdown = scraper_module.score_candidate(
                    context, wrong_candidate
                )
                self.assertEqual(
                    breakdown.rejected_constraints,
                    ["low_information_variant_match"],
                )
                self.assertNotEqual(breakdown.matched_query, "1")

                result = scraper_module.TMDBScraper(
                    client=_DeterministicClient([wrong_candidate])
                ).deterministic_recognize(filename, "1")

                self.assertEqual(result.status, "low_confidence")
                self.assertTrue(result.need_confirm)
                self.assertEqual(
                    result.threshold_decision["reason"],
                    "low_information_variant_match",
                )
                self.assertIn("低信息目录", result.error)
                self.assertEqual(
                    result.metadata["recognition_evidence"]["folder_title"],
                    "1",
                )

    def test_low_information_parent_keeps_correct_filename_matches(self):
        scraper_module = self.recognition_module()
        samples = (
            (
                "9001",
                (
                    "[ANi] Animatica「北斗之拳 拳王軍雜兵們的輓歌」 - 19 "
                    "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
                ),
                "Animatica「北斗之拳 拳王軍雜兵們的輓歌」",
                19,
            ),
            (
                "9002",
                (
                    "[ANi] 麵包超人電影版：小水滴的英雄！ [電影] [中文配音] - 01 "
                    "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
                ),
                "麵包超人電影版：小水滴的英雄！ 中文配音",
                1,
            ),
        )

        for tmdb_id, filename, title, episode in samples:
            with self.subTest(filename=filename):
                candidate = {
                    "id": int(tmdb_id),
                    "name": title,
                    "first_air_date": "2026-01-01",
                    "media_type": "tv",
                }
                client = _DeterministicClient([candidate], {
                    tmdb_id: {
                        "seasons": [
                            {"season_number": 1, "episode_count": 24}
                        ],
                    },
                })
                result = scraper_module.TMDBScraper(
                    client=client
                ).deterministic_recognize(filename, "1")

                self.assertEqual(result.status, "matched")
                self.assertFalse(result.need_confirm)
                self.assertEqual(result.tmdb_id, tmdb_id)
                self.assertEqual(result.context.episode, episode)
                self.assertNotIn("1", result.query_variants)
                self.assertNotIn(
                    "low_information_variant_match",
                    result.rejected_constraints,
                )

    def test_meaningful_folder_still_identifies_generic_episode_filename(self):
        scraper_module = self.recognition_module()
        candidate = {
            "id": 9003,
            "name": "Example Show",
            "first_air_date": "2024-01-01",
            "media_type": "tv",
        }
        client = _DeterministicClient([candidate], {
            "9003": {"seasons": [{"season_number": 1, "episode_count": 12}]},
        })

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            "01.mkv", "/剧集/Example Show"
        )

        self.assertEqual(result.status, "matched")
        self.assertFalse(result.need_confirm)
        self.assertIn("Example Show", result.query_variants)
        self.assertNotIn(
            "low_information_variant_match", result.rejected_constraints
        )

    def test_four_digit_movie_title_can_still_auto_match(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 530915,
            "title": "1917",
            "release_date": "2019-12-25",
            "media_type": "movie",
        }])
        tmdb = scraper_module.TMDBScraper(client=client)

        result = tmdb.deterministic_recognize("1917.2019.1080p.BluRay.mkv")

        self.assertEqual(result.status, "matched")
        self.assertFalse(result.need_confirm)
        self.assertNotEqual(result.threshold_decision["reason"], "low_information_title")

    def test_pipeline_returns_top3_breakdowns_queries_and_threshold_decision(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([
            {
                "id": 10, "name": "幕府将军", "original_name": "Shōgun",
                "aliases": ["Shogun"], "first_air_date": "2024-02-27",
                "media_type": "tv", "overview": "正确候选",
            },
            {
                "id": 11, "name": "幕府将军", "original_name": "Shōgun",
                "first_air_date": "1980-09-15", "media_type": "tv",
            },
            {
                "id": 12, "title": "幕府将军", "original_title": "Shogun",
                "release_date": "2024-01-01", "media_type": "movie",
            },
            {
                "id": 13, "name": "将军家的小娘子", "first_air_date": "2020-01-01",
                "media_type": "tv",
            },
        ], {
            "10": {"seasons": [{"season_number": 1, "episode_count": 10}]},
        })
        tmdb = scraper_module.TMDBScraper(client=client)

        with patch.object(tmdb, "_set_lock") as set_lock:
            result = tmdb.deterministic_recognize(
                "幕府将军.Shogun.2024.S01E01.2160p.WEB-DL.mkv",
                "/剧集/幕府将军 Shogun (2024)/Season 01",
            )

        self.assertIsInstance(result, scraper_module.RecognitionResult)
        self.assertEqual(result.tmdb_id, "10")
        self.assertEqual(result.provider, "tmdb")
        self.assertEqual(result.external_id, "10")
        self.assertEqual(result.matched_by, "search")
        self.assertEqual(len(result.candidates), 3)
        self.assertTrue(all(item.provider == "tmdb" for item in result.candidates))
        self.assertEqual(
            [item.external_id for item in result.candidates],
            [item.tmdb_id for item in result.candidates],
        )
        self.assertTrue(result.query_variants)
        self.assertTrue(result.threshold_decision["passed"])
        self.assertEqual(result.threshold_decision["score"], result.confidence)
        self.assertEqual(result.candidates[0].score_breakdown.final_score, result.candidates[0].score)
        self.assertNotIn("media_type_mismatch", result.rejected_constraints)
        self.assertTrue(all(item.media_type == "tv" for item in result.candidates))
        self.assertNotIn("12", [item.tmdb_id for item in result.candidates])
        self.assertTrue(all(call[2] == "tv" for call in client.search_calls))
        set_lock.assert_not_called()

    def test_same_name_same_year_near_tie_requires_confirmation(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([
            {
                "id": 101, "name": "同名作品", "original_name": "同名作品",
                "release_date": "2026-01-01", "media_type": "movie",
            },
            {
                "id": 202, "name": "同名作品", "original_name": "同名作品",
                "release_date": "2026-02-01", "media_type": "movie",
            },
        ])
        tmdb = scraper_module.TMDBScraper(client=client)

        result = tmdb.deterministic_recognize("同名作品.2026.mkv")

        self.assertTrue(result.need_confirm)
        self.assertEqual(result.status, "low_confidence")
        self.assertEqual(result.threshold_decision["reason"], "ambiguous_near_tie")
        self.assertIn("同名同年", result.error)

    def test_detail_alias_enrichment_matches_romanized_anime_title(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient(
            [{
                "id": 86034,
                "name": "平凡职业造就世界最强",
                "original_name": "ありふれた職業で世界最強",
                "first_air_date": "2019-07-08",
                "media_type": "tv",
            }],
            {
                "86034": {
                    "alternative_titles": {
                        "results": [{
                            "title": "Arifureta Shokugyou de Sekai Saikyou"
                        }]
                    },
                    "seasons": [{"season_number": 2, "episode_count": 12}],
                }
            },
        )
        tmdb = scraper_module.TMDBScraper(client=client)

        result = tmdb.deterministic_recognize(
            "Arifureta Shokugyou de Sekai Saikyou - 01.mkv",
            "/动漫/[H-Enc] Arifureta Shokugyou de Sekai Saikyou 2nd Season",
        )

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.tmdb_id, "86034")
        self.assertEqual(result.confidence, 1.0)
        self.assertIn(
            "Arifureta Shokugyou de Sekai Saikyou",
            result.candidates[0].aliases,
        )
        self.assertEqual(client.detail_calls, [("86034", "tv")])

    def test_exact_romaji_alias_shared_by_multiple_tmdb_ids_requires_confirmation(self):
        scraper_module = self.recognition_module()
        shared_alias = "Shared Romaji Title"
        client = _DeterministicClient(
            [
                {
                    "id": 101,
                    "name": "正确候选甲",
                    "original_name": "作品甲",
                    "first_air_date": "2024-01-01",
                    "media_type": "tv",
                },
                {
                    "id": 102,
                    "name": "正确候选乙",
                    "original_name": "作品乙",
                    "first_air_date": "2024-01-02",
                    "media_type": "tv",
                },
            ],
            {
                "101": {"alternative_titles": {"results": [{"title": shared_alias}]}},
                "102": {"alternative_titles": {"results": [{"title": shared_alias}]}},
            },
        )
        tmdb = scraper_module.TMDBScraper(client=client)

        result = tmdb.deterministic_recognize(
            f"{shared_alias} - 01.mkv", f"/动漫/{shared_alias}/Season 01"
        )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertFalse(result.threshold_decision["passed"])
        self.assertEqual(result.threshold_decision["reason"], "ambiguous_romaji_alias")
        self.assertIn("ambiguous_romaji_alias", result.rejected_constraints)
        self.assertEqual({item.tmdb_id for item in result.candidates}, {"101", "102"})
        self.assertEqual(client.detail_calls, [("101", "tv"), ("102", "tv")])

    def test_exact_alias_ambiguity_is_resolved_only_by_unique_tmdb_position(self):
        scraper_module = self.recognition_module()
        shared_alias = "Yamishibai"
        client = _DeterministicClient(
            [
                {
                    "id": 56559,
                    "name": "暗芝居",
                    "original_name": "闇芝居",
                    "first_air_date": "2013-07-15",
                    "media_type": "tv",
                },
                {
                    "id": 136895,
                    "name": "暗芝居（生）",
                    "original_name": "闇芝居（生）",
                    "first_air_date": "2020-09-10",
                    "media_type": "tv",
                },
            ],
            {
                "56559": {
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [{"season_number": 17, "episode_count": 13}],
                },
                "136895": {
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [{"season_number": 1, "episode_count": 13}],
                },
            },
        )

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            "Yamishibai.S17E05.1080p.WEB-DL.mkv"
        )

        self.assertEqual(result.status, "matched")
        self.assertFalse(result.need_confirm)
        self.assertEqual(result.tmdb_id, "56559")
        self.assertNotIn("ambiguous_romaji_alias", result.rejected_constraints)
        resolution = result.metadata["alias_position_resolution"]
        self.assertEqual(resolution["selected_tmdb_id"], "56559")
        self.assertEqual(resolution["excluded"][0]["reason"], "season_not_found")

    def test_enclosed_yami_shibai_high_season_uses_verified_fallback(self):
        scraper_module = self.recognition_module()
        filename = (
            "[UHA-WINGS&YUI-7][Yami Shibai 17][06]"
            "[x264 1080p][CHS].mp4"
        )
        candidates = [
            {
                "id": 56559,
                "name": "暗芝居",
                "original_name": "闇芝居",
                "first_air_date": "2013-07-15",
                "media_type": "tv",
            },
            {
                "id": 136895,
                "name": "暗芝居（生）",
                "original_name": "闇芝居（生）",
                "first_air_date": "2020-09-10",
                "media_type": "tv",
            },
        ]
        client = _QueryDeterministicClient({"Yami Shibai": candidates}, {
            "56559": {
                "alternative_titles": {"results": [{"title": "Yami Shibai"}]},
                "seasons": [{"season_number": 17, "episode_count": 13}],
            },
            "136895": {
                "alternative_titles": {"results": [{"title": "Yami Shibai"}]},
                "seasons": [{"season_number": 1, "episode_count": 13}],
            },
        })
        tmdb = scraper_module.TMDBScraper(client=client)

        result = tmdb.deterministic_recognize(filename, "1")
        parsed = tmdb.parse_media(filename, "1", result)

        self.assertEqual(result.status, "matched")
        self.assertFalse(result.need_confirm)
        self.assertEqual(result.tmdb_id, "56559")
        self.assertEqual(result.matched_by, "implicit_season_fallback")
        self.assertEqual(result.season_override, 17)
        self.assertEqual(
            (result.context.normalized_title, result.context.season, result.context.episode),
            ("Yami Shibai", 17, 6),
        )
        self.assertEqual((parsed.source_season, parsed.source_episode), (None, 6))
        self.assertEqual((parsed.effective_season, parsed.effective_episode), (17, 6))
        self.assertIn(("Yami Shibai 17", "", "tv"), client.search_calls)
        self.assertIn(("Yami Shibai", "", "tv"), client.search_calls)
        self.assertEqual(
            result.metadata["implicit_season_fallback"]["source_title"],
            "Yami Shibai 17",
        )

    def test_enclosed_numeric_title_prefers_primary_tmdb_identity(self):
        scraper_module = self.recognition_module()
        filename = "[Room 17][06][1080p].mkv"
        candidate = {
            "id": 1701,
            "name": "Room 17",
            "original_name": "Room 17",
            "first_air_date": "2024-01-01",
            "media_type": "tv",
        }
        client = _QueryDeterministicClient({"Room 17": [candidate]}, {
            "1701": {"seasons": [{"season_number": 1, "episode_count": 10}]},
        })

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            filename
        )

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.tmdb_id, "1701")
        self.assertEqual(result.context.normalized_title, "Room 17")
        self.assertIsNone(result.context.season)
        self.assertIsNone(result.season_override)
        self.assertNotIn(("Room", "", "tv"), client.search_calls)
        self.assertNotIn("implicit_season_fallback", result.metadata)

    def test_enclosed_season_fallback_requires_tmdb_position_proof(self):
        scraper_module = self.recognition_module()
        filename = "[UHA-WINGS&YUI-7][Yami Shibai 17][06][1080p].mp4"
        candidate = {
            "id": 56559,
            "name": "暗芝居",
            "original_name": "闇芝居",
            "first_air_date": "2013-07-15",
            "media_type": "tv",
        }
        client = _QueryDeterministicClient({"Yami Shibai": [candidate]}, {
            "56559": {
                "alternative_titles": {"results": [{"title": "Yami Shibai"}]},
                "seasons": [{"season_number": 16, "episode_count": 13}],
            },
        })

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            filename
        )

        self.assertEqual(result.status, "no_result")
        self.assertTrue(result.need_confirm)
        self.assertIsNone(result.season_override)
        self.assertEqual(result.context.normalized_title, "Yami Shibai 17")
        self.assertNotIn("implicit_season_fallback", result.metadata)

    def test_enclosed_numeric_title_is_not_overridden_by_single_base_series(self):
        scraper_module = self.recognition_module()
        filename = (
            "[UHA-WINGS&YUI-7][The Fantastic Four 17][06]"
            "[x264 1080p].mkv"
        )
        candidate = {
            "id": 99,
            "name": "The Fantastic Four",
            "original_name": "The Fantastic Four",
            "first_air_date": "2000-01-01",
            "media_type": "tv",
        }
        client = _QueryDeterministicClient({"The Fantastic Four": [candidate]}, {
            "99": {"seasons": [{"season_number": 17, "episode_count": 12}]},
        })

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            filename
        )

        self.assertEqual(result.status, "no_result")
        self.assertTrue(result.need_confirm)
        self.assertIsNone(result.season_override)
        self.assertEqual(result.context.normalized_title, "The Fantastic Four 17")
        self.assertIn(("The Fantastic Four", "", "tv"), client.search_calls)
        self.assertNotIn("implicit_season_fallback", result.metadata)

    def test_enclosed_season_fallback_rejects_language_only_release_evidence(self):
        scraper_module = self.recognition_module()
        filename = "[UHA-WINGS&YUI-7][Yami Shibai 17][06][CHS].mp4"
        candidates = [{
            "id": 56559,
            "name": "暗芝居",
            "original_name": "闇芝居",
            "first_air_date": "2013-07-15",
            "media_type": "tv",
        }]
        client = _QueryDeterministicClient({"Yami Shibai": candidates}, {
            "56559": {
                "alternative_titles": {"results": [{"title": "Yami Shibai"}]},
                "seasons": [{"season_number": 17, "episode_count": 13}],
            },
        })

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            filename
        )

        self.assertEqual(result.status, "no_result")
        self.assertNotIn(("Yami Shibai", "", "tv"), client.search_calls)
        self.assertIsNone(result.season_override)

    def test_enclosed_season_fallback_stops_when_exact_title_query_fails(self):
        scraper_module = self.recognition_module()
        filename = (
            "[UHA-WINGS&YUI-7][Yami Shibai 17][06]"
            "[x264 1080p][CHS].mp4"
        )
        candidates = [
            {
                "id": 56559,
                "name": "暗芝居",
                "original_name": "闇芝居",
                "first_air_date": "2013-07-15",
                "media_type": "tv",
            },
            {
                "id": 136895,
                "name": "暗芝居（生）",
                "original_name": "闇芝居（生）",
                "first_air_date": "2020-09-10",
                "media_type": "tv",
            },
        ]

        class _FailingExactClient(_QueryDeterministicClient):
            def search(self, title, year, media_type):
                self.search_calls.append((title, year, media_type))
                if title == "Yami Shibai 17":
                    raise scraper_module.ProviderUnavailable("temporary outage")
                return list(self.candidates_by_query.get(str(title), []))

        client = _FailingExactClient({"Yami Shibai": candidates}, {})

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            filename, "1"
        )

        self.assertEqual(result.status, "request_error")
        self.assertTrue(result.need_confirm)
        self.assertIsNone(result.season_override)
        self.assertNotIn(("Yami Shibai", "", "tv"), client.search_calls)
        attempts = result.metadata["search_attempts"]
        self.assertTrue(any(
            item["query"] == "Yami Shibai 17"
            and item["status"] == "request_error"
            for item in attempts
        ))

    def test_enclosed_season_fallback_rejects_cached_empty_exact_title(self):
        scraper_module = self.recognition_module()
        filename = (
            "[UHA-WINGS&YUI-7][Yami Shibai 17][06]"
            "[x264 1080p][CHS].mp4"
        )
        candidates = [
            {
                "id": 56559,
                "name": "暗芝居",
                "original_name": "闇芝居",
                "first_air_date": "2013-07-15",
                "media_type": "tv",
            },
            {
                "id": 136895,
                "name": "暗芝居（生）",
                "original_name": "闇芝居（生）",
                "first_air_date": "2020-09-10",
                "media_type": "tv",
            },
        ]
        client = _QueryDeterministicClient({"Yami Shibai": candidates}, {})
        tmdb = scraper_module.TMDBScraper(client=client)
        self.assertEqual(tmdb.search("Yami Shibai 17", "", "tv"), [])
        client.candidates_by_query["Yami Shibai 17"] = [{
            "id": 777,
            "name": "Yami Shibai 17",
            "original_name": "Yami Shibai 17",
            "first_air_date": "2026-01-01",
            "media_type": "tv",
        }]

        result = tmdb.deterministic_recognize(filename, "1")

        self.assertEqual(result.status, "no_result")
        self.assertTrue(result.need_confirm)
        self.assertIsNone(result.season_override)
        self.assertNotIn(("Yami Shibai", "", "tv"), client.search_calls)
        self.assertEqual(
            result.metadata["implicit_season_fallback_skipped"],
            "primary_title_unverified",
        )
        exact_attempt = next(
            item for item in result.metadata["search_attempts"]
            if item["query"] == "Yami Shibai 17" and item["year"] == ""
        )
        self.assertTrue(exact_attempt["cache_hit"])
        self.assertTrue(exact_attempt["empty_cache_hit"])

    def test_enclosed_season_fallback_cache_provenance_is_thread_local(self):
        scraper_module = self.recognition_module()
        filename = (
            "[UHA-WINGS&YUI-7][Yami Shibai 17][06]"
            "[x264 1080p][CHS].mp4"
        )
        candidates = [
            {
                "id": 56559,
                "name": "暗芝居",
                "original_name": "闇芝居",
                "first_air_date": "2013-07-15",
                "media_type": "tv",
            },
            {
                "id": 136895,
                "name": "暗芝居（生）",
                "original_name": "闇芝居（生）",
                "first_air_date": "2020-09-10",
                "media_type": "tv",
            },
        ]
        client = _QueryDeterministicClient({"Yami Shibai": candidates}, {})
        tmdb = scraper_module.TMDBScraper(client=client)
        self.assertEqual(tmdb.search("Yami Shibai 17", "", "tv"), [])
        reached = threading.Event()
        release = threading.Event()
        worker_result = []
        original_take = tmdb._take_thread_search_outcome
        blocked = False

        def gated_take(results):
            nonlocal blocked
            if threading.current_thread().name == "recognition-a" and not blocked:
                blocked = True
                reached.set()
                self.assertTrue(release.wait(3))
            return original_take(results)

        def recognize():
            worker_result.append(tmdb.deterministic_recognize(filename, "1"))

        with patch.object(tmdb, "_take_thread_search_outcome", side_effect=gated_take):
            worker = threading.Thread(target=recognize, name="recognition-a")
            worker.start()
            self.assertTrue(reached.wait(3))
            self.assertEqual(tmdb.search("Unrelated Fresh Empty", "", "tv"), [])
            release.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())

        self.assertEqual(len(worker_result), 1)
        result = worker_result[0]
        self.assertEqual(result.status, "no_result")
        self.assertIsNone(result.season_override)
        self.assertNotIn(("Yami Shibai", "", "tv"), client.search_calls)
        exact_attempt = next(
            item for item in result.metadata["search_attempts"]
            if item["query"] == "Yami Shibai 17" and item["year"] == ""
        )
        self.assertTrue(exact_attempt["cache_hit"])
        self.assertTrue(exact_attempt["empty_cache_hit"])

    def test_enclosed_season_fallback_rejects_multiple_candidates_at_same_position(self):
        scraper_module = self.recognition_module()
        filename = (
            "[UHA-WINGS&YUI-7][Yami Shibai 17][06]"
            "[x264 1080p][CHS].mp4"
        )
        candidates = [
            {
                "id": 56559,
                "name": "暗芝居",
                "original_name": "闇芝居",
                "first_air_date": "2013-07-15",
                "media_type": "tv",
            },
            {
                "id": 136895,
                "name": "暗芝居（生）",
                "original_name": "闇芝居（生）",
                "first_air_date": "2020-09-10",
                "media_type": "tv",
            },
        ]
        shared_detail = {
            "alternative_titles": {"results": [{"title": "Yami Shibai"}]},
            "seasons": [{"season_number": 17, "episode_count": 13}],
        }
        client = _QueryDeterministicClient({"Yami Shibai": candidates}, {
            "56559": shared_detail,
            "136895": shared_detail,
        })

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            filename, "1"
        )

        self.assertEqual(result.status, "no_result")
        self.assertTrue(result.need_confirm)
        self.assertIsNone(result.season_override)
        self.assertNotIn("implicit_season_fallback", result.metadata)

    def test_match_preserves_verified_implicit_season_context_and_effective_position(self):
        scraper_module = self.recognition_module()
        filename = (
            "[UHA-WINGS&YUI-7][Yami Shibai 17][06]"
            "[x264 1080p][CHS].mp4"
        )
        candidates = [
            {
                "id": 56559,
                "name": "暗芝居",
                "original_name": "闇芝居",
                "first_air_date": "2013-07-15",
                "media_type": "tv",
            },
            {
                "id": 136895,
                "name": "暗芝居（生）",
                "original_name": "闇芝居（生）",
                "first_air_date": "2020-09-10",
                "media_type": "tv",
            },
        ]
        client = _QueryDeterministicClient({"Yami Shibai": candidates}, {
            "56559": {
                "alternative_titles": {"results": [{"title": "Yami Shibai"}]},
                "seasons": [{"season_number": 17, "episode_count": 13}],
            },
            "136895": {
                "alternative_titles": {"results": [{"title": "Yami Shibai"}]},
                "seasons": [{"season_number": 1, "episode_count": 13}],
            },
        })
        tmdb = scraper_module.TMDBScraper(client=client)

        with patch.object(tmdb, "_get_lock", return_value=None), patch(
            "app.modules.tmdb_regex_rules.find_tmdb_regex_match", return_value=None,
        ), patch(
            "app.modules.media_aliases.lookup_manual_alias", return_value=None,
        ):
            result = tmdb.match(filename, "1")
        parsed = tmdb.parse_media(filename, "1", result)

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.matched_by, "implicit_season_fallback")
        self.assertEqual(result.season_override, 17)
        self.assertEqual((result.effective_season, result.effective_episode), (17, 6))
        self.assertEqual((result.context.season, result.context.episode), (17, 6))
        self.assertEqual((parsed.source_season, parsed.source_episode), (None, 6))
        self.assertEqual((parsed.effective_season, parsed.effective_episode), (17, 6))

    def test_exact_alias_ambiguity_can_use_source_year_and_tmdb_position(self):
        scraper_module = self.recognition_module()
        shared_alias = "A Certain Magical Index"
        client = _DeterministicClient(
            [
                {
                    "id": 30980,
                    "name": "魔法禁书目录",
                    "original_name": "とある魔術の禁書目録",
                    "first_air_date": "2008-10-05",
                    "media_type": "tv",
                },
                {
                    "id": 312314,
                    "name": "Toaru",
                    "original_name": "Toaru",
                    # 搜索响应年份可能来自不完整的兼容 API；先让两个候选都
                    # 进入精确别名歧义集，再由详情中的权威年份做硬排除。
                    "first_air_date": "2008-01-01",
                    "media_type": "tv",
                },
            ],
            {
                "30980": {
                    "id": 30980,
                    "first_air_date": "2008-10-05",
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [{
                        "season_number": 1,
                        "episode_count": 24,
                        "air_date": "2008-10-05",
                    }],
                },
                "312314": {
                    "id": 312314,
                    "first_air_date": "2004-01-01",
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [{
                        "season_number": 1,
                        "episode_count": 12,
                        "air_date": "2004-01-01",
                    }],
                },
            },
        )

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            "A Certain Magical Index.2008.S01E01.1080p.WEB-DL.mkv"
        )

        self.assertEqual(result.status, "matched")
        self.assertFalse(result.need_confirm)
        self.assertEqual(result.tmdb_id, "30980")
        self.assertNotIn("ambiguous_romaji_alias", result.rejected_constraints)
        resolution = result.metadata["alias_position_resolution"]
        self.assertEqual(resolution["selected_tmdb_id"], "30980")
        self.assertEqual(resolution["excluded"][0]["tmdb_id"], "312314")
        self.assertEqual(resolution["excluded"][0]["reason"], "year_mismatch")
        self.assertEqual(
            resolution["excluded"][0]["year_reason"],
            "target_season_year_mismatch",
        )

    def test_exact_alias_year_does_not_resolve_when_multiple_candidates_still_pass(self):
        scraper_module = self.recognition_module()
        shared_alias = "Shared Official Alias"
        candidates = [
            {
                "id": candidate_id,
                "name": title,
                "first_air_date": "2008-01-01",
                "media_type": "tv",
            }
            for candidate_id, title in ((101, "候选甲"), (102, "候选乙"))
        ]
        details = {
            str(candidate_id): {
                "id": candidate_id,
                "alternative_titles": {"results": [{"title": shared_alias}]},
                "seasons": [{"season_number": 1, "episode_count": 12}],
            }
            for candidate_id in (101, 102)
        }

        result = scraper_module.TMDBScraper(
            client=_DeterministicClient(candidates, details)
        ).deterministic_recognize(
            "Shared Official Alias.2008.S01E01.1080p.WEB-DL.mkv"
        )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertEqual(result.threshold_decision["reason"], "ambiguous_romaji_alias")
        self.assertIn("ambiguous_romaji_alias", result.rejected_constraints)
        self.assertNotIn("alias_position_resolution", result.metadata)

    def test_exact_alias_can_use_absolute_episode_only_for_unique_long_running_candidate(self):
        scraper_module = self.recognition_module()
        shared_alias = "One Piece"
        client = _DeterministicClient(
            [
                {
                    "id": 111110,
                    "name": "航海王 真人版",
                    "first_air_date": "2023-08-31",
                    "media_type": "tv",
                },
                {
                    "id": 37854,
                    "name": "航海王",
                    "original_name": "ワンピース",
                    "first_air_date": "1999-10-20",
                    "media_type": "tv",
                },
                {
                    "id": 241709,
                    "name": "THE ONE PIECE",
                    "first_air_date": "2026-01-01",
                    "media_type": "tv",
                },
            ],
            {
                "111110": {
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [{"season_number": 1, "episode_count": 8}],
                },
                "37854": {
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [
                        {"season_number": 1, "episode_count": 100},
                        {"season_number": 2, "episode_count": 1100},
                    ],
                },
                "241709": {
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [{"season_number": 1, "episode_count": 25}],
                },
            },
        )

        for filename, expected_source_season in (
            ("One.Piece.S01E1173.1080p.WEB-DL.mkv", 1),
            ("One Piece - 1173 [1080p].mkv", None),
        ):
            with self.subTest(filename=filename):
                result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
                    filename
                )

                self.assertEqual(result.status, "matched")
                self.assertFalse(result.need_confirm)
                self.assertEqual(result.tmdb_id, "37854")
                mapping = result.metadata["episode_mapping"]
                self.assertEqual(mapping["mode"], "absolute")
                self.assertEqual(
                    (mapping["source_season"], mapping["source_episode"]),
                    (expected_source_season, 1173),
                )
                self.assertEqual(
                    (mapping["target_season"], mapping["target_episode"]),
                    (2, 1073),
                )
                resolution = result.metadata["alias_position_resolution"]
                self.assertEqual(resolution["selected_tmdb_id"], "37854")
                self.assertEqual(
                    (resolution["source_season"], resolution["source_episode"]),
                    (expected_source_season, 1173),
                )

    def test_exact_chinese_title_uses_split_cour_position_to_select_unique_tmdb_identity(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient(
            [
                {
                    "id": 102008,
                    "name": "大主宰",
                    "first_air_date": "2019-08-08",
                    "media_type": "tv",
                },
                {
                    "id": 226045,
                    "name": "大主宰",
                    "first_air_date": "2023-06-30",
                    "media_type": "tv",
                },
            ],
            {
                "102008": {
                    "id": 102008,
                    "seasons": [{"season_number": 1, "episode_count": 12}],
                },
                "226045": {
                    "id": 226045,
                    "seasons": [{"season_number": 1, "episode_count": 104}],
                },
            },
        )
        tmdb = scraper_module.TMDBScraper(client=client)

        def season_detail(tmdb_id, season):
            if str(tmdb_id) == "102008":
                start = date(2019, 8, 8)
                count = 12
                second_start = None
            else:
                start = date(2023, 6, 30)
                count = 104
                second_start = date(2026, 1, 9)
            episodes = []
            for number in range(1, count + 1):
                if second_start is not None and number >= 53:
                    aired_on = second_start + timedelta(days=7 * (number - 53))
                else:
                    aired_on = start + timedelta(days=7 * (number - 1))
                episodes.append({
                    "episode_number": number,
                    "air_date": aired_on.isoformat(),
                })
            return {"season_number": season, "episodes": episodes}

        with patch.object(tmdb, "get_tv_season_detail", side_effect=season_detail):
            result = tmdb.deterministic_recognize(
                "大主宰.S02E33.1080p.WEB-DL.mkv"
            )

        self.assertEqual((result.status, result.need_confirm), ("matched", False))
        self.assertEqual(result.tmdb_id, "226045")
        mapping = result.metadata["episode_mapping"]
        self.assertEqual(
            (mapping["source_season"], mapping["source_episode"]), (2, 33)
        )
        self.assertEqual(
            (mapping["target_season"], mapping["target_episode"]), (1, 85)
        )
        self.assertEqual(
            mapping["reason"], "publisher_cour_mapped_to_merged_tmdb_season"
        )
        resolution = result.metadata["alias_position_resolution"]
        self.assertEqual(resolution["selected_tmdb_id"], "226045")
        self.assertEqual(resolution["excluded"][0]["tmdb_id"], "102008")

    def test_exact_alias_position_resolution_stays_fail_closed_when_multiple_candidates_fit(self):
        scraper_module = self.recognition_module()
        shared_alias = "Shared Position Title"
        client = _DeterministicClient(
            [
                {
                    "id": 201,
                    "name": "候选甲",
                    "first_air_date": "2024-01-01",
                    "media_type": "tv",
                },
                {
                    "id": 202,
                    "name": "候选乙",
                    "first_air_date": "2024-01-01",
                    "media_type": "tv",
                },
            ],
            {
                "201": {
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [{"season_number": 2, "episode_count": 12}],
                },
                "202": {
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [{"season_number": 2, "episode_count": 24}],
                },
            },
        )

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            f"{shared_alias}.S02E05.1080p.mkv"
        )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertEqual(result.threshold_decision["reason"], "ambiguous_romaji_alias")
        self.assertNotIn("alias_position_resolution", result.metadata)

    def test_bare_absolute_alias_resolution_stays_fail_closed_when_multiple_candidates_fit(self):
        scraper_module = self.recognition_module()
        shared_alias = "Shared Long Running Title"
        client = _DeterministicClient(
            [
                {"id": 211, "name": "候选甲", "media_type": "tv"},
                {"id": 212, "name": "候选乙", "media_type": "tv"},
            ],
            {
                "211": {
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [{"season_number": 1, "episode_count": 1200}],
                },
                "212": {
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [
                        {"season_number": 1, "episode_count": 100},
                        {"season_number": 2, "episode_count": 1100},
                    ],
                },
            },
        )

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            f"{shared_alias} - 1173 [1080p].mkv"
        )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertEqual(result.threshold_decision["reason"], "ambiguous_romaji_alias")
        self.assertNotIn("alias_position_resolution", result.metadata)
        self.assertNotIn("episode_mapping", result.metadata)

    def test_absolute_alias_probe_is_not_used_for_ordinary_episode_numbers(self):
        scraper_module = self.recognition_module()
        shared_alias = "Ordinary Alias Show"
        client = _DeterministicClient(
            [
                {"id": 301, "name": "候选甲", "media_type": "tv"},
                {"id": 302, "name": "候选乙", "media_type": "tv"},
            ],
            {
                "301": {
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [
                        {"season_number": 1, "episode_count": 8},
                        {"season_number": 2, "episode_count": 12},
                    ],
                },
                "302": {
                    "alternative_titles": {"results": [{"title": shared_alias}]},
                    "seasons": [{"season_number": 1, "episode_count": 12}],
                },
            },
        )

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            f"{shared_alias}.S01E12.1080p.mkv"
        )

        # 301 若按绝对编号可解释为 S02E04，但普通集号不得启用该补救；
        # 302 的原始 S01E12 合法，因而唯一位置候选仍可安全选中 302。
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.tmdb_id, "302")
        self.assertNotIn("episode_mapping", result.metadata)

    def test_romaji_alias_ambiguity_is_checked_beyond_top_three_candidates(self):
        scraper_module = self.recognition_module()
        shared_alias = "Shared Romaji Overflow"
        raw_candidates = [
            {
                "id": candidate_id,
                "name": f"候选{candidate_id}",
                "original_name": f"原名{candidate_id}",
                "first_air_date": "2024-01-01",
                "media_type": "tv",
            }
            for candidate_id in (101, 102, 103, 104)
        ]
        details = {
            str(candidate_id): {
                "alternative_titles": {
                    "results": ([{"title": shared_alias}] if candidate_id in {103, 104} else [])
                }
            }
            for candidate_id in (101, 102, 103, 104)
        }
        client = _DeterministicClient(raw_candidates, details)
        tmdb = scraper_module.TMDBScraper(client=client)

        result = tmdb.deterministic_recognize(
            f"{shared_alias} - 01.mkv", f"/动漫/{shared_alias}/Season 01"
        )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertEqual(result.threshold_decision["reason"], "ambiguous_romaji_alias")
        self.assertEqual(client.detail_calls, [
            ("101", "tv"), ("102", "tv"), ("103", "tv"), ("104", "tv")
        ])

    def test_romaji_alias_position_resolution_covers_up_to_twenty_search_candidates(self):
        scraper_module = self.recognition_module()
        shared_alias = "Shared Long Running Overflow"

        for candidate_count in (9, 20):
            with self.subTest(candidate_count=candidate_count):
                candidate_ids = list(range(1001, 1001 + candidate_count))
                selected_id, excluded_id = candidate_ids[-2:]
                raw_candidates = [
                    {
                        "id": candidate_id,
                        "name": f"候选{candidate_id}",
                        "original_name": f"原名{candidate_id}",
                        "first_air_date": "2024-01-01",
                        "media_type": "tv",
                    }
                    for candidate_id in candidate_ids
                ]
                details = {
                    str(candidate_id): {
                        "alternative_titles": {
                            "results": (
                                [{"title": shared_alias}]
                                if candidate_id in {selected_id, excluded_id}
                                else []
                            )
                        },
                        "seasons": [{
                            "season_number": 1,
                            "episode_count": (
                                1200 if candidate_id == selected_id else 100
                            ),
                        }],
                    }
                    for candidate_id in candidate_ids
                }
                client = _DeterministicClient(raw_candidates, details)

                result = scraper_module.TMDBScraper(
                    client=client
                ).deterministic_recognize(
                    f"{shared_alias} - 1173 [1080p].mkv"
                )

                self.assertEqual(result.status, "matched")
                self.assertFalse(result.need_confirm)
                self.assertEqual(result.tmdb_id, str(selected_id))
                self.assertEqual(
                    result.metadata["alias_position_resolution"]["candidate_count"],
                    2,
                )
                self.assertEqual(
                    client.detail_calls,
                    [(str(candidate_id), "tv") for candidate_id in candidate_ids],
                )

    def test_romaji_alias_position_resolution_over_enrichment_limit_stays_closed(self):
        scraper_module = self.recognition_module()
        shared_alias = "Shared Long Running Overflow"
        candidate_ids = list(range(2001, 2022))
        selected_id, excluded_id = candidate_ids[:2]
        raw_candidates = [
            {
                "id": candidate_id,
                "name": f"候选{candidate_id}",
                "original_name": f"原名{candidate_id}",
                "first_air_date": "2024-01-01",
                "media_type": "tv",
            }
            for candidate_id in candidate_ids
        ]
        details = {
            str(candidate_id): {
                "alternative_titles": {
                    "results": (
                        [{"title": shared_alias}]
                        if candidate_id in {selected_id, excluded_id}
                        else []
                    )
                },
                "seasons": [{
                    "season_number": 1,
                    "episode_count": 1200 if candidate_id == selected_id else 100,
                }],
            }
            for candidate_id in candidate_ids
        }
        client = _DeterministicClient(raw_candidates, details)

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            f"{shared_alias} - 1173 [1080p].mkv"
        )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertNotIn("alias_position_resolution", result.metadata)
        self.assertEqual(len(client.detail_calls), 20)

    def test_romaji_alias_detail_coverage_failure_requires_confirmation(self):
        scraper_module = self.recognition_module()

        class FailingDetailClient(_DeterministicClient):
            def detail(self, tmdb_id, media_type):
                if str(tmdb_id) == "3002":
                    self.detail_calls.append((str(tmdb_id), media_type))
                    raise RuntimeError("detail unavailable")
                return super().detail(tmdb_id, media_type)

        shared_alias = "Unique Latin Identity"
        client = FailingDetailClient(
            [
                {
                    "id": 3001,
                    "name": shared_alias,
                    "media_type": "tv",
                    "first_air_date": "2024-01-01",
                },
                {
                    "id": 3002,
                    "name": "Unrelated Candidate",
                    "media_type": "tv",
                    "first_air_date": "2024-01-01",
                },
            ],
            {
                "3001": {
                    "alternative_titles": {"results": []},
                    "seasons": [{"season_number": 1, "episode_count": 12}],
                },
            },
        )

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            f"{shared_alias}.S01E01.1080p.mkv"
        )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertEqual(
            result.threshold_decision["reason"],
            "romaji_alias_coverage_incomplete",
        )
        self.assertNotIn("alias_position_resolution", result.metadata)

    def test_detail_alias_enrichment_is_cached_across_episodes(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient(
            [{
                "id": 117933,
                "name": "夏日重现",
                "original_name": "サマータイムレンダ",
                "first_air_date": "2022-04-15",
                "media_type": "tv",
            }],
            {
                "117933": {
                    "alternative_titles": {
                        "results": [{"title": "Summer Time Rendering"}]
                    },
                    "seasons": [{"season_number": 1, "episode_count": 25}],
                }
            },
        )
        tmdb = scraper_module.TMDBScraper(client=client)

        first = tmdb.deterministic_recognize(
            "Summer Time Rendering - 01.mkv", "/动漫/Summer Time Rendering"
        )
        second = tmdb.deterministic_recognize(
            "Summer Time Rendering - 02.mkv", "/动漫/Summer Time Rendering"
        )

        self.assertEqual((first.status, second.status), ("matched", "matched"))
        self.assertEqual((first.tmdb_id, second.tmdb_id), ("117933", "117933"))
        self.assertEqual(client.detail_calls, [("117933", "tv")])

    def test_ordinal_attack_season_alias_can_match_the_base_series_title(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 105556,
            "name": "不要欺负我，长瀞同学",
            "first_air_date": "2021-04-11",
            "media_type": "tv",
        }], details={"105556": {
            "id": 105556,
            "name": "不要欺负我，长瀞同学",
            "first_air_date": "2021-04-11",
            "seasons": [{"season_number": 2, "episode_count": 12}],
        }})
        tmdb = scraper_module.TMDBScraper(client=client)
        filename = (
            "[ANi] 不要欺负我，长瀞同学 2nd Attack（仅限港澳台地区） - 04 "
            "[1080P][Bilibili][WEB-DL][AAC AVC][CHT CHS][MP4].mp4"
        )

        result = tmdb.deterministic_recognize(
            filename, f"/动漫/{filename.rsplit('.', 1)[0]}"
        )

        self.assertEqual(result.status, "matched")
        self.assertFalse(result.need_confirm)
        self.assertEqual(result.tmdb_id, "105556")
        self.assertNotIn("distinctive_title_tokens_missing", result.rejected_constraints)

    def test_ordinal_attack_alias_does_not_fold_latin_title_into_base_show(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 229619,
            "name": "Art Attack",
            "first_air_date": "2000-06-15",
            "media_type": "tv",
        }], {
            "229619": {"seasons": [{"season_number": 2, "episode_count": 20}]},
        })
        tmdb = scraper_module.TMDBScraper(client=client)

        result = tmdb.deterministic_recognize(
            "Art Attack 2nd Attack - 04.mkv",
            "/TV/Art Attack 2nd Attack",
        )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertIn("ambiguous_ordinal_attack_alias", result.rejected_constraints)

    def test_noisy_ani_title_does_not_auto_match_art_attack(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 229619,
            "name": "Art Attack",
            "first_air_date": "2000-06-15",
            "media_type": "tv",
        }])
        tmdb = scraper_module.TMDBScraper(client=client)
        filename = (
            "[ANi] 不要欺负我，长瀞同学 2nd Attack（仅限港澳台地区） - 04 "
            "[1080P][Bilibili][WEB-DL][AAC AVC][CHT CHS][MP4].mp4"
        )

        result = tmdb.deterministic_recognize(
            filename, f"/动漫/{filename.rsplit('.', 1)[0]}"
        )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertLess(result.confidence, 0.9)
        self.assertNotIn("Attack", result.query_variants)

    def test_full_title_suffix_beats_short_franchise_alias(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([
            {
                "id": 19836,
                "name": "魔法少女奈叶",
                "original_name": "魔法少女リリカルなのは",
                "aliases": ["魔法少女奈葉"],
                "first_air_date": "2004-10-01",
                "media_type": "tv",
            },
            {
                "id": 287075,
                "name": "魔法少女奈叶 EXCEEDS复仇枪焰",
                "original_name": "魔法少女リリカルなのは EXCEEDS Gun Blaze Vengeance",
                "aliases": ["魔法少女奈葉 EXCEEDS Gun Blaze Vengeance"],
                "first_air_date": "2026-04-01",
                "media_type": "tv",
            },
        ], {
            "287075": {"seasons": [{"season_number": 1, "episode_count": 12}]},
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        filename = (
            "[ANi] 魔法少女奈葉 EXCEEDS Gun Blaze Vengeance - 04 "
            "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        )

        result = tmdb.deterministic_recognize(
            filename, "/动漫/魔法少女奈葉"
        )

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.tmdb_id, "287075")
        old_candidate = next(item for item in result.candidates if item.tmdb_id == "19836")
        self.assertIn(
            "distinctive_title_tokens_missing",
            old_candidate.score_breakdown.rejected_constraints,
        )
        self.assertLess(old_candidate.score, result.confidence)

        old_only = scraper_module.TMDBScraper(
            client=_DeterministicClient(client.candidates[:1])
        ).deterministic_recognize(
            filename, "/动漫/魔法少女奈葉"
        )
        self.assertEqual(old_only.status, "low_confidence")
        self.assertEqual(
            old_only.threshold_decision["reason"],
            "distinctive_title_tokens_missing",
        )
        self.assertIn("完整标题", old_only.error)

    def test_official_multilingual_original_covers_bilingual_release_title(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 229676,
            "name": "魔域英雄传说",
            "original_name": "Übel Blatt～ユーベルブラット～",
            "first_air_date": "2025-01-11",
            "media_type": "tv",
        }], {
            "229676": {"seasons": [{"season_number": 1, "episode_count": 12}]},
        })
        tmdb = scraper_module.TMDBScraper(client=client)

        result = tmdb.deterministic_recognize(
            "[ANi] 魔域英雄传说 Ubel Blatt - 01 "
            "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        )

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.tmdb_id, "229676")
        self.assertNotIn("distinctive_title_tokens_missing", result.rejected_constraints)

    def test_unofficial_bilingual_remainder_still_requires_confirmation(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 229676,
            "name": "魔域英雄传说",
            "original_name": "Übel Blatt～ユーベルブラット～",
            "first_air_date": "2025-01-11",
            "media_type": "tv",
        }])
        tmdb = scraper_module.TMDBScraper(client=client)

        result = tmdb.deterministic_recognize(
            "[ANi] 魔域英雄传说 Totally Different - 01 "
            "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        )

        self.assertEqual(result.status, "low_confidence")
        self.assertIn("distinctive_title_tokens_missing", result.rejected_constraints)

    def test_long_traditional_title_matches_simplified_candidate(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 325158,
            "name": "无用圣女的异世界美食之旅 凭借隐藏技能召唤露营车",
            "original_name": "無用聖女の異世界グルメ旅",
            "first_air_date": "2026-04-01",
            "media_type": "tv",
        }], {
            "325158": {"seasons": [{"season_number": 1, "episode_count": 12}]},
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        filename = (
            "[ANi] 無用聖女的異世界美食之旅 憑藉隱藏技能召喚露營車 - 05 "
            "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        )

        result = tmdb.deterministic_recognize(filename)

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.tmdb_id, "325158")
        self.assertGreaterEqual(result.confidence, 0.9)

    def test_tv_position_validation_blocks_missing_season_and_episode(self):
        scraper_module = self.recognition_module()
        candidate = {
            "id": 9001, "name": "Example Show", "first_air_date": "2024-01-01",
            "media_type": "tv",
        }
        missing_season = scraper_module.TMDBScraper(client=_DeterministicClient(
            [candidate], {"9001": {
                "seasons": [{"season_number": 1, "episode_count": 12}],
            }},
        )).deterministic_recognize("Example.Show.S02E03.1080p.mkv")
        out_of_range = scraper_module.TMDBScraper(client=_DeterministicClient(
            [candidate], {"9001": {
                "seasons": [{"season_number": 2, "episode_count": 2}],
            }},
        )).deterministic_recognize("Example.Show.S02E03.1080p.mkv")

        self.assertEqual(missing_season.status, "low_confidence")
        self.assertIn("tmdb_position_season_not_found", missing_season.rejected_constraints)
        self.assertEqual(out_of_range.status, "low_confidence")
        self.assertIn("tmdb_position_episode_out_of_range", out_of_range.rejected_constraints)

    @staticmethod
    def _controlled_breakdown(scraper_module, score: float, matched_title: str):
        return scraper_module.CandidateScoreBreakdown(
            title_score=score,
            original_title_score=0.2,
            alias_score=0.1,
            year_score=1.0,
            year_penalty=0.0,
            media_type_score=1.0,
            constraint_penalty=0.0,
            final_score=score,
            matched_title=matched_title,
            rejected_constraints=[],
        )

    def test_target_season_air_year_can_resolve_series_premiere_year_mismatch(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 9001,
            "name": "Example Show",
            "first_air_date": "2020-01-01",
            "media_type": "tv",
        }], {
            "9001": {
                "id": 9001,
                "name": "Example Show",
                "first_air_date": "2020-01-01",
                "seasons": [{
                    "season_number": 3,
                    "episode_count": 12,
                    "air_date": "2024-01-05",
                }],
            },
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "strict"

        result = tmdb.deterministic_recognize(
            "Example.Show.2024.S03E02.1080p.mkv"
        )

        self.assertEqual(result.status, "matched")
        self.assertFalse(result.need_confirm)
        self.assertEqual(result.tmdb_id, "9001")
        self.assertEqual(result.confidence, 1.0)
        evidence = result.metadata.get("target_season_year_evidence")
        self.assertIsInstance(evidence, dict)
        self.assertEqual(evidence["expected_year"], "2024")
        self.assertEqual((evidence["target_season"], evidence["target_episode"]), (3, 2))

    def test_target_season_air_year_accepts_unique_exact_alias_over_derivative_title(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([
            {
                "id": 61415,
                "name": "命运之夜 无限剑制",
                "original_name": "Fate/stay night [Unlimited Blade Works]",
                "first_air_date": "2014-10-12",
                "media_type": "tv",
            },
            {
                "id": 331020,
                "name": "Fate/Stay Night: Unlimited Blade Works Abridged",
                "original_name": "Fate/Stay Night: Unlimited Blade Works Abridged",
                "first_air_date": "2017-01-01",
                "media_type": "tv",
            },
        ], {
            "61415": {
                "id": 61415,
                "name": "命运之夜 无限剑制",
                "original_name": "Fate/stay night [Unlimited Blade Works]",
                "first_air_date": "2014-10-12",
                "seasons": [{
                    "season_number": 2,
                    "episode_count": 13,
                    "air_date": "2015-04-05",
                }],
            },
            "331020": {
                "id": 331020,
                "name": "Fate/Stay Night: Unlimited Blade Works Abridged",
                "first_air_date": "2017-01-01",
                "seasons": [{
                    "season_number": 2,
                    "episode_count": 13,
                    "air_date": "2018-01-01",
                }],
            },
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "strict"

        result = tmdb.deterministic_recognize(
            "Fate stay night Unlimited Blade Works 2015 "
            "S02E01-[1080p][BDRIP][x265.OPUS].mkv"
        )

        self.assertEqual((result.status, result.need_confirm), ("matched", False))
        self.assertEqual(result.tmdb_id, "61415")
        self.assertEqual(result.confidence, 1.0)
        evidence = result.metadata.get("target_season_year_evidence")
        self.assertIsInstance(evidence, dict)
        self.assertEqual(evidence["expected_year"], "2015")
        self.assertEqual(
            (evidence["target_season"], evidence["target_episode"]), (2, 1)
        )

    def test_type_specific_tv_search_without_media_type_keeps_tv_context(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 94664,
            "name": "无职转生到了异世界就拿出真本事",
            "original_name": "無職転生 ～異世界行ったら本気だす～",
            "first_air_date": "2021-01-11",
            # TMDB /search/tv 响应本来就可能不携带 media_type。
        }], {
            "94664": {
                "id": 94664,
                "name": "无职转生到了异世界就拿出真本事",
                "original_name": "無職転生 ～異世界行ったら本気だす～",
                "first_air_date": "2021-01-11",
                "alternative_titles": {"results": [{"title": "Mushoku Tensei III"}]},
                "seasons": [{
                    "season_number": 3,
                    "episode_count": 12,
                    "air_date": "2026-01-05",
                }],
            },
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "strict"

        result = tmdb.deterministic_recognize(
            "Mushoku.Tensei.III.2026.S03E01.1080p.mkv"
        )

        self.assertEqual((result.status, result.need_confirm), ("matched", False))
        self.assertEqual((result.tmdb_id, result.media_type), ("94664", "tv"))
        self.assertEqual(result.confidence, 1.0)
        evidence = result.metadata.get("target_season_year_evidence")
        self.assertIsInstance(evidence, dict)
        self.assertEqual((evidence["target_season"], evidence["target_episode"]), (3, 1))

    def test_type_specific_tv_search_rejects_explicit_movie_result(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 9001,
            "title": "Example Show",
            "release_date": "2024-01-01",
            "media_type": "movie",
        }])
        tmdb = scraper_module.TMDBScraper(client=client)

        result = tmdb.deterministic_recognize(
            "Example.Show.S01E01.1080p.mkv", media_type_hint="tv"
        )

        self.assertEqual(result.status, "no_result")
        self.assertTrue(result.need_confirm)
        self.assertEqual(result.candidates, [])

    def test_bracket_tv_season_marker_can_bind_target_season_air_year(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 9001,
            "name": "Example Show",
            "first_air_date": "2020-01-01",
            "media_type": "tv",
        }], {
            "9001": {
                "id": 9001,
                "name": "Example Show",
                "first_air_date": "2020-01-01",
                "seasons": [{
                    "season_number": 3,
                    "episode_count": 12,
                    "air_date": "2024-01-05",
                }],
            },
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "strict"

        result = tmdb.deterministic_recognize(
            "02.mkv", "/动漫/Example Show (2024)/[TV-3]"
        )

        self.assertEqual(result.status, "matched")
        self.assertFalse(result.need_confirm)
        self.assertEqual(result.tmdb_id, "9001")
        self.assertEqual(result.confidence, 1.0)
        evidence = result.metadata.get("target_season_year_evidence")
        self.assertIsInstance(evidence, dict)
        self.assertEqual((evidence["target_season"], evidence["target_episode"]), (3, 2))

    def test_target_season_air_year_fails_closed_when_detail_year_differs(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 9001,
            "name": "Example Show",
            "first_air_date": "2020-01-01",
            "media_type": "tv",
        }], {
            "9001": {
                "id": 9001,
                "seasons": [{
                    "season_number": 3,
                    "episode_count": 12,
                    "air_date": "2023-01-05",
                }],
            },
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "strict"

        result = tmdb.deterministic_recognize(
            "Example.Show.2024.S03E02.1080p.mkv"
        )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertNotIn("target_season_year_evidence", result.metadata)

    def test_target_season_air_year_does_not_resolve_two_strong_candidates(self):
        scraper_module = self.recognition_module()
        candidates = [
            {
                "id": 9001,
                "name": "Example Show",
                "first_air_date": "2020-01-01",
                "media_type": "tv",
            },
            {
                "id": 9002,
                "name": "Example Show",
                "first_air_date": "2021-01-01",
                "media_type": "tv",
            },
        ]
        details = {
            str(item["id"]): {
                "id": item["id"],
                "seasons": [{
                    "season_number": 3,
                    "episode_count": 12,
                    "air_date": "2024-01-05",
                }],
            }
            for item in candidates
        }
        tmdb = scraper_module.TMDBScraper(
            client=_DeterministicClient(candidates, details)
        )
        tmdb.match_mode = "strict"

        result = tmdb.deterministic_recognize(
            "Example.Show.2024.S03E02.1080p.mkv"
        )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertNotIn("target_season_year_evidence", result.metadata)

    def test_low_score_unique_tv_episode_emits_verified_automatic_identity_proof(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 9001, "name": "Example Show", "first_air_date": "2024-01-01",
            "media_type": "tv",
        }], {
            "9001": {"seasons": [{"season_number": 1, "episode_count": 12}]},
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "strict"
        breakdown = self._controlled_breakdown(
            scraper_module, 0.85, "Example Show"
        )

        with patch("app.modules.scraper.score_candidate", return_value=breakdown):
            result = tmdb.deterministic_recognize(
                "Example.Show.S01E03.1080p.mkv"
            )

        proof = scraper_module.verified_automatic_identity_proof(result)
        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertEqual(result.threshold, 0.9)
        self.assertIsNotNone(proof)
        self.assertEqual(proof["external_id"], "9001")
        self.assertEqual(proof["source_position"], {"season": 1, "episode": 3})
        self.assertEqual(proof["target_position"], {"season": 1, "episode": 3})
        self.assertEqual(proof["position_validation"]["reason"], "episode_verified")
        self.assertEqual(client.detail_calls, [("9001", "tv")])

    def test_low_score_bare_episode_in_loose_mode_emits_s01_bound_proof(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 9001, "name": "Example Show", "first_air_date": "2024-01-01",
            "media_type": "tv",
        }], {
            "9001": {"seasons": [{"season_number": 1, "episode_count": 12}]},
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "loose"
        breakdown = self._controlled_breakdown(
            scraper_module, 0.85, "Example Show"
        )

        with patch("app.modules.scraper.score_candidate", return_value=breakdown):
            result = tmdb.deterministic_recognize(
                "Example Show - 03 [1080p].mkv"
            )

        proof = scraper_module.verified_automatic_identity_proof(result)
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.threshold, 0.6)
        self.assertIsNotNone(proof)
        self.assertEqual(proof["recognition_threshold"], 0.6)
        self.assertEqual(proof["source_position"], {"season": None, "episode": 3})
        self.assertEqual(proof["target_position"], {"season": 1, "episode": 3})
        self.assertEqual(proof["position_validation"]["reason"], "episode_verified")

    def test_low_score_tv_episode_without_valid_tmdb_position_has_no_proof(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 9001, "name": "Example Show", "first_air_date": "2024-01-01",
            "media_type": "tv",
        }], {
            "9001": {"seasons": [{"season_number": 1, "episode_count": 2}]},
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "strict"
        breakdown = self._controlled_breakdown(
            scraper_module, 0.85, "Example Show"
        )

        with patch("app.modules.scraper.score_candidate", return_value=breakdown):
            result = tmdb.deterministic_recognize(
                "Example.Show.S01E03.1080p.mkv"
            )

        self.assertIsNone(scraper_module.verified_automatic_identity_proof(result))
        self.assertIn(
            "tmdb_position_episode_out_of_range", result.rejected_constraints
        )

    def test_low_score_nearby_tv_candidates_do_not_emit_automatic_proof(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([
            {
                "id": 9001, "name": "Example Show",
                "first_air_date": "2024-01-01", "media_type": "tv",
            },
            {
                "id": 9002, "name": "Example Show Alternative",
                "first_air_date": "2024-01-01", "media_type": "tv",
            },
        ], {
            "9001": {"seasons": [{"season_number": 1, "episode_count": 12}]},
            "9002": {"seasons": [{"season_number": 1, "episode_count": 12}]},
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "strict"

        def controlled_score(_context, raw):
            score = 0.85 if str(raw.get("id")) == "9001" else 0.80
            return self._controlled_breakdown(
                scraper_module, score, str(raw.get("name") or "")
            )

        with patch("app.modules.scraper.score_candidate", side_effect=controlled_score):
            result = tmdb.deterministic_recognize(
                "Example.Show.S01E03.1080p.mkv"
            )

        self.assertEqual(result.status, "low_confidence")
        self.assertIsNone(scraper_module.verified_automatic_identity_proof(result))
        # 候选详情可能已用于别名补全；但近邻候选不得触发强证据放行。

    def test_low_score_movie_never_emits_episode_identity_proof(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 530915, "title": "Example Movie",
            "release_date": "2024-01-01", "media_type": "movie",
        }])
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "strict"
        breakdown = self._controlled_breakdown(
            scraper_module, 0.85, "Example Movie"
        )

        with patch("app.modules.scraper.score_candidate", return_value=breakdown):
            result = tmdb.deterministic_recognize(
                "Example.Movie.2024.1080p.mkv", media_type_hint="movie"
            )

        self.assertEqual(result.status, "low_confidence")
        self.assertIsNone(scraper_module.verified_automatic_identity_proof(result))
        # 电影详情可能用于别名补全，但绝不能生成剧集位置强证据。

    def test_tv_position_validation_accepts_existing_episode_and_reuses_detail(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 9001, "name": "Example Show", "first_air_date": "2024-01-01",
            "media_type": "tv",
        }], {
            "9001": {"seasons": [{"season_number": 2, "episode_count": 12}]},
        })
        tmdb = scraper_module.TMDBScraper(client=client)

        first = tmdb.deterministic_recognize("Example.Show.S02E03.1080p.mkv")
        second = tmdb.deterministic_recognize("Example.Show.S02E04.1080p.mkv")

        self.assertEqual((first.status, second.status), ("matched", "matched"))
        self.assertEqual(client.detail_calls, [("9001", "tv")])

    @staticmethod
    def _merged_cour_season_detail():
        episodes = []
        first_start = date(2026, 1, 11)
        second_start = date(2026, 7, 5)
        for number in range(1, 26):
            aired_on = (
                first_start + timedelta(days=7 * (number - 1))
                if number <= 12
                else second_start + timedelta(days=7 * (number - 13))
            )
            episodes.append({
                "episode_number": number,
                "air_date": aired_on.isoformat(),
            })
        return {"season_number": 1, "episodes": episodes}

    def test_strict_merged_cour_maps_publisher_s02_into_single_tmdb_season(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 9001, "name": "Merged Cour Show",
            "first_air_date": "2026-01-11", "media_type": "tv",
        }], {
            "9001": {
                "id": 9001,
                "seasons": [{"season_number": 1, "episode_count": 25}],
            },
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "strict"

        with patch.object(
            tmdb, "get_tv_season_detail",
            return_value=self._merged_cour_season_detail(),
        ) as get_season_detail:
            result = tmdb.deterministic_recognize(
                "Merged.Cour.Show.S02E06.1080p.mkv"
            )

        self.assertEqual((result.status, result.need_confirm), ("matched", False))
        self.assertEqual(
            (result.tmdb_id, result.provider, result.external_id),
            ("9001", "tmdb", "9001"),
        )
        mapping = result.metadata["episode_mapping"]
        self.assertEqual(
            (mapping["source_season"], mapping["source_episode"]), (2, 6)
        )
        self.assertEqual(
            (mapping["target_season"], mapping["target_episode"]), (1, 18)
        )
        self.assertEqual(mapping["mode"], "absolute")
        self.assertEqual(
            mapping["reason"], "publisher_cour_mapped_to_merged_tmdb_season"
        )
        self.assertEqual((mapping["range_start"], mapping["range_end"]), (13, 25))
        get_season_detail.assert_called_once_with("9001", 1)

    def test_low_score_merged_cour_can_map_only_after_verified_precheck(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 9001, "name": "Merged Cour Show",
            "first_air_date": "2026-01-11", "media_type": "tv",
        }], {
            "9001": {
                "id": 9001,
                "seasons": [{"season_number": 1, "episode_count": 25}],
            },
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "strict"
        breakdown = self._controlled_breakdown(
            scraper_module, 0.89, "Merged Cour Show"
        )

        with (
            patch.object(scraper_module, "score_candidate", return_value=breakdown),
            patch.object(
                tmdb, "get_tv_season_detail",
                return_value=self._merged_cour_season_detail(),
            ) as get_season_detail,
        ):
            result = tmdb.deterministic_recognize(
                "Merged.Cour.Show.S02E06.1080p.mkv"
            )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertEqual(result.confidence, 0.89)
        mapping = result.metadata["episode_mapping"]
        self.assertEqual(
            (mapping["target_season"], mapping["target_episode"]), (1, 18)
        )
        proof = scraper_module.verified_automatic_identity_proof(result)
        self.assertIsNotNone(proof)
        self.assertEqual(proof["target_position"], {"season": 1, "episode": 18})
        get_season_detail.assert_called_once_with("9001", 1)

    def test_merged_cour_below_verified_precheck_floor_stays_manual(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 9001, "name": "Merged Cour Show",
            "first_air_date": "2026-01-11", "media_type": "tv",
        }], {
            "9001": {
                "id": 9001,
                "seasons": [{"season_number": 1, "episode_count": 25}],
            },
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "strict"
        breakdown = self._controlled_breakdown(
            scraper_module, 0.81, "Merged Cour Show"
        )

        with (
            patch.object(scraper_module, "score_candidate", return_value=breakdown),
            patch.object(tmdb, "get_tv_season_detail") as get_season_detail,
        ):
            result = tmdb.deterministic_recognize(
                "Merged.Cour.Show.S02E06.1080p.mkv"
            )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertEqual(result.confidence, 0.81)
        self.assertNotIn("episode_mapping", result.metadata)
        self.assertIsNone(scraper_module.verified_automatic_identity_proof(result))
        get_season_detail.assert_not_called()

    def test_loose_merged_cour_can_map_when_candidate_meets_strict_score(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 9001, "name": "Merged Cour Show",
            "first_air_date": "2026-01-11", "media_type": "tv",
        }], {
            "9001": {
                "id": 9001,
                "seasons": [{"season_number": 1, "episode_count": 25}],
            },
        })
        tmdb = scraper_module.TMDBScraper(client=client)
        tmdb.match_mode = "loose"

        with patch.object(
            tmdb, "get_tv_season_detail",
            return_value=self._merged_cour_season_detail(),
        ):
            result = tmdb.deterministic_recognize(
                "Merged.Cour.Show.S02E06.1080p.mkv"
            )

        self.assertEqual((result.status, result.need_confirm), ("matched", False))
        mapping = result.metadata["episode_mapping"]
        self.assertEqual(
            (mapping["target_season"], mapping["target_episode"]), (1, 18)
        )
        self.assertEqual(
            mapping["reason"], "publisher_cour_mapped_to_merged_tmdb_season"
        )

    def test_movie_match_does_not_fetch_detail_for_position_validation(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 530915, "title": "1917", "release_date": "2019-12-25",
            "media_type": "movie",
        }])

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(
            "1917.2019.1080p.BluRay.mkv"
        )

        self.assertEqual(result.status, "matched")
        self.assertEqual(client.detail_calls, [])

    def test_real_world_arifureta_season_three_episode_sixteen_matches_correct_series(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([
            {
                "id": 213306, "name": "神探追缉令", "first_air_date": "2024-01-01",
                "media_type": "tv",
            },
            {
                "id": 86034, "name": "平凡职业造就世界最强",
                "original_name": "ありふれた職業で世界最強",
                "first_air_date": "2019-07-08", "media_type": "tv",
            },
        ], {
            "86034": {
                "alternative_titles": {"results": [{
                    "title": "Arifureta Shokugyou de Sekai Saikyou",
                }]},
                "seasons": [{"season_number": 3, "episode_count": 16}],
            },
        })
        tmdb = scraper_module.TMDBScraper(client=client)

        chinese = tmdb.deterministic_recognize(
            "平凡职业造就世界最强 第三季.EP16.1080p.CR.WEBRip.x264.AAC.CHS-LxyLab.mkv"
        )
        romaji = tmdb.deterministic_recognize(
            "Arifureta Shokugyou de Sekai Saikyou Season 3 - 16.mkv",
            "/动漫/[H-Enc] Arifureta Shokugyou de Sekai Saikyou Season 3",
        )

        self.assertEqual((chinese.tmdb_id, romaji.tmdb_id), ("86034", "86034"))
        self.assertEqual((chinese.status, romaji.status), ("matched", "matched"))
        self.assertEqual((chinese.context.season, chinese.context.episode), (3, 16))
        self.assertEqual((romaji.context.season, romaji.context.episode), (3, 16))

    def test_real_world_noisy_ani_title_never_auto_matches_30_days(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([{
            "id": 1543, "name": "30天", "first_air_date": "2005-06-15",
            "media_type": "tv",
        }], {
            "1543": {"seasons": [{"season_number": 1, "episode_count": 6}]},
        })
        filename = (
            "[ANi] 被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生（仅限港澳台地区） "
            "- 01 [1080P][Bilibili][WEB-DL][AAC AVC][CHT CHS][MP4].mp4"
        )

        result = scraper_module.TMDBScraper(client=client).deterministic_recognize(filename)

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        self.assertNotEqual(result.tmdb_id if result.status == "matched" else "", "1543")

    def test_low_confidence_stays_manual_and_does_not_create_automatic_lock(self):
        scraper_module = self.recognition_module()
        client = _DeterministicClient([
            {
                "id": 20, "title": "完全不同的电影", "release_date": "1999-01-01",
                "media_type": "movie",
            },
        ])
        tmdb = scraper_module.TMDBScraper(client=client)

        with patch.object(tmdb, "_set_lock") as set_lock:
            result = tmdb.deterministic_recognize("目标电影.2024.mkv")

        self.assertTrue(result.need_confirm)
        self.assertEqual(result.status, "low_confidence")
        self.assertFalse(result.threshold_decision["passed"])
        set_lock.assert_not_called()

    def test_match_preserves_precedence_and_uses_deterministic_pipeline_only_as_fallback(self):
        scraper_module = self.recognition_module()
        expected = scraper_module.RecognitionResult(
            tmdb_id="30", title="搜索结果", year="2024", media_type="movie",
            confidence=0.95, status="matched", matched_by="search",
        )
        tmdb = scraper_module.TMDBScraper(client=_DeterministicClient([]))

        with patch.object(tmdb, "_get_lock", return_value=None), patch(
            "app.modules.tmdb_regex_rules.find_tmdb_regex_match", return_value=None
        ), patch.object(tmdb, "deterministic_recognize", return_value=expected) as recognize:
            result = tmdb.match("Fallback.Movie.2024.mkv", "/电影/Fallback Movie (2024)")

        self.assertIs(result, expected)
        recognize.assert_called_once()
        self.assertEqual(
            recognize.call_args.args,
            ("Fallback.Movie.2024.mkv", "/电影/Fallback Movie (2024)"),
        )
        source_context = recognize.call_args.kwargs["source_context"]
        self.assertEqual(source_context.filename_title, "Fallback Movie")


class RecognitionPreviewContractTests(RecognitionContractMixin, unittest.TestCase):
    def test_api_accepts_parent_path_and_returns_structured_safe_diagnostics(self):
        scraper_module = self.recognition_module()
        context = scraper_module.RecognitionContext(
            filename="Show.S01E01.mkv",
            parent_path="/剧集/Show (2024)/Season 01",
            normalized_title="Show",
            filename_title="Show",
            filename_year="",
            folder_title="Show",
            folder_year="2024",
            media_type="tv",
            season=1,
            episode=1,
            title_variants=["Show"],
            cleaned_components={"release_prefixes": [], "checksums": [], "release_groups": []},
        )
        breakdown = scraper_module.CandidateScoreBreakdown(
            title_score=1.0,
            original_title_score=0.8,
            alias_score=0.0,
            year_score=1.0,
            year_penalty=0.0,
            media_type_score=1.0,
            constraint_penalty=0.0,
            final_score=0.96,
            matched_title="Show",
            matched_query="Show",
            rejected_constraints=[],
        )
        result = scraper_module.RecognitionResult(
            tmdb_id="40", title="Show", year="2024", media_type="tv",
            confidence=0.96, status="matched", matched_by="search",
            threshold=0.9,
            candidates=[scraper_module.Candidate(
                "40", "Show", "2024", 0.96, "tv", score_breakdown=breakdown,
            )],
            context=context,
            query_variants=["Show"],
            threshold_decision={"threshold": 0.9, "score": 0.96, "passed": True, "reason": "score_met"},
            rejected_constraints=[],
        )

        class PreviewScraper:
            match_mode = "strict"

            def parse_media(self, filename, parent_path="", match=None):
                from tests.support import release_parse_result
                return release_parse_result(
                    {"title": "Show", "year": "", "type": "tv", "season": 1, "episode": 1},
                    filename=filename, parent_path=parent_path,
                )

            def parse_resource_tags(self, filename):
                return {}

            def match(self, filename, parent_path=""):
                self.received = (filename, parent_path)
                return result

            def get_detail(self, tmdb_id, media_type):
                return {"id": 40, "name": "Show", "first_air_date": "2024-01-01"}

        preview = PreviewScraper()
        from app.routes import tools_api
        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api, "TMDBScraper", return_value=preview
        ):
            response = tools_api.scrape_preview(Mock(), {
                "filename": "Show.S01E01.mkv",
                "parent_path": "/剧集/Show (2024)/Season 01",
            })

        payload = json.loads(response.body)
        self.assertEqual(preview.received[1], "/剧集/Show (2024)/Season 01")
        self.assertEqual(payload["parent_path"], preview.received[1])
        self.assertEqual(payload["recognition"]["folder_context"]["year"], "2024")
        self.assertEqual(payload["recognition"]["query_variants"], ["Show"])
        self.assertEqual(payload["candidates"][0]["score_breakdown"]["final_score"], 0.96)
        self.assertEqual(
            payload["candidates"][0]["score_breakdown"]["matched_query"], "Show"
        )
        self.assertEqual(payload["recognition"]["threshold_decision"]["reason"], "score_met")
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("test-key", serialized)
        self.assertNotIn("signed_url", serialized)



if __name__ == "__main__":
    unittest.main()
