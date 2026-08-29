"""成人内容番号识别与 MetaTube 整理链路聚焦回归测试。"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.directory_media import DirectoryInspection, MediaSnapshot
from app.modules.directory_scrape import DirectoryScrapeService, FixedMatchScraper
from app.modules.directory_scrape_errors import DirectoryScrapeRequestError
from app.modules.local_media_service import LocalMediaService
from app.modules.organize_correction import OrganizeCorrectionService
from app.modules.naming import build_context
from app.modules.nsfw import (
    MetaTubeClient,
    MetaTubeError,
    MetaTubeMetadata,
    NsfwRecognizer,
    build_clean_title_candidate,
    clear_nsfw_cache,
    clean_nsfw_archive_title,
    clean_nsfw_release_text,
    extract_nsfw_identifier,
    extract_nsfw_multipart,
    extract_nsfw_part_index,
    normalize_code,
    validate_category_name,
)
from app.modules.organize import (
    OrganizePlan,
    OrganizeRules,
    Organizer,
    organize_rules_snapshot,
    organize_rules_snapshot_matches,
    restore_organize_rules_snapshot,
)
from app.modules.scraper import Candidate, MatchResult, TMDBScraper
from app.routes.api import _validate_nsfw_organize_updates
from tests.support import IsolatedDatabaseTestCase


class OrganizeRulesSnapshotTests(unittest.TestCase):
    def test_snapshot_excludes_server_secret_and_restore_uses_trusted_value(self):
        rules = OrganizeRules(
            target_dir_id="archive", nsfw_enabled=True, nsfw_exclusive=True,
            nsfw_metatube_token="server-secret", nsfw_category_name="成人内容",
        )

        snapshot = organize_rules_snapshot(rules)

        self.assertNotIn("nsfw_metatube_token", snapshot)
        self.assertNotIn("server-secret", str(snapshot))
        legacy_or_tampered = {**snapshot, "nsfw_metatube_token": "attacker-secret"}
        restored = restore_organize_rules_snapshot(
            legacy_or_tampered, trusted_rules=rules,
        )
        self.assertEqual(restored.nsfw_metatube_token, "server-secret")
        self.assertTrue(organize_rules_snapshot_matches(legacy_or_tampered, rules))
        changed = OrganizeRules(**{
            **rules.__dict__, "nsfw_category_name": "其他成人分类",
        })
        self.assertFalse(organize_rules_snapshot_matches(snapshot, changed))


class FakeResponse:
    def __init__(self, payload, *, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class NsfwIdentifierTests(unittest.TestCase):
    def test_display_title_does_not_repeat_existing_number_variants(self):
        cases = (
            ("SSIS-001", "SSIS-001 测试标题"),
            ("SSIS-001", "【SSIS001】测试标题"),
            ("SSIS-001", "Best Selection SSIS-001 Special"),
            ("FC2-PPV-123456", "FC2PPV-123456 测试标题"),
            ("1PONDO-012324-001", "012324_001 一本道测试"),
        )
        for number, title in cases:
            with self.subTest(number=number, title=title):
                metadata = MetaTubeMetadata(provider="test", external_id=number, number=number, title=title)
                self.assertEqual(metadata.display_title, title)

    def test_display_title_adds_number_when_title_does_not_contain_it(self):
        metadata = MetaTubeMetadata(provider="test", external_id="ssis001", number="SSIS-001", title="测试标题")
        self.assertEqual(metadata.display_title, "SSIS-001 测试标题")

    def test_extracts_common_identifiers_and_normalizes_variants(self):
        cases = {
            "www.example.com FC2 PPV 1234567 1080p.mp4": "FC2-PPV-1234567",
            "HEYZO_1234_HDR.mkv": "HEYZO-1234",
            "1pondo-012324_001.mp4": "1PONDO-012324-001",
            "ABP 123 x265.mkv": "ABP-123",
            "ssis123.mp4": "SSIS-123",
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                identifier = extract_nsfw_identifier(filename)
                self.assertIsNotNone(identifier)
                self.assertEqual(identifier.code, expected)

    def test_codec_and_episode_tokens_are_not_treated_as_identifiers(self):
        for filename in (
            "Movie.2026.2160p.H265.DDP5.1.HDR10.mkv",
            "Dune.2021.2160p.BluRay.mkv",
            "Alien-1979-Remux.mkv",
            "Anime.S01E03.1080p.HEVC.AAC.mkv",
            "WEB-DL.x264.AV1.mp4",
        ):
            with self.subTest(filename=filename):
                self.assertIsNone(extract_nsfw_identifier(filename))

    def test_configured_domains_are_removed_before_identifier_detection(self):
        cleaned = clean_nsfw_release_text(
            "ads.example.tv/path SSIS-001 1080p", "ads.example.tv"
        )
        self.assertNotIn("example", cleaned.lower())
        self.assertEqual(extract_nsfw_identifier(cleaned).code, "SSIS-001")

    def test_category_is_a_single_safe_path_segment(self):
        self.assertEqual(validate_category_name("成人内容"), "成人内容")
        for value in ("../adult", "成人/内容", "", "a" * 41):
            with self.subTest(value=value):
                if value == "":
                    self.assertEqual(validate_category_name(value), "成人内容")
                else:
                    with self.assertRaises(ValueError):
                        validate_category_name(value)


class MetaTubeClientTests(unittest.TestCase):
    def setUp(self):
        clear_nsfw_cache()

    @staticmethod
    def _item(number="SSIS-001", provider="javbus", item_id="ssis001"):
        return {
            "provider": provider,
            "id": item_id,
            "number": number,
            "title": "测试标题",
            "release_date": "2024-01-02T00:00:00Z",
            "actors": ["演员甲"],
            "genres": ["剧情"],
            "cover_url": "https://img.example/cover.jpg",
        }

    def test_search_uses_bearer_timeout_and_keeps_only_exact_number(self):
        session = FakeSession(FakeResponse({"data": [
            self._item(), self._item(number="SSIS-002", item_id="ssis002"),
        ], "error": None}))
        client = MetaTubeClient(
            "http://127.0.0.1:8080/", "secret", timeout=7, session=session
        )
        result = client.search_exact("ssis 001")

        self.assertEqual([item.number for item in result], ["SSIS-001"])
        url, kwargs = session.calls[0]
        self.assertEqual(url, "http://127.0.0.1:8080/v1/movies/search")
        self.assertEqual(kwargs["params"]["q"], "SSIS-001")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(kwargs["timeout"], 7)

    def test_timeout_and_invalid_payload_are_recoverable_errors(self):
        timeout_client = MetaTubeClient(
            "http://127.0.0.1:8080", session=FakeSession(requests.Timeout())
        )
        with self.assertRaisesRegex(MetaTubeError, "超时"):
            timeout_client.search_exact("SSIS-001")

        invalid_client = MetaTubeClient(
            "http://127.0.0.1:8080", session=FakeSession(FakeResponse([]))
        )
        with self.assertRaisesRegex(MetaTubeError, "格式"):
            invalid_client.search_exact("SSIS-001")

        redirect_client = MetaTubeClient(
            "http://127.0.0.1:8080",
            session=FakeSession(FakeResponse({}, status_code=302)),
        )
        with self.assertRaisesRegex(MetaTubeError, "重定向"):
            redirect_client.search_exact("SSIS-001")

    def test_recognizer_cache_avoids_duplicate_requests(self):
        session = FakeSession(FakeResponse({"data": [self._item()], "error": None}))
        recognizer = NsfwRecognizer("http://127.0.0.1:8080", session=session)
        first = recognizer.match("SSIS-001.mp4")
        second = recognizer.match("SSIS-001.1080p.mp4")
        self.assertEqual(first.external_id, "javbus:ssis001")
        self.assertEqual(second.external_id, first.external_id)
        self.assertEqual(len(session.calls), 1)

    def test_malformed_optional_metadata_is_safely_normalized(self):
        item = self._item()
        item.update({"score": "N/A", "actors": {"bad": "shape"}, "genres": "剧情"})
        client = MetaTubeClient(
            "http://127.0.0.1:8080",
            session=FakeSession(FakeResponse({"data": [item], "error": None})),
        )
        result = client.search_exact("SSIS-001")
        self.assertEqual(result[0].score, 0.0)
        self.assertEqual(result[0].actors, ())
        self.assertEqual(result[0].genres, ("剧情",))

    def test_concurrent_exact_lookup_is_single_flight(self):
        class SlowSession(FakeSession):
            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                time.sleep(0.08)
                return FakeResponse({"data": [MetaTubeClientTests._item()], "error": None})

        session = SlowSession()
        recognizer = NsfwRecognizer("http://127.0.0.1:8080", session=session)
        barrier = threading.Barrier(4)
        results = []

        def run():
            barrier.wait()
            results.append(recognizer.match("SSIS-001.mp4"))

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(item and item.external_id == "javbus:ssis001" for item in results))
        self.assertEqual(len(session.calls), 1)

    def test_equivalent_exact_providers_are_collapsed_and_auto_selected(self):
        sparse = self._item(provider="javdb", item_id="internal-1")
        sparse.update({
            "title": "SKMJ-749 現役保育士さん＆ふんわりおっぱい",
            "number": "SKMJ-749",
            "release_date": "2026-05-07T00:00:00Z",
            "actors": [],
            "genres": [],
            "cover_url": "",
        })
        rich = self._item(provider="javbus", item_id="SKMJ-749")
        rich.update({
            "title": "SKMJ-749 現役保育士さん&ふんわりおっぱい",
            "number": "SKMJ-749",
            "release_date": "2026-05-07T00:00:00Z",
            "actors": ["演员甲", "演员乙"],
            "genres": ["剧情", "单体作品"],
            "summary": "完整简介",
        })
        jav321 = dict(sparse)
        jav321.update({
            "provider": "jav321", "id": "h_1324skmj00749",
            "title": "SKMJ-749 現役保育士さん＆ふんわりおっぱい",
        })
        duga = dict(sparse)
        duga.update({
            "provider": "duga", "id": "sekimen-0747",
            "title": "SKMJ-749 現役保育士さん&ふんわりおっぱい",
        })
        session = FakeSession(FakeResponse({
            "data": [duga, jav321, sparse, rich], "error": None,
        }))

        match = NsfwRecognizer(
            "http://127.0.0.1:8080", session=session,
        ).match("SKMJ-749.mp4")

        self.assertFalse(match.need_confirm)
        self.assertEqual(match.status, "matched")
        self.assertEqual(len(match.candidates), 1)
        self.assertEqual(match.external_id, "javbus:SKMJ-749")
        self.assertEqual(match.matched_by, "metatube_equivalent_sources")
        self.assertEqual(
            match.metadata["mediaflux_equivalent_source_count"], 4,
        )

    def test_exact_same_number_with_different_titles_is_auto_selected(self):
        first = self._item(provider="javbus", item_id="SSIS-001")
        second = self._item(provider="javdb", item_id="internal-b")
        first["title"] = "完全不同的宣传标题甲"
        second["title"] = "另一来源编写的标题乙"
        session = FakeSession(FakeResponse({"data": [first, second], "error": None}))

        match = NsfwRecognizer(
            "http://127.0.0.1:8080", session=session,
        ).match("SSIS-001.mp4")

        self.assertFalse(match.need_confirm)
        self.assertEqual(match.status, "matched")
        self.assertEqual(len(match.candidates), 1)
        self.assertEqual(match.external_id, "javbus:SSIS-001")
        self.assertEqual(match.matched_by, "metatube_equivalent_sources")
        self.assertEqual(match.metadata["mediaflux_equivalent_source_count"], 2)

    def test_fjin_140_provider_titles_and_dates_are_collapsed_by_exact_number(self):
        jav321 = self._item(
            number="FJIN-140", provider="JAV321", item_id="fjin00140a",
        )
        jav321.update({
            "title": (
                "FJIN-140 【抵抗虚しく幽閉姦】敵対組織に囚われ肉オナホ監禁された"
                "金髪捜査官 メロディー・雛・マークス"
            ),
            "release_date": "2026-04-15T00:00:00Z",
            "actors": ["メロディー・雛・マークス"],
        })
        javbus = self._item(
            number="FJIN-140", provider="JavBus", item_id="FJIN-140",
        )
        javbus.update({
            "title": (
                "FJIN-140 【ぬるぬる蠢く触手監獄に閉じ込められ…】触手組織に囚われ"
                "肉オナホ監禁された金髪捜査官 メロディー・雛・マークス"
            ),
            "release_date": "2026-04-14T00:00:00Z",
            "actors": ["メロディー・雛・マークス"],
        })
        dated_javbus = self._item(
            number="FJIN-140", provider="JavBus",
            item_id="FJIN-140_2026-05-01",
        )
        dated_javbus.update({
            "title": (
                "FJIN-140 触手組織に囚われ肉オナホ監禁された金髪捜査官 "
                "メロディー・雛・マークス"
            ),
            "release_date": "2026-05-01T00:00:00Z",
            "actors": [],
        })
        session = FakeSession(FakeResponse({
            "data": [jav321, javbus, dated_javbus], "error": None,
        }))

        match = NsfwRecognizer(
            "http://127.0.0.1:8080", session=session,
        ).match("hhd800.com@FJIN-140.mp4")

        self.assertFalse(match.need_confirm)
        self.assertEqual(match.status, "matched")
        self.assertEqual(len(match.candidates), 1)
        self.assertEqual(match.external_id, "JavBus:FJIN-140")
        self.assertEqual(match.metadata["mediaflux_equivalent_source_count"], 3)

    def test_same_title_with_different_release_year_remains_ambiguous(self):
        first = self._item(provider="javbus", item_id="a")
        second = self._item(provider="javdb", item_id="b")
        first["release_date"] = "2024-01-02"
        second["release_date"] = "2025-01-02"
        session = FakeSession(FakeResponse({"data": [first, second], "error": None}))

        match = NsfwRecognizer(
            "http://127.0.0.1:8080", session=session,
        ).match("SSIS-001.mp4")

        self.assertTrue(match.need_confirm)
        self.assertEqual(len(match.candidates), 2)


class NsfwMultipartAndFallbackTests(unittest.TestCase):
    def test_short_s_prefix_number_is_not_mistaken_for_episode_marker(self):
        identifier = extract_nsfw_identifier("SW-1047.mp4")
        self.assertIsNotNone(identifier)
        self.assertEqual(identifier.code, "SW-1047")
        self.assertEqual(
            build_clean_title_candidate("SW-1047.mp4")["external_id"],
            "SW-1047",
        )
        self.assertIsNone(extract_nsfw_identifier("S-001.mp4"))
        self.assertIsNone(extract_nsfw_identifier("E-001.mp4"))

    def test_multipart_recognizes_numeric_and_label_suffixes(self):
        numeric = extract_nsfw_multipart("hhd800.com@FJIN-140-1.mp4")
        labelled = extract_nsfw_multipart("FJIN-140.CD2.1080p.mkv")
        ambiguous = extract_nsfw_multipart("FJIN-140-A.mp4")

        self.assertEqual(numeric.part_index, 1)
        self.assertEqual(labelled.part_index, 2)
        self.assertTrue(ambiguous.ambiguous)
        self.assertIsNone(ambiguous.part_index)
        self.assertEqual(extract_nsfw_part_index("FJIN-140.CD2.mkv"), 2)

    def test_clean_title_removes_site_noise_and_keeps_safe_number(self):
        self.assertEqual(
            clean_nsfw_archive_title("hhd800.com@ATID-675.mp4"),
            "ATID-675",
        )
        self.assertEqual(
            clean_nsfw_archive_title("SSIS-001.2024.1080p.mp4"),
            "SSIS-001",
        )
        candidate = build_clean_title_candidate("hhd800.com@ATID-675.mp4")
        self.assertEqual(candidate["provider"], "clean_title")
        self.assertEqual(candidate["external_id"], "ATID-675")
        self.assertEqual(candidate["title"], "ATID-675")

    def test_adult_part_marker_is_inserted_without_affecting_tmdb(self):
        organizer = Organizer(client=object(), scraper=TMDBScraper())
        file = GuangYaFile("f1", "FJIN-140-1.mp4", False, 1, "e", "p")
        rules = OrganizeRules(naming_scope="both")
        adult = MatchResult(
            title="FJIN-140", media_type="movie", provider="clean_title",
            external_id="FJIN-140",
        )
        ordinary = MatchResult(
            tmdb_id="1", title="普通电影", media_type="movie", provider="tmdb",
        )

        self.assertEqual(
            organizer.build_new_name(adult, file, {"part": 1}, rules),
            "FJIN-140.CD1.mp4",
        )
        self.assertNotIn(".CD1.", organizer.build_new_name(
            ordinary, file, {"part": 1}, rules,
        ))

    def test_ambiguous_multipart_group_requires_manual_confirmation(self):
        organizer = Organizer(client=object(), scraper=TMDBScraper())
        match = MatchResult(
            title="FJIN-140", media_type="movie", provider="metatube",
            external_id="javbus:FJIN-140",
        )
        plans = [
            OrganizePlan(
                file_id=f"f{index}", original_name=f"FJIN-140-{letter}.mp4",
                original_path="FJIN-140", match=MatchResult(**match.__dict__),
            )
            for index, letter in enumerate(("A", "B"), 1)
        ]
        rules = OrganizeRules(nsfw_enabled=True, nsfw_exclusive=True)

        organizer._apply_nsfw_multipart_policy(plans, rules)

        self.assertTrue(all(plan.action == "skip" for plan in plans))
        self.assertTrue(all(plan.match.need_confirm for plan in plans))
        self.assertTrue(all(plan.multipart_ambiguous for plan in plans))
        self.assertTrue(all(
            plan.match.metadata.get("nsfw_multipart_confirmation") for plan in plans
        ))

    def test_explicit_multipart_group_keeps_automatic_plans(self):
        organizer = Organizer(client=object(), scraper=TMDBScraper())
        plans = [
            OrganizePlan(
                file_id=f"f{index}", original_name=f"FJIN-140-{index}.mp4",
                original_path="FJIN-140",
                match=MatchResult(
                    title="FJIN-140", media_type="movie", provider="metatube",
                    external_id="javbus:FJIN-140",
                ),
            )
            for index in (1, 2)
        ]
        organizer._apply_nsfw_multipart_policy(
            plans, OrganizeRules(nsfw_enabled=True, nsfw_exclusive=True),
        )
        self.assertTrue(all(plan.action == "move" for plan in plans))
        self.assertEqual([plan.multipart_index for plan in plans], [1, 2])

    def test_no_metadata_confirmation_group_gets_clean_title_candidate(self):
        organizer = Organizer(client=object(), scraper=TMDBScraper())
        match = MatchResult(
            media_type="movie", provider="metatube", need_confirm=True,
            error="MetaTube 没有返回完全一致的结果",
        )
        plan = OrganizePlan(
            file_id="f1", original_name="hhd800.com@ATID-675.mp4",
            original_path="ATID-675", original_parent_id="p",
            match=match, action="skip",
        )
        groups = organizer._build_confirmation_groups(
            [plan], {}, source_dir_id="adult", source_name="NSFW",
            rules=OrganizeRules(nsfw_enabled=True, nsfw_exclusive=True),
        )
        self.assertEqual(groups[0]["candidates"][0]["provider"], "clean_title")
        self.assertEqual(groups[0]["candidates"][0]["title"], "ATID-675")
        validated, actionable = Organizer._validated_task_confirmation_groups({
            "confirmation_groups": groups,
        })
        self.assertEqual(validated[0]["candidates"][0]["provider"], "clean_title")
        self.assertEqual(actionable, 1)

    def test_sw_number_confirmation_group_offers_clean_title_instead_of_skip_only(self):
        organizer = Organizer(client=object(), scraper=TMDBScraper())
        plan = OrganizePlan(
            file_id="sw-video", original_name="SW-1047.mp4",
            original_path="SW-1047", original_parent_id="adult-parent",
            size=1024, etag="etag-sw", action="skip",
            match=MatchResult(
                media_type="movie", provider="metatube", need_confirm=True,
                status="no_match", error="MetaTube 没有返回完全一致的结果",
            ),
        )
        groups = organizer._build_confirmation_groups(
            [plan], {}, source_dir_id="adult-source", source_name="NSFW",
            rules=OrganizeRules(nsfw_enabled=True, nsfw_exclusive=True),
        )
        validated, actionable = Organizer._validated_task_confirmation_groups({
            "confirmation_groups": groups,
        })

        self.assertEqual(actionable, 1)
        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["identity"], "SW-1047")
        self.assertEqual(validated[0]["candidates"], [{
            "provider": "clean_title",
            "external_id": "SW-1047",
            "tmdb_id": "",
            "media_type": "movie",
            "title": "SW-1047",
            "year": "",
            "score": 1.0,
            "support": 1,
            "metadata": {"number": "SW-1047", "title": "SW-1047", "fallback": True},
        }])

    def test_confirmed_clean_title_match_is_accepted_by_nsfw_organizer(self):
        match = MatchResult(
            title="ATID-675", media_type="movie", confidence=1.0,
            provider="clean_title", external_id="ATID-675",
            metadata={"number": "ATID-675", "title": "ATID-675", "fallback": True},
            locked=True,
        )
        organizer = Organizer(
            client=object(),
            scraper=FixedMatchScraper(TMDBScraper(), match, match.metadata),
        )
        file = GuangYaFile("f1", "hhd800.com@ATID-675.mp4", False, 100, "etag")

        resolved = organizer._resolve_plan_match(
            file,
            OrganizeRules(nsfw_enabled=True, nsfw_exclusive=True),
            match_name=file.name,
            parent_path="ATID-675",
            recognition_media_type_hint="movie",
            match_override=None,
            recognition_work_cache=None,
            recognition_work_cache_key=None,
        )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.provider, "clean_title")
        self.assertEqual(resolved.external_id, "ATID-675")

    def test_clean_title_identity_marker_is_recognized_by_guard(self):
        self.assertEqual(
            Organizer._identity_marker("成人内容/ATID-675 {clean_title-ATID-675}"),
            "{clean_title-atid-675}",
        )

    def test_different_numbers_in_same_directory_create_separate_groups(self):
        organizer = Organizer(client=object(), scraper=TMDBScraper())
        plans = []
        for index, code in enumerate(("ATID-675", "FJIN-140"), 1):
            plans.append(OrganizePlan(
                file_id=f"f{index}", original_name=f"{code}.mp4",
                original_path="未分类", original_parent_id="p",
                match=MatchResult(
                    media_type="movie", provider="metatube", need_confirm=True,
                    error="MetaTube 没有返回完全一致的结果",
                ),
                action="skip",
            ))
        groups = organizer._build_confirmation_groups(
            plans, {}, source_dir_id="adult", source_name="NSFW",
            rules=OrganizeRules(nsfw_enabled=True, nsfw_exclusive=True),
        )
        self.assertEqual(len(groups), 2)
        self.assertEqual(
            {group["candidates"][0]["external_id"] for group in groups},
            {"ATID-675", "FJIN-140"},
        )


class NsfwOrganizerTests(unittest.TestCase):
    def setUp(self):
        clear_nsfw_cache()

    def test_rules_enable_adult_recognition_only_for_selected_sources(self):
        rules = OrganizeRules(
            nsfw_enabled=True,
            nsfw_source_ids='["adult-source"]',
            nsfw_metatube_endpoint="http://127.0.0.1:8080",
        )

        adult = rules.for_source("adult-source")
        ordinary = rules.for_source("ordinary-source")
        local_adult = rules.for_local_source("nsfw")
        local_ordinary = rules.for_local_source("auto")

        self.assertTrue(adult.nsfw_enabled)
        self.assertTrue(adult.nsfw_exclusive)
        self.assertFalse(ordinary.nsfw_enabled)
        self.assertFalse(ordinary.nsfw_exclusive)
        self.assertTrue(local_adult.nsfw_enabled)
        self.assertTrue(local_adult.nsfw_exclusive)
        self.assertFalse(local_ordinary.nsfw_enabled)
        self.assertFalse(local_ordinary.nsfw_exclusive)

    def test_exclusive_source_does_not_fall_back_to_tmdb_when_metatube_has_no_match(self):
        scraper = TMDBScraper()
        scraper.match = MagicMock()
        organizer = Organizer(client=object(), scraper=scraper)
        recognizer = MagicMock()
        recognizer.match.return_value = None
        rules = OrganizeRules(
            nsfw_enabled=True,
            nsfw_exclusive=True,
            nsfw_metatube_endpoint="http://127.0.0.1:8080",
        )

        with patch.object(organizer, "_nsfw_recognizer", return_value=recognizer):
            match = organizer._resolve_plan_match(
                GuangYaFile("f1", "SSIS-001.mp4", False, 1, "e", "p1"),
                rules,
                match_name="SSIS-001.mp4",
                parent_path="成人源",
                recognition_media_type_hint="",
                match_override=None,
                recognition_work_cache=None,
                recognition_work_cache_key=None,
            )

        recognizer.match.assert_called_once_with("SSIS-001.mp4", "成人源")
        scraper.match.assert_not_called()
        self.assertEqual(match.provider, "metatube")
        self.assertTrue(match.need_confirm)
        self.assertIn("MetaTube", match.error)

    def test_directory_scope_inherits_selected_adult_source_from_ancestor(self):
        client = MagicMock()
        client.file_info.side_effect = {
            "child": SimpleNamespace(parent_id="adult-source"),
        }.get
        rules = OrganizeRules(
            nsfw_enabled=True,
            nsfw_source_ids='["adult-source"]',
            nsfw_metatube_endpoint="http://127.0.0.1:8080",
        )
        service = DirectoryScrapeService(client=client, rules_loader=lambda: rules)

        scoped = service._rules_for_scope("child")
        ordinary = service._rules_for_scope("ordinary")

        self.assertTrue(scoped.nsfw_enabled)
        self.assertTrue(scoped.nsfw_exclusive)
        self.assertFalse(ordinary.nsfw_enabled)
        self.assertFalse(ordinary.nsfw_exclusive)

    def test_metatube_match_uses_dedicated_category_and_provider_identity_tag(self):
        organizer = Organizer(client=object(), scraper=TMDBScraper())
        session = FakeSession(FakeResponse({"data": [MetaTubeClientTests._item()], "error": None}))
        recognizer = NsfwRecognizer("http://127.0.0.1:8080", session=session)
        match = recognizer.match("SSIS-001.1080p.mp4")
        rules = OrganizeRules(
            nsfw_enabled=True,
            nsfw_metatube_endpoint="http://127.0.0.1:8080",
            nsfw_category_name="成人内容",
        )

        self.assertEqual(organizer.classify(match, rules), ("成人内容", "其他", "2024"))
        self.assertIn("{metatube-javbus-ssis001}", organizer.build_media_dir(match, rules))
        self.assertEqual(organizer._match_identity_key(match), "metatube:javbus:ssis001")

    def test_tmdb_tag_keeps_tmdb_semantics_while_identity_tag_is_generic(self):
        context = build_context(
            title="SSIS-001", year="2024",
            identity_id="metatube:javbus:ssis001",
            identity_tag="{metatube-javbus-ssis001}",
        )
        self.assertEqual(context.tmdb_tag, "")
        self.assertEqual(context.identity_tag, "{metatube-javbus-ssis001}")

    def test_manual_metatube_preview_requires_source_number_match(self):
        inspection = DirectoryInspection(
            directory_id="d1", directory_name="SSIS-001",
            media_type="movie", suggested_query="SSIS-001",
            videos=(MediaSnapshot(
                file_id="f1", parent_id="d1", name="SSIS-001.mp4",
                size=1, etag="e", role="video", relative_dir="",
            ),),
            companions=(), counts={}, mixed=False, fingerprint="fp",
        )
        rules = OrganizeRules(nsfw_strip_domains="")
        DirectoryScrapeService._validate_metatube_source_identity(
            inspection, {"number": "SSIS-001"}, rules,
        )
        with self.assertRaisesRegex(DirectoryScrapeRequestError, "番号与当前目录不一致"):
            DirectoryScrapeService._validate_metatube_source_identity(
                inspection, {"number": "ABP-123"}, rules,
            )


class NsfwCorrectionIntegrationTests(IsolatedDatabaseTestCase):
    def _pending_log(self, *, source_dir_id: str = "adult-source") -> int:
        name = "SSIS-001.2024.1080p.mp4"
        log_id = db.add_organize_log(
            "guangya", "成人待整理/SSIS-001", "", "video", "manual", "",
            source_dir_id=source_dir_id, original_parent_id="adult-source",
            original_name=name, current_parent_id="adult-source", current_name=name,
            media_type="movie", error="MetaTube 未返回唯一精确候选",
            legacy_incomplete=False,
        )
        db.add_organize_log_items(log_id, [{
            "file_id": "video", "role": "video",
            "original_parent_id": "adult-source", "original_name": name,
            "current_parent_id": "adult-source", "current_name": name,
            "size": 1024, "etag": "etag-video", "status": "manual",
        }])
        return log_id

    @staticmethod
    def _rules() -> OrganizeRules:
        return OrganizeRules(
            target_dir_id="archive", small_file_mb=0, region_split=False,
            year_split=False, link_strm=False, nsfw_enabled=True,
            nsfw_source_ids='["adult-source"]',
            nsfw_metatube_endpoint="http://127.0.0.1:8080",
            nsfw_metatube_token="server-secret",
            nsfw_category_name="成人内容",
        )

    def test_pending_adult_log_exposes_metatube_recognition_profile(self):
        log_id = self._pending_log()
        service = OrganizeCorrectionService(client=MagicMock(), scraper=MagicMock())

        with patch(
            "app.modules.organize_correction.OrganizeRules.from_config",
            return_value=self._rules(),
        ):
            detail = service.detail(log_id)

        self.assertEqual(detail["recognition"]["provider"], "metatube")
        self.assertTrue(detail["recognition"]["nsfw_only"])
        self.assertTrue(detail["allowed_actions"]["search"])
        self.assertTrue(detail["allowed_actions"]["reorganize"])

    def test_pending_adult_search_uses_metatube_without_tmdb_fallback(self):
        log_id = self._pending_log()
        scraper = MagicMock()
        service = OrganizeCorrectionService(client=MagicMock(), scraper=scraper)
        recognizer = MagicMock()
        recognizer.candidates.return_value = [Candidate(
            tmdb_id="", title="SSIS-001 测试标题", year="2024", score=1.0,
            media_type="movie", provider="metatube",
            external_id="javbus:ssis001",
        )]

        with patch(
            "app.modules.organize_correction.OrganizeRules.from_config",
            return_value=self._rules(),
        ), patch.object(service.organizer, "_nsfw_recognizer", return_value=recognizer):
            candidates = service.search_candidates(log_id, "SSIS-001")
            with self.assertRaisesRegex(ValueError, "只允许使用 MetaTube"):
                service.search_tmdb(log_id, "SSIS-001")

        recognizer.candidates.assert_called_once_with("SSIS-001")
        scraper.search_candidates.assert_not_called()
        self.assertEqual(candidates[0]["provider"], "metatube")
        self.assertEqual(candidates[0]["external_id"], "javbus:ssis001")

    def test_pending_adult_search_falls_back_to_clean_title_candidate(self):
        log_id = self._pending_log()
        service = OrganizeCorrectionService(client=MagicMock(), scraper=MagicMock())
        recognizer = MagicMock()
        recognizer.candidates.return_value = []

        with patch(
            "app.modules.organize_correction.OrganizeRules.from_config",
            return_value=self._rules(),
        ), patch.object(service.organizer, "_nsfw_recognizer", return_value=recognizer):
            candidates = service.search_candidates(log_id, "hhd800.com@SSIS-001.mp4")

        self.assertEqual(candidates[0]["provider"], "clean_title")
        self.assertEqual(candidates[0]["external_id"], "SSIS-001")
        self.assertEqual(candidates[0]["title"], "SSIS-001")

    def test_pending_adult_preview_accepts_clean_title_candidate(self):
        log_id = self._pending_log()
        service = OrganizeCorrectionService(client=MagicMock(), scraper=TMDBScraper())

        with patch(
            "app.modules.organize_correction.OrganizeRules.from_config",
            return_value=self._rules(),
        ):
            preview = service.preview_reorganize(
                log_id, "", "movie", title="被篡改的垃圾标题",
                provider="clean_title", external_id="SSIS-001",
            )

        self.assertEqual(preview["match"]["provider"], "clean_title")
        self.assertEqual(preview["match"]["external_id"], "SSIS-001")
        self.assertEqual(preview["match"]["title"], "SSIS-001")
        self.assertIn("{clean_title-SSIS-001}", preview["target_path"])
        self.assertTrue(preview["target_path"].startswith("成人内容/"))

    def test_pending_adult_preview_resolves_metatube_and_keeps_scoped_archive(self):
        log_id = self._pending_log()
        service = OrganizeCorrectionService(client=MagicMock(), scraper=TMDBScraper())
        recognizer = MagicMock()
        detail = {
            "provider": "javbus", "id": "ssis001", "number": "SSIS-001",
            "title": "测试标题", "original_title": "测试标题",
            "release_date": "2024-01-02", "overview": "", "genres": [],
        }
        recognizer.resolve.return_value = (MatchResult(
            title="SSIS-001 测试标题", year="2024", media_type="movie",
            confidence=1.0, status="matched", matched_by="metatube_manual",
            provider="metatube", external_id="javbus:ssis001", metadata=detail,
        ), detail)

        with patch(
            "app.modules.organize_correction.OrganizeRules.from_config",
            return_value=self._rules(),
        ), patch.object(service.organizer, "_nsfw_recognizer", return_value=recognizer):
            preview = service.preview_reorganize(
                log_id, "", "movie", provider="metatube",
                external_id="javbus:ssis001",
            )

        self.assertEqual(preview["match"]["provider"], "metatube")
        self.assertEqual(preview["match"]["external_id"], "javbus:ssis001")
        self.assertTrue(preview["target_path"].startswith("成人内容/"))
        self.assertIn("{metatube-javbus-ssis001}", preview["target_path"])
        self.assertTrue(preview["rules_snapshot"]["nsfw_exclusive"])
        self.assertNotIn("nsfw_metatube_token", preview["rules_snapshot"])
        self.assertNotIn("server-secret", str(preview["rules_snapshot"]))

    def test_pending_adult_preview_rejects_different_number(self):
        log_id = self._pending_log()
        service = OrganizeCorrectionService(client=MagicMock(), scraper=TMDBScraper())
        recognizer = MagicMock()
        detail = {"number": "ABP-123"}
        recognizer.resolve.return_value = (MatchResult(
            title="ABP-123", year="2024", media_type="movie",
            provider="metatube", external_id="javbus:abp123", metadata=detail,
        ), detail)

        with patch(
            "app.modules.organize_correction.OrganizeRules.from_config",
            return_value=self._rules(),
        ), patch.object(service.organizer, "_nsfw_recognizer", return_value=recognizer), \
                self.assertRaisesRegex(ValueError, "候选番号与原文件不一致"):
            service.preview_reorganize(
                log_id, "", "movie", provider="metatube",
                external_id="javbus:abp123",
            )


class NsfwAuditIdentityTests(IsolatedDatabaseTestCase):
    def test_history_normalizes_legacy_tmdb_and_persists_provider_identity(self):
        db.add_organize_log(
            "guangya", "1", "电影/测试 (2024)", "f1", "success", "123",
            original_parent_id="1", original_name="a.mkv", media_type="movie",
        )
        db.add_organize_log(
            "guangya", "1", "成人内容/SSIS-001 (2024)", "f2", "success", "",
            provider="metatube", external_id="javbus:ssis001",
            original_parent_id="1", original_name="b.mkv", media_type="movie",
        )
        organizer = Organizer(client=object(), scraper=TMDBScraper())
        self.assertEqual(
            organizer._historical_root_identities("电影/测试 (2024)"),
            {("movie", "tmdb:123")},
        )
        self.assertEqual(
            organizer._historical_root_identities("成人内容/SSIS-001 (2024)"),
            {("movie", "metatube:javbus:ssis001")},
        )


class NsfwLocalIntegrationTests(IsolatedDatabaseTestCase):
    def test_explicit_local_adult_source_uses_metatube_and_media_library_movie_target(self):
        clear_nsfw_cache()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            target = root / "target"
            source.mkdir(); target.mkdir()
            (source / "SSIS-001.1080p.mp4").write_bytes(b"video")
            source_id = db.create_local_media_source(
                name="nsfw-source", qb_profile="", qb_path_prefix="",
                local_root=str(source), media_type="nsfw", owner="admin",
            )
            db.upsert_local_library_target(source_id, "movie", str(target), owner="admin")
            service = LocalMediaService(scraper=TMDBScraper())
            inspection = service.inspect_source("admin", source_id, source)
            recognizer = NsfwRecognizer(
                "http://127.0.0.1:8080",
                session=FakeSession(FakeResponse({"data": [MetaTubeClientTests._item()], "error": None})),
            )
            rules = OrganizeRules(
                naming_scope="both", region_split=False, year_split=False,
                nsfw_enabled=True, nsfw_metatube_endpoint="http://127.0.0.1:8080",
                nsfw_category_name="成人内容",
            )
            with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=rules), patch.object(
                service.organizer, "_nsfw_recognizer", return_value=recognizer
            ):
                preview = service.preview("admin", inspection["inspection_id"])

            self.assertEqual(preview["status"], "planned")
            self.assertEqual(preview["matches"][0]["provider"], "metatube")
            self.assertIn("{metatube-javbus-ssis001}", preview["plans"][0]["target_path"])
            destination = Path(preview["plans"][0]["target_path"])
            relative = destination.relative_to(target.resolve())
            self.assertNotIn("成人内容", relative.parts)


class NsfwConfigValidationTests(unittest.TestCase):
    def test_config_normalizes_boolean_domains_timeout_and_masked_token(self):
        with patch("app.routes.api.config.get_bool", return_value=False), patch(
            "app.routes.api.config.get", side_effect=lambda key, default="": default
        ):
            result = _validate_nsfw_organize_updates({
                "GY_ORGANIZE_NSFW_ENABLED": "0",
                "GY_ORGANIZE_NSFW_METATUBE_TOKEN": "********",
                "GY_ORGANIZE_NSFW_CATEGORY_NAME": "成人内容",
                "GY_ORGANIZE_NSFW_STRIP_DOMAINS": "WWW.Example.COM, example.com",
                "GY_ORGANIZE_NSFW_TIMEOUT_SECONDS": "8",
            })
        self.assertEqual(result["GY_ORGANIZE_NSFW_ENABLED"], "0")
        self.assertEqual(result["GY_ORGANIZE_NSFW_STRIP_DOMAINS"], "example.com")
        self.assertNotIn("GY_ORGANIZE_NSFW_METATUBE_TOKEN", result)

    def test_enabled_requires_endpoint_and_rejects_unsafe_category(self):
        with patch("app.routes.api.config.get_bool", return_value=False), patch(
            "app.routes.api.config.get", return_value=""
        ):
            with self.assertRaisesRegex(ValueError, "MetaTube 服务地址"):
                _validate_nsfw_organize_updates({"GY_ORGANIZE_NSFW_ENABLED": "1"})
        with self.assertRaises(ValueError):
            _validate_nsfw_organize_updates({"GY_ORGANIZE_NSFW_CATEGORY_NAME": "../adult"})


if __name__ == "__main__":
    unittest.main()
