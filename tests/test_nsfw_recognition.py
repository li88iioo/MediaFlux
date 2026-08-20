"""成人内容番号识别与 MetaTube 整理链路聚焦回归测试。"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from app import database as db
from app.modules.directory_media import DirectoryInspection, MediaSnapshot
from app.modules.directory_scrape import DirectoryScrapeService
from app.modules.directory_scrape_errors import DirectoryScrapeRequestError
from app.modules.local_media_service import LocalMediaService
from app.modules.naming import build_context
from app.modules.nsfw import (
    MetaTubeClient,
    MetaTubeError,
    NsfwRecognizer,
    clear_nsfw_cache,
    clean_nsfw_release_text,
    extract_nsfw_identifier,
    normalize_code,
    validate_category_name,
)
from app.modules.organize import OrganizeRules, Organizer
from app.modules.scraper import TMDBScraper
from app.routes.api import _validate_nsfw_organize_updates
from tests.support import IsolatedDatabaseTestCase


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

    def test_multiple_exact_providers_require_manual_choice(self):
        session = FakeSession(FakeResponse({"data": [
            self._item(provider="javbus", item_id="a"),
            self._item(provider="javdb", item_id="b"),
        ], "error": None}))
        match = NsfwRecognizer("http://127.0.0.1:8080", session=session).match("SSIS-001.mp4")
        self.assertTrue(match.need_confirm)
        self.assertEqual(match.status, "ambiguous")
        self.assertEqual(len(match.candidates), 2)


class NsfwOrganizerTests(unittest.TestCase):
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
    def test_default_local_automatic_preview_uses_shared_exact_nsfw_match(self):
        clear_nsfw_cache()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            target = root / "target"
            source.mkdir(); target.mkdir()
            (source / "SSIS-001.1080p.mp4").write_bytes(b"video")
            source_id = db.create_local_media_source(
                name="nsfw-source", qb_profile="", qb_path_prefix="",
                local_root=str(source), owner="admin",
            )
            db.upsert_local_library_target(source_id, "default", str(target), owner="admin")
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
            self.assertIn("成人内容", preview["plans"][0]["target_path"])
            self.assertIn("{metatube-javbus-ssis001}", preview["plans"][0]["target_path"])


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
