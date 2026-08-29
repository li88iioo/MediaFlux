"""Task 17：AI 仅作为确定性 TMDB 识别失败后的结构化回退。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import requests
from fastapi.responses import JSONResponse

from tests.support import IsolatedDatabaseTestCase, release_parse_result


class _Response:
    def __init__(self, payload, status_code: int = 200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.text = self.content.decode("utf-8")

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class _SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected provider request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _choice(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


class AIRecognitionClientTests(unittest.TestCase):
    def test_valid_structured_json_is_parsed_and_ai_tmdb_id_is_discarded(self):
        from app.clients.ai_recognition import (
            AIRecognitionClient,
            AIRecognitionInput,
        )

        session = _Session(_Response(_choice(json.dumps({
            "title": "幕府将军",
            "original_title": "Shogun",
            "year": 2024,
            "media_type": "tv",
            "season": 1,
            "episode": 3,
            "aliases": ["将军"],
            "confidence": 0.93,
            "tmdb_id": 999999,
        }, ensure_ascii=False))))
        client = AIRecognitionClient(
            api_url="https://ai.invalid/v1/chat/completions",
            api_key="secret-key",
            model="compatible-model",
            session=session,
        )

        result = client.recognize(AIRecognitionInput(
            normalized_title="幕府将军 Shogun",
            filename_title="幕府将军 Shogun",
            folder_title="幕府将军 Shogun",
            folder_year="2024",
            media_type="tv",
            season=1,
            episode=3,
            aliases=("幕府将军", "Shogun"),
        ))

        self.assertEqual(result.title, "幕府将军")
        self.assertEqual(result.media_type, "tv")
        self.assertEqual(result.year, 2024)
        self.assertEqual(result.confidence, 0.93)
        self.assertFalse(hasattr(result, "tmdb_id"))
        url, options = session.calls[0]
        self.assertEqual(url, "https://ai.invalid/v1/chat/completions")
        self.assertEqual(options["headers"]["Authorization"], "Bearer secret-key")
        self.assertFalse(options["allow_redirects"])
        self.assertIn("json_schema", options["json"]["response_format"])

    def test_responses_protocol_uses_responses_endpoint_and_schema(self):
        from app.clients.ai_recognition import AIRecognitionClient, AIRecognitionInput

        content = json.dumps({
            "title": "Movie", "original_title": "", "year": 2024,
            "media_type": "movie", "season": None, "episode": None,
            "aliases": [], "confidence": 0.9,
        })
        session = _Session(_Response({"output_text": content}))
        client = AIRecognitionClient(
            api_url="https://ai.invalid/v1",
            protocol="responses",
            model="compatible-model",
            session=session,
        )

        result = client.recognize(AIRecognitionInput(normalized_title="Movie"))

        self.assertEqual(result.title, "Movie")
        url, options = session.calls[0]
        self.assertEqual(url, "https://ai.invalid/v1/responses")
        self.assertEqual(options["json"]["text"]["format"]["type"], "json_schema")
        self.assertIn("max_output_tokens", options["json"])

    def test_auto_protocol_falls_back_when_responses_200_has_invalid_envelope(self):
        from app.clients.ai_recognition import AIRecognitionClient, AIRecognitionInput

        payload = {
            "title": "Movie",
            "original_title": "",
            "year": 2024,
            "media_type": "movie",
            "season": None,
            "episode": None,
            "aliases": [],
            "confidence": 0.9,
        }
        session = _SequenceSession([
            _Response({"id": "resp_compat", "output": []}),
            _Response(_choice(json.dumps(payload))),
        ])
        client = AIRecognitionClient(
            api_url="https://ai.invalid/v1",
            protocol="auto",
            model="compatible-model",
            session=session,
        )

        result = client.recognize(AIRecognitionInput(normalized_title="Movie"))

        self.assertEqual(result.title, "Movie")
        self.assertEqual(
            [url for url, _options in session.calls],
            [
                "https://ai.invalid/v1/responses",
                "https://ai.invalid/v1/chat/completions",
            ],
        )

    def test_explicit_responses_does_not_fallback_on_invalid_envelope(self):
        from app.clients.ai_recognition import (
            AIRecognitionClient,
            AIRecognitionError,
            AIRecognitionInput,
        )

        session = _SequenceSession([
            _Response({"id": "resp_invalid", "output": []}),
            _Response(_choice("{}")),
        ])
        client = AIRecognitionClient(
            api_url="https://ai.invalid/v1",
            protocol="responses",
            model="compatible-model",
            session=session,
        )

        with self.assertRaisesRegex(AIRecognitionError, "严格 JSON"):
            client.recognize(AIRecognitionInput(normalized_title="Movie"))

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][0], "https://ai.invalid/v1/responses")

    def test_malformed_json_invalid_fields_and_timeout_fail_safely(self):
        from app.clients.ai_recognition import (
            AIRecognitionClient,
            AIRecognitionError,
            AIRecognitionInput,
        )

        input_data = AIRecognitionInput(normalized_title="Movie", media_type="movie")
        cases = [
            _Session(_Response(_choice("```json\n{}\n```"))),
            _Session(_Response(_choice(json.dumps({
                "title": "Movie", "original_title": "", "year": 1200,
                "media_type": "documentary", "season": None, "episode": None,
                "aliases": [], "confidence": 1.4,
            })))),
            _Session(error=requests.Timeout("timeout api_key=top-secret")),
        ]
        for session in cases:
            with self.subTest(session=session):
                client = AIRecognitionClient(
                    api_url="https://ai.invalid/v1/chat/completions",
                    api_key="top-secret",
                    model="compatible-model",
                    timeout_seconds=3,
                    session=session,
                )
                with self.assertRaises(AIRecognitionError) as caught:
                    client.recognize(input_data)
                self.assertNotIn("top-secret", str(caught.exception))

    def test_title_original_title_and_aliases_reject_non_string_values(self):
        from app.clients.ai_recognition import (
            AIRecognitionClient,
            AIRecognitionError,
            AIRecognitionInput,
        )

        valid = {
            "title": "Movie",
            "original_title": "Original Movie",
            "year": 2024,
            "media_type": "movie",
            "season": None,
            "episode": None,
            "aliases": ["Alias"],
            "confidence": 0.9,
        }
        invalid_fields = {
            "title": 123,
            "original_title": 456,
            "aliases": ["Alias", 789],
        }
        for field, invalid_value in invalid_fields.items():
            with self.subTest(field=field):
                payload = {**valid, field: invalid_value}
                client = AIRecognitionClient(
                    api_url="https://ai.invalid/v1/chat/completions",
                    model="compatible-model",
                    session=_Session(_Response(_choice(json.dumps(payload)))),
                )
                with self.assertRaises(AIRecognitionError):
                    client.recognize(AIRecognitionInput(
                        normalized_title="Movie", media_type="movie"
                    ))

    def test_alias_overflow_and_non_finite_confidence_are_rejected(self):
        from app.clients.ai_recognition import AIRecognitionError, _parse_result

        base = {
            "title": "Movie", "original_title": "", "year": 2024,
            "media_type": "movie", "season": None, "episode": None,
            "aliases": [], "confidence": 0.9,
        }
        with self.assertRaises(AIRecognitionError):
            _parse_result(dict(base, aliases=[f"Alias {index}" for index in range(11)]))
        with self.assertRaises(AIRecognitionError):
            _parse_result(dict(base, confidence=float("nan")))

    def test_invalid_endpoint_and_oversized_response_are_rejected(self):
        from app.clients.ai_recognition import (
            AIRecognitionClient,
            AIRecognitionError,
            AIRecognitionInput,
        )

        invalid_urls = [
            "https://user:pass@ai.invalid/v1/chat/completions",
            "https://ai.invalid/v1/chat/completions?api_key=secret",
            "https://ai.invalid/v1/chat/completions#debug",
        ]
        for api_url in invalid_urls:
            with self.subTest(api_url=api_url), self.assertRaises(AIRecognitionError):
                AIRecognitionClient(
                    api_url=api_url,
                    model="compatible-model",
                )

        response = _Response(_choice("{}"))
        response.content = b"x" * 70_000
        client = AIRecognitionClient(
            api_url="https://ai.invalid/v1/chat/completions",
            model="compatible-model",
            session=_Session(response),
        )
        with self.assertRaises(AIRecognitionError):
            client.recognize(AIRecognitionInput(
                normalized_title="Movie", media_type="movie"
            ))

    def test_sensitive_provider_output_is_rejected_before_use(self):
        from app.clients.ai_recognition import (
            AIRecognitionClient,
            AIRecognitionError,
            AIRecognitionInput,
            _parse_release_group_result,
        )

        payload = {
            "title": "Movie api_key=top-secret-value",
            "original_title": "",
            "year": 2024,
            "media_type": "movie",
            "season": None,
            "episode": None,
            "aliases": [],
            "confidence": 0.9,
        }
        client = AIRecognitionClient(
            api_url="https://ai.invalid/v1/chat/completions",
            model="compatible-model",
            session=_Session(_Response(_choice(json.dumps(payload)))),
        )
        with self.assertRaises(AIRecognitionError) as caught:
            client.recognize(AIRecognitionInput(normalized_title="Movie"))
        self.assertIn("返回内容", str(caught.exception))
        self.assertNotIn("top-secret-value", str(caught.exception))

        with self.assertRaises(AIRecognitionError) as caught:
            _parse_release_group_result({
                "is_release_group": True,
                "canonical_name": "Authorization: Bearer sk-12345678901234567890",
                "aliases": [],
                "confidence": 0.9,
            })
        self.assertIn("返回内容", str(caught.exception))
        self.assertNotIn("sk-12345678901234567890", str(caught.exception))

    def test_transport_failures_are_classified_for_bounded_web_fallback(self):
        from app.clients.ai_recognition import (
            AIRecognitionClient,
            AIRecognitionInput,
            AIRecognitionProviderError,
            AIRecognitionUnavailableError,
        )

        input_data = AIRecognitionInput(normalized_title="Movie")
        unavailable = AIRecognitionClient(
            api_url="https://ai.invalid/v1/chat/completions",
            model="compatible-model",
            session=_Session(error=requests.Timeout("offline")),
        )
        with self.assertRaises(AIRecognitionUnavailableError):
            unavailable.recognize(input_data)

        for error in (
            requests.exceptions.InvalidURL("invalid endpoint"),
            httpx.LocalProtocolError("invalid protocol"),
        ):
            with self.subTest(error=type(error).__name__):
                client = AIRecognitionClient(
                    api_url="https://ai.invalid/v1/chat/completions",
                    model="compatible-model",
                    session=_Session(error=error),
                )
                with self.assertRaises(AIRecognitionProviderError) as caught:
                    client.recognize(input_data)
                self.assertNotIsInstance(
                    caught.exception, AIRecognitionUnavailableError
                )

    def test_production_client_checks_sync_bridge_before_request_budget(self):
        from app.agent.async_bridge import AsyncBridgeUnavailable
        from app.clients.ai_recognition import (
            AIRecognitionClient,
            AIRecognitionError,
            AIRecognitionInput,
        )

        client = AIRecognitionClient(
            api_url="https://ai.invalid/v1/chat/completions",
            model="compatible-model",
        )
        with patch(
            "app.clients.ai_recognition.ensure_sync_bridge_available",
            side_effect=AsyncBridgeUnavailable("active loop"),
        ), patch(
            "app.clients.ai_recognition.acquire_ai_recognition_attempt"
        ) as acquire:
            with self.assertRaises(AIRecognitionError):
                client.recognize(AIRecognitionInput(normalized_title="Movie"))
        acquire.assert_not_called()

    def test_production_client_acquires_and_releases_governance_lease(self):
        from app.clients.ai_recognition import AIRecognitionClient, AIRecognitionInput

        payload = {
            "title": "Movie",
            "original_title": "",
            "year": 2024,
            "media_type": "movie",
            "season": None,
            "episode": None,
            "aliases": [],
            "confidence": 0.9,
        }
        client = AIRecognitionClient(
            api_url="https://ai.invalid/v1/chat/completions",
            model="compatible-model",
        )
        lease = Mock()
        with patch(
            "app.clients.ai_recognition.ensure_sync_bridge_available"
        ), patch(
            "app.clients.ai_recognition.acquire_ai_recognition_attempt",
            return_value=lease,
        ) as acquire, patch.object(
            client, "_post", return_value=_Response(_choice(json.dumps(payload)))
        ), patch(
            "app.clients.ai_recognition.record_ai_recognition_success"
        ) as success:
            result = client.recognize(AIRecognitionInput(normalized_title="Movie"))

        self.assertEqual(result.title, "Movie")
        acquire.assert_called_once_with(client.provider_fingerprint)
        lease.release.assert_called_once_with()
        success.assert_called_once_with(client.provider_fingerprint)


class _TMDBClient:
    api_key = "tmdb-key"
    base_url = "https://tmdb.invalid/3"
    config_error = ""
    session = None

    def __init__(
        self,
        corrected_title: str = "Corrected Movie",
        detail_payload=None,
        season_detail_payload=None,
    ):
        self.corrected_title = corrected_title
        self.search_calls = []
        self.detail_calls = []
        self.season_detail_calls = []
        self.season_detail_payload = season_detail_payload or {}
        self.detail_payload = detail_payload if detail_payload is not None else {
            "id": 42,
            "title": corrected_title,
            "original_title": "Corrected Original",
            "release_date": "2024-01-01",
        }

    def search(self, title, year, media_type):
        self.search_calls.append((title, year, media_type))
        if title != self.corrected_title:
            return []
        return [{
            "id": 42,
            "title": self.corrected_title,
            "original_title": "Corrected Original",
            "release_date": "2024-01-01",
            "media_type": "movie",
        }]

    def detail(self, tmdb_id, media_type):
        self.detail_calls.append((tmdb_id, media_type))
        return dict(self.detail_payload)

    def tv_season_detail(self, tmdb_id, season_number):
        self.season_detail_calls.append((tmdb_id, season_number))
        return dict(self.season_detail_payload)


class TMDBPositionValidationTests(unittest.TestCase):
    def test_tv_season_and_episode_must_exist_in_tmdb_detail(self):
        from app.modules.scraper import _validate_tmdb_position

        detail = {
            "seasons": [
                {"season_number": 0, "episode_count": 2},
                {"season_number": 2, "episode_count": 12},
            ]
        }
        self.assertTrue(_validate_tmdb_position(detail, "tv", 2, 3)["passed"])
        self.assertEqual(
            _validate_tmdb_position(detail, "tv", 3, 1)["reason"],
            "season_not_found",
        )
        self.assertEqual(
            _validate_tmdb_position(detail, "tv", 2, 13)["reason"],
            "episode_out_of_range",
        )
        self.assertTrue(_validate_tmdb_position(detail, "tv", 0, 1)["passed"])
        self.assertEqual(
            _validate_tmdb_position({"seasons": "invalid"}, "tv", 1, 1)["reason"],
            "seasons_missing",
        )

    def test_movie_or_tv_without_position_does_not_require_position_validation(self):
        from app.modules.scraper import _validate_tmdb_position

        self.assertTrue(_validate_tmdb_position({}, "movie", None, None)["passed"])
        result = _validate_tmdb_position({}, "tv", None, None)
        self.assertTrue(result["passed"])
        self.assertFalse(result["required"])


class AIFallbackPipelineTests(IsolatedDatabaseTestCase):
    @staticmethod
    def _ai_result(confidence: float = 0.95):
        from app.clients.ai_recognition import AIRecognitionResult

        return AIRecognitionResult(
            title="Corrected Movie",
            original_title="Corrected Original",
            year=2024,
            media_type="movie",
            season=None,
            episode=None,
            aliases=("Corrected Alias",),
            confidence=confidence,
        )

    def test_deterministic_success_bypasses_ai(self):
        from app.modules.scraper import RecognitionResult, TMDBScraper

        scraper = TMDBScraper(client=_TMDBClient())
        matched = RecognitionResult(
            tmdb_id="1", title="Deterministic", year="2024",
            media_type="movie", confidence=1.0, status="matched",
            matched_by="search",
        )
        with patch.object(
            scraper, "deterministic_recognize", return_value=matched
        ), patch("app.modules.scraper.AIRecognitionClient") as ai_client:
            result = scraper.match("Deterministic.2024.mkv")

        self.assertIs(result, matched)
        ai_client.assert_not_called()

    def test_ai_disabled_keeps_deterministic_failure(self):
        from app.modules.scraper import RecognitionResult, TMDBScraper

        scraper = TMDBScraper(client=_TMDBClient())
        failed = RecognitionResult(
            media_type="movie", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
        )
        with patch.object(
            scraper, "deterministic_recognize", return_value=failed
        ), patch("app.modules.scraper.get_bool", return_value=False), patch(
            "app.modules.scraper.AIRecognitionClient"
        ) as ai_client:
            result = scraper.match("Unknown.Release.2024.mkv")

        self.assertIs(result, failed)
        self.assertFalse(result.ai_diagnostic["attempted"])
        ai_client.assert_not_called()

    def test_ai_enabled_but_misconfigured_does_not_construct_client(self):
        from app.modules.scraper import RecognitionResult, TMDBScraper

        scraper = TMDBScraper(client=_TMDBClient())
        failed = RecognitionResult(
            media_type="movie", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
        )
        with patch.object(
            scraper, "deterministic_recognize", return_value=failed
        ), patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get", return_value=""
        ), patch("app.modules.scraper.AIRecognitionClient") as ai_client:
            result = scraper.match("Unknown.Release.2024.mkv")

        self.assertIs(result, failed)
        self.assertFalse(result.ai_diagnostic["attempted"])
        self.assertEqual(result.ai_diagnostic["reason"], "misconfigured")
        ai_client.assert_not_called()

    def test_ai_provider_uses_media_agent_connection(self):
        from app.modules.scraper import TMDBScraper

        values = {
            "AGENT_LLM_API_URL": "https://agent.invalid/v1/chat/completions",
            "AGENT_LLM_API_KEY": "shared-secret",
            "AGENT_LLM_MODEL": "shared-model",
            "AGENT_LLM_TIMEOUT_SECONDS": "9",
        }
        with patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": values.get(key, default),
        ):
            provider = TMDBScraper._ai_provider_settings()

        self.assertEqual(provider, (
            "https://agent.invalid/v1/chat/completions",
            "shared-model",
            "shared-secret",
            9,
            "chat_completions",
        ))

    def test_ai_provider_is_disabled_without_media_agent_connection(self):
        from app.modules.scraper import TMDBScraper

        with patch("app.modules.scraper.get", return_value=""):
            provider = TMDBScraper._ai_provider_settings()

        self.assertEqual(provider, ("", "", "", 12, "auto"))

    def test_ai_result_runs_fresh_tmdb_search_and_requires_tmdb_detail_revalidation(self):
        from app.modules.scraper import RecognitionResult, TMDBScraper

        tmdb_client = _TMDBClient()
        scraper = TMDBScraper(client=tmdb_client)
        failed = RecognitionResult(
            media_type="movie", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
        )
        ai = Mock()
        ai.recognize.return_value = self._ai_result()
        with patch.object(
            scraper, "deterministic_recognize", return_value=failed
        ), patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": {
                "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
                "AGENT_LLM_API_KEY": "secret",
                "AGENT_LLM_MODEL": "compatible-model",
                "AGENT_LLM_TIMEOUT_SECONDS": "8",
                "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
            }.get(key, default),
        ), patch(
            "app.modules.scraper.AIRecognitionClient", return_value=ai
        ), patch.object(scraper, "_set_lock") as set_lock:
            result = scraper.match(
                "Corrected.Movie.2024.mkv",
                "/电影/Corrected Movie (2024)",
            )

        self.assertEqual(result.tmdb_id, "42")
        self.assertEqual(result.matched_by, "ai_tmdb_revalidated")
        self.assertTrue(result.ai_diagnostic["attempted"])
        self.assertEqual(result.ai_diagnostic["reason"], "deterministic_failed")
        self.assertEqual(result.ai_diagnostic["output"]["title"], "Corrected Movie")
        self.assertTrue(any(call[0] == "Corrected Movie" for call in tmdb_client.search_calls))
        self.assertEqual(tmdb_client.detail_calls, [("42", "movie")])
        self.assertFalse(result.need_confirm)
        self.assertEqual(result.status, "matched")
        self.assertTrue(result.ai_diagnostic["tmdb_revalidation"]["passed"])
        set_lock.assert_not_called()

    def test_ai_exact_tmdb_result_without_source_anchor_stays_manual(self):
        from app.modules.scraper import RecognitionResult, TMDBScraper

        scraper = TMDBScraper(client=_TMDBClient())
        failed = RecognitionResult(
            media_type="movie", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
        )
        ai = Mock()
        ai.recognize.return_value = self._ai_result()
        with patch.object(
            scraper, "deterministic_recognize", return_value=failed
        ), patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": {
                "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
                "AGENT_LLM_MODEL": "compatible-model",
                "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
            }.get(key, default),
        ), patch("app.modules.scraper.AIRecognitionClient", return_value=ai):
            result = scraper.match(
                "Unknown.Release.2024.mkv", "/电影/Unknown Release (2024)"
            )

        self.assertEqual(result.status, "low_confidence")
        self.assertTrue(result.need_confirm)
        anchor = result.ai_diagnostic["tmdb_revalidation"]["source_anchor"]
        self.assertFalse(anchor["passed"])
        self.assertEqual(anchor["reason"], "source_anchor_unverified")

    def test_ai_base_franchise_candidate_cannot_drop_distinctive_source_title(self):
        from app.clients.ai_recognition import AIRecognitionResult
        from app.modules.scraper import Candidate, RecognitionContext, RecognitionResult, TMDBScraper

        detail = {
            "id": 100,
            "name": "Kamen Rider",
            "original_name": "仮面ライダー",
            "first_air_date": "1971-04-03",
        }
        scraper = TMDBScraper(client=_TMDBClient(
            corrected_title="Kamen Rider", detail_payload=detail,
        ))
        failed = RecognitionResult(
            media_type="tv", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
            context=RecognitionContext(
                filename="Kamen Rider ZEZTZ [42].mkv",
                normalized_title="Kamen Rider ZEZTZ",
                filename_title="Kamen Rider ZEZTZ",
                media_type="tv",
                episode=42,
                title_variants=["Kamen Rider ZEZTZ"],
            ),
        )
        ai_result = AIRecognitionResult(
            title="Kamen Rider", original_title="仮面ライダー", year=1971,
            media_type="tv", season=1, episode=42, aliases=(), confidence=0.99,
        )
        second = RecognitionResult(
            tmdb_id="100", title="Kamen Rider", year="1971", media_type="tv",
            confidence=1.0, status="matched", matched_by="ai_search",
            threshold_decision={"passed": True},
            candidates=[Candidate(
                tmdb_id="100", title="Kamen Rider", year="1971", score=1.0,
                media_type="tv", original_title="仮面ライダー",
            )],
        )
        values = {
            "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
            "AGENT_LLM_MODEL": "compatible-model",
            "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
        }
        with patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch.object(
            scraper, "_recognize_with_ai_cache", return_value=ai_result,
        ), patch.object(
            scraper, "_recognize_context", return_value=second,
        ), patch.object(
            scraper, "_tavily_ai_tmdb_corroboration", return_value=False,
        ):
            result = scraper._ai_fallback(
                "Kamen Rider ZEZTZ [42].mkv", "", failed,
            )

        self.assertTrue(result.need_confirm)
        self.assertEqual(result.status, "low_confidence")
        anchor = result.ai_diagnostic["tmdb_revalidation"]["source_anchor"]
        self.assertFalse(anchor["passed"])
        self.assertEqual(anchor["reason"], "distinctive_source_title_remainder")

    def test_ai_source_anchor_accepts_explicit_quoted_release_wrapper_only(self):
        from app.clients.ai_recognition import AIRecognitionResult
        from app.modules.scraper import Candidate, RecognitionContext, RecognitionResult, TMDBScraper

        title = "北斗之拳 拳王軍雜兵們的輓歌"
        wrapped_title = f"Animatica「{title}」"
        detail = {
            "id": 310026,
            "name": title,
            "original_name": title,
            "first_air_date": "2026-01-01",
            "seasons": [{"season_number": 1, "episode_count": 12}],
        }
        scraper = TMDBScraper(client=_TMDBClient(
            corrected_title=title, detail_payload=detail,
        ))
        failed = RecognitionResult(
            media_type="tv", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
            context=RecognitionContext(
                filename=f"{wrapped_title} - 09.mkv",
                normalized_title=wrapped_title,
                filename_title=wrapped_title,
                media_type="tv", season=1, episode=9,
                title_variants=[wrapped_title, title, "Animatica"],
            ),
        )
        ai_result = AIRecognitionResult(
            title=title, original_title=title, year=2026,
            media_type="tv", season=1, episode=9, aliases=(), confidence=0.99,
        )
        second = RecognitionResult(
            tmdb_id="310026", title=title, year="2026", media_type="tv",
            confidence=1.0, status="matched", matched_by="ai_search",
            threshold_decision={"passed": True},
            candidates=[Candidate(
                tmdb_id="310026", title=title, year="2026", score=1.0,
                media_type="tv", original_title=title,
            )],
        )
        values = {
            "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
            "AGENT_LLM_MODEL": "compatible-model",
            "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
        }
        with patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch.object(
            scraper, "_recognize_with_ai_cache", return_value=ai_result,
        ), patch.object(
            scraper, "_recognize_context", return_value=second,
        ):
            result = scraper._ai_fallback(
                f"{wrapped_title} - 09.mkv", "", failed,
            )

        self.assertFalse(result.need_confirm)
        self.assertEqual(result.status, "matched")
        anchor = result.ai_diagnostic["tmdb_revalidation"]["source_anchor"]
        self.assertTrue(anchor["passed"])
        self.assertEqual(anchor["matched_source"], wrapped_title)

    def test_ai_title_revalidation_accepts_official_tmdb_alias(self):
        from app.clients.ai_recognition import AIRecognitionResult
        from app.modules.scraper import Candidate, RecognitionContext, RecognitionResult, TMDBScraper

        detail = {
            "id": 30980,
            "name": "魔法禁书目录",
            "original_name": "とある魔術の禁書目録",
            "first_air_date": "2008-10-05",
            "alternative_titles": {
                "results": [{"title": "A Certain Magical Index"}],
            },
            "seasons": [{"season_number": 1, "episode_count": 24}],
        }
        scraper = TMDBScraper(client=_TMDBClient(
            corrected_title="A Certain Magical Index", detail_payload=detail,
        ))
        failed = RecognitionResult(
            media_type="tv", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
            context=RecognitionContext(
                filename="A Certain Magical Index S01E01.mkv",
                normalized_title="A Certain Magical Index",
                filename_title="A Certain Magical Index",
                filename_year="2008",
                media_type="tv", season=1, episode=1,
                title_variants=["A Certain Magical Index"],
            ),
        )
        ai_result = AIRecognitionResult(
            title="A Certain Magical Index", original_title="", year=2008,
            media_type="tv", season=1, episode=1, aliases=(), confidence=0.99,
        )
        second = RecognitionResult(
            tmdb_id="30980", title="魔法禁书目录", year="2008", media_type="tv",
            confidence=1.0, status="matched", matched_by="ai_search",
            threshold_decision={"passed": True},
            candidates=[Candidate(
                tmdb_id="30980", title="魔法禁书目录", year="2008", score=1.0,
                media_type="tv", original_title="とある魔術の禁書目録",
                aliases=["A Certain Magical Index"],
            )],
        )
        values = {
            "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
            "AGENT_LLM_MODEL": "compatible-model",
            "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
        }
        with patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch.object(
            scraper, "_recognize_with_ai_cache", return_value=ai_result,
        ), patch.object(scraper, "_recognize_context", return_value=second):
            result = scraper._ai_fallback(
                "A Certain Magical Index S01E01.mkv", "", failed,
            )

        self.assertFalse(result.need_confirm)
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.tmdb_id, "30980")
        revalidation = result.ai_diagnostic["tmdb_revalidation"]
        self.assertTrue(revalidation["title_verified"])
        self.assertTrue(revalidation["source_anchor"]["passed"])

    def test_ai_detail_missing_id_or_requested_year_stays_manual(self):
        from app.modules.scraper import RecognitionResult, TMDBScraper

        for detail, failed_field in (
            ({"title": "Corrected Movie", "release_date": "2024-01-01"}, "id_verified"),
            ({"id": 42, "title": "Corrected Movie"}, "year_verified"),
        ):
            with self.subTest(failed_field=failed_field):
                scraper = TMDBScraper(client=_TMDBClient(detail_payload=detail))
                failed = RecognitionResult(
                    media_type="movie", confidence=0.0, status="no_result",
                    need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
                )
                ai = Mock()
                ai.recognize.return_value = self._ai_result()
                with patch.object(
                    scraper, "deterministic_recognize", return_value=failed
                ), patch("app.modules.scraper.get_bool", return_value=True), patch(
                    "app.modules.scraper.get",
                    side_effect=lambda key, default="": {
                        "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
                        "AGENT_LLM_MODEL": "compatible-model",
                        "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
                    }.get(key, default),
                ), patch("app.modules.scraper.AIRecognitionClient", return_value=ai):
                    result = scraper.match(
                        "Corrected.Movie.2024.mkv", "/电影/Corrected Movie (2024)"
                    )

                self.assertTrue(result.need_confirm)
                self.assertFalse(
                    result.ai_diagnostic["tmdb_revalidation"][failed_field]
                )

    def test_ai_cannot_override_explicit_source_year_without_confirmation(self):
        from app.clients.ai_recognition import AIRecognitionResult
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        detail = {
            "id": 42, "title": "The Thing", "original_title": "The Thing",
            "release_date": "2011-10-12",
        }
        scraper = TMDBScraper(client=_TMDBClient(
            corrected_title="The Thing", detail_payload=detail,
        ))
        failed = RecognitionResult(
            media_type="movie", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
            context=RecognitionContext(
                filename="The.Thing.1982.mkv", normalized_title="The Thing",
                filename_title="The Thing", filename_year="1982",
                media_type="movie", title_variants=["The Thing"],
            ),
        )
        ai = Mock()
        ai.recognize.return_value = AIRecognitionResult(
            title="The Thing", original_title="The Thing", year=2011,
            media_type="movie", season=None, episode=None, aliases=(), confidence=0.98,
        )
        second = RecognitionResult(
            tmdb_id="42", title="The Thing", year="2011", media_type="movie",
            confidence=1.0, status="matched", matched_by="ai_search",
            threshold_decision={"passed": True},
        )
        with patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": {
                "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
                "AGENT_LLM_MODEL": "compatible-model",
                "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
            }.get(key, default),
        ), patch("app.modules.scraper.AIRecognitionClient", return_value=ai), patch.object(
            scraper, "_recognize_context", return_value=second
        ):
            result = scraper._ai_fallback("The.Thing.1982.mkv", "", failed)

        self.assertTrue(result.need_confirm)
        self.assertEqual(result.status, "low_confidence")
        self.assertIn("原始文件年份冲突", result.error)
        self.assertFalse(result.ai_diagnostic["tmdb_revalidation"]["source_year_verified"])
        self.assertEqual(result.ai_diagnostic["tmdb_revalidation"]["source_year"], "1982")

    def test_ai_tv_candidate_requires_deterministic_episode_to_exist(self):
        from app.clients.ai_recognition import AIRecognitionResult
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        detail = {
            "id": 42,
            "name": "Corrected Show",
            "original_name": "Corrected Show",
            "first_air_date": "2024-01-01",
            "seasons": [{"season_number": 2, "episode_count": 2}],
        }
        tmdb_client = _TMDBClient(corrected_title="Corrected Show", detail_payload=detail)
        scraper = TMDBScraper(client=tmdb_client)
        failed = RecognitionResult(
            media_type="tv", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
            context=RecognitionContext(
                filename="Unknown.Show.S02E03.mkv", normalized_title="Unknown Show",
                filename_title="Unknown Show", media_type="tv", season=2, episode=3,
                title_variants=["Unknown Show"],
            ),
        )
        ai = Mock()
        ai.recognize.return_value = AIRecognitionResult(
            title="Corrected Show", original_title="Corrected Show", year=2024,
            media_type="tv", season=9, episode=99, aliases=(), confidence=0.95,
        )
        second = RecognitionResult(
            tmdb_id="42", title="Corrected Show", year="2024", media_type="tv",
            confidence=1.0, status="matched", matched_by="ai_search",
            threshold_decision={"passed": True},
        )
        with patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": {
                "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
                "AGENT_LLM_MODEL": "compatible-model",
                "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
            }.get(key, default),
        ), patch("app.modules.scraper.AIRecognitionClient", return_value=ai), patch.object(
            scraper, "_recognize_context", return_value=second
        ):
            result = scraper._ai_fallback("Unknown.Show.S02E03.mkv", "", failed)

        self.assertTrue(result.need_confirm)
        self.assertEqual(result.status, "low_confidence")
        position = result.ai_diagnostic["tmdb_revalidation"]["position"]
        self.assertEqual(position["season"], 2)
        self.assertEqual(position["episode"], 3)
        self.assertEqual(position["reason"], "episode_out_of_range")
        self.assertEqual(result.ai_diagnostic["position_guard"]["ignored_season"], 9)

    @staticmethod
    def _merged_cour_season_detail():
        from datetime import date, timedelta

        episodes = []
        aired_on = date(2024, 1, 1)
        for number in range(1, 25):
            if number == 13:
                aired_on += timedelta(days=49)
            episodes.append({
                "episode_number": number,
                "air_date": aired_on.isoformat(),
            })
            aired_on += timedelta(days=7)
        return {
            "season_number": 1,
            "episodes": episodes,
        }

    def test_ai_fallback_can_reuse_verified_merged_cour_mapping(self):
        from app.clients.ai_recognition import AIRecognitionResult
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        detail = {
            "id": 42,
            "name": "Corrected Show",
            "original_name": "Corrected Show",
            "first_air_date": "2024-01-01",
            "seasons": [{"season_number": 1, "episode_count": 24}],
        }
        tmdb_client = _TMDBClient(
            corrected_title="Corrected Show",
            detail_payload=detail,
            season_detail_payload=self._merged_cour_season_detail(),
        )
        scraper = TMDBScraper(client=tmdb_client)
        failed = RecognitionResult(
            media_type="tv", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
            context=RecognitionContext(
                filename="Corrected.Show.S02E06.mkv",
                normalized_title="Corrected Show",
                filename_title="Corrected Show",
                media_type="tv", season=2, episode=6,
                title_variants=["Corrected Show"],
            ),
        )
        ai = Mock()
        ai.recognize.return_value = AIRecognitionResult(
            title="Corrected Show", original_title="Corrected Show", year=2024,
            media_type="tv", season=2, episode=6, aliases=(), confidence=0.95,
        )
        second = RecognitionResult(
            tmdb_id="42", title="Corrected Show", year="2024", media_type="tv",
            confidence=1.0, status="matched", matched_by="ai_search",
            threshold_decision={"passed": True},
        )
        with patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": {
                "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
                "AGENT_LLM_MODEL": "compatible-model",
                "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
            }.get(key, default),
        ), patch("app.modules.scraper.AIRecognitionClient", return_value=ai), patch.object(
            scraper, "_recognize_context", return_value=second
        ):
            result = scraper._ai_fallback("Corrected.Show.S02E06.mkv", "", failed)

        self.assertFalse(result.need_confirm)
        self.assertEqual(result.matched_by, "ai_tmdb_revalidated")
        mapping = result.metadata["episode_mapping"]
        self.assertEqual(
            (mapping["source_season"], mapping["source_episode"]), (2, 6)
        )
        self.assertEqual(
            (mapping["target_season"], mapping["target_episode"]), (1, 18)
        )
        self.assertEqual(tmdb_client.season_detail_calls, [("42", 1)])

    def test_ai_position_conflict_has_clear_error_and_skips_tavily(self):
        from app.clients.ai_recognition import AIRecognitionResult
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        detail = {
            "id": 42,
            "name": "Corrected Show",
            "original_name": "Corrected Show",
            "first_air_date": "2024-01-01",
            "seasons": [{"season_number": 1, "episode_count": 12}],
        }
        scraper = TMDBScraper(client=_TMDBClient(
            corrected_title="Corrected Show", detail_payload=detail
        ))
        failed = RecognitionResult(
            media_type="tv", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
            context=RecognitionContext(
                filename="Corrected.Show.S03E19.mkv",
                normalized_title="Corrected Show",
                filename_title="Corrected Show",
                media_type="tv", season=3, episode=19,
                title_variants=["Corrected Show"],
            ),
        )
        ai = Mock()
        ai.recognize.return_value = AIRecognitionResult(
            title="Corrected Show", original_title="Corrected Show", year=2024,
            media_type="tv", season=3, episode=19, aliases=(), confidence=0.95,
        )
        second = RecognitionResult(
            tmdb_id="42", title="Corrected Show", year="2024", media_type="tv",
            confidence=0.88, status="low_confidence", matched_by="ai_search",
            threshold_decision={"passed": False},
        )
        with patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": {
                "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
                "AGENT_LLM_MODEL": "compatible-model",
                "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
            }.get(key, default),
        ), patch("app.modules.scraper.AIRecognitionClient", return_value=ai), patch.object(
            scraper, "_recognize_context", return_value=second
        ), patch.object(scraper, "_tavily_ai_tmdb_corroboration") as tavily:
            result = scraper._ai_fallback("Corrected.Show.S03E19.mkv", "", failed)

        self.assertTrue(result.need_confirm)
        self.assertIn("文件季号在 TMDB 中不存在", result.error)
        tavily.assert_not_called()
        corroboration = result.ai_diagnostic["tmdb_revalidation"][
            "tavily_corroboration"
        ]
        self.assertEqual(corroboration["status"], "position_conflict")
        self.assertEqual(corroboration["reason"], "season_not_found")

    def test_ai_candidate_with_missing_tmdb_detail_stays_manual(self):
        from app.modules.scraper import RecognitionResult, TMDBScraper

        tmdb_client = _TMDBClient(detail_payload={})
        scraper = TMDBScraper(client=tmdb_client)
        failed = RecognitionResult(
            media_type="movie", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
        )
        ai = Mock()
        ai.recognize.return_value = self._ai_result()
        with patch.object(
            scraper, "deterministic_recognize", return_value=failed
        ), patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": {
                "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
                "AGENT_LLM_MODEL": "compatible-model",
                "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
            }.get(key, default),
        ), patch("app.modules.scraper.AIRecognitionClient", return_value=ai):
            result = scraper.match("Unknown.Release.2024.mkv")

        self.assertTrue(result.need_confirm)
        self.assertEqual(result.status, "low_confidence")
        self.assertIn("详情复核", result.error)
        self.assertFalse(result.ai_diagnostic["tmdb_revalidation"]["passed"])

    def test_low_ai_confidence_requires_confirmation_even_for_exact_tmdb_match(self):
        from app.modules.scraper import RecognitionResult, TMDBScraper

        scraper = TMDBScraper(client=_TMDBClient())
        failed = RecognitionResult(
            media_type="movie", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
        )
        ai = Mock()
        ai.recognize.return_value = self._ai_result(confidence=0.4)
        with patch.object(
            scraper, "deterministic_recognize", return_value=failed
        ), patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": {
                "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
                "AGENT_LLM_API_KEY": "",
                "AGENT_LLM_MODEL": "compatible-model",
                "AGENT_LLM_TIMEOUT_SECONDS": "8",
                "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
            }.get(key, default),
        ), patch(
            "app.modules.scraper.AIRecognitionClient", return_value=ai
        ), patch.object(scraper, "_set_lock") as set_lock:
            result = scraper.match("Unknown.Release.2024.mkv")

        self.assertEqual(result.tmdb_id, "42")
        self.assertTrue(result.need_confirm)
        self.assertEqual(result.status, "low_confidence")
        self.assertIn("AI", result.error)
        set_lock.assert_not_called()

    def test_ai_timeout_returns_original_deterministic_failure_without_credentials(self):
        from app.clients.ai_recognition import AIRecognitionError
        from app.modules.scraper import RecognitionResult, TMDBScraper

        scraper = TMDBScraper(client=_TMDBClient())
        failed = RecognitionResult(
            media_type="movie", confidence=0.0, status="no_result",
            need_confirm=True, matched_by="search", error="TMDB 无搜索结果",
        )
        ai = Mock()
        ai.recognize.side_effect = AIRecognitionError(
            "AI 请求失败 api_key=********"
        )
        with patch.object(
            scraper, "deterministic_recognize", return_value=failed
        ), patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": {
                "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
                "AGENT_LLM_API_KEY": "secret",
                "AGENT_LLM_MODEL": "compatible-model",
            }.get(key, default),
        ), patch(
            "app.modules.scraper.AIRecognitionClient", return_value=ai
        ):
            result = scraper.match("Unknown.Release.2024.mkv")

        self.assertIs(result, failed)
        serialized = json.dumps(result.ai_diagnostic, ensure_ascii=False)
        self.assertNotIn("secret", serialized)
        self.assertTrue(result.ai_diagnostic["attempted"])

    @staticmethod
    def _corroboration_ai_result():
        from app.clients.ai_recognition import AIRecognitionResult

        return AIRecognitionResult(
            title="The Eminence in Shadow",
            original_title="The Eminence in Shadow",
            year=2024,
            media_type="movie",
            season=None,
            episode=None,
            aliases=(),
            confidence=0.96,
        )

    @staticmethod
    def _corroboration_failed(*, rejected_constraints=None):
        from app.modules.scraper import RecognitionContext, RecognitionResult

        source = "Kage no Jitsuryokusha ni Naritakute"
        return RecognitionResult(
            media_type="movie",
            confidence=0.0,
            status="no_result",
            need_confirm=True,
            matched_by="search",
            error="TMDB 无搜索结果",
            rejected_constraints=list(rejected_constraints or []),
            context=RecognitionContext(
                filename=f"{source}.2024.mkv",
                normalized_title=source,
                filename_title=source,
                filename_year="2024",
                folder_title=source,
                folder_year="2024",
                media_type="movie",
                title_variants=[source],
            ),
        )

    @staticmethod
    def _corroboration_match(
        tmdb_id: str = "42",
        *,
        matched_by="ai_search",
        confidence: float = 0.96,
        threshold_passed: bool = True,
        rejected_constraints=None,
    ):
        from app.modules.scraper import RecognitionResult

        return RecognitionResult(
            tmdb_id=tmdb_id,
            title="The Eminence in Shadow",
            year="2024",
            media_type="movie",
            confidence=confidence,
            status="matched" if threshold_passed else "low_confidence",
            need_confirm=not threshold_passed,
            matched_by=matched_by,
            threshold_decision={"passed": threshold_passed},
            rejected_constraints=list(rejected_constraints or []),
        )

    def _run_tavily_ai_corroboration(
        self,
        *,
        hint_titles=(
            "Kage no Jitsuryokusha ni Naritakute The Eminence in Shadow",
        ),
        strict_tmdb_id="42",
        rejected_constraints=None,
        primary_rejected_constraints=None,
        primary_confidence: float = 0.96,
        primary_threshold_passed: bool = True,
        hint_error=None,
    ):
        from app.modules.recognition_web_hints import RecognitionWebHintResult
        from app.modules.scraper import TMDBScraper

        detail = {
            "id": 42,
            "title": "The Eminence in Shadow",
            "original_title": "The Eminence in Shadow",
            "release_date": "2024-01-01",
        }
        scraper = TMDBScraper(client=_TMDBClient(
            corrected_title="The Eminence in Shadow",
            detail_payload=detail,
        ))
        failed = self._corroboration_failed(
            rejected_constraints=rejected_constraints,
        )
        primary = self._corroboration_match(
            confidence=primary_confidence,
            threshold_passed=primary_threshold_passed,
            rejected_constraints=primary_rejected_constraints,
        )
        strict = self._corroboration_match(
            strict_tmdb_id,
            matched_by="ai_tavily_hint_search",
        )
        values = {
            "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
            "AGENT_LLM_API_KEY": "",
            "AGENT_LLM_MODEL": "compatible-model",
            "AGENT_LLM_TIMEOUT_SECONDS": "8",
            "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
        }
        hint_patch = patch(
            "app.modules.recognition_web_hints.search_recognition_titles",
            side_effect=hint_error,
            return_value=RecognitionWebHintResult(
                titles=tuple(hint_titles),
                attempted=True,
                status="matched",
            ),
        )
        with patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch.object(
            scraper,
            "_recognize_with_ai_cache",
            return_value=self._corroboration_ai_result(),
        ), patch.object(
            scraper,
            "_recognize_context",
            side_effect=[primary, strict],
        ) as recognize, hint_patch as web_search:
            result = scraper._ai_fallback(
                failed.context.filename,
                "",
                failed,
            )
        return result, scraper, recognize, web_search

    def test_tavily_ai_tmdb_corroboration_can_auto_pass_same_strict_id(self):
        result, scraper, recognize, web_search = self._run_tavily_ai_corroboration()

        self.assertEqual(result.matched_by, "ai_tavily_tmdb_revalidated")
        self.assertEqual(result.status, "matched")
        self.assertFalse(result.need_confirm)
        self.assertEqual(recognize.call_count, 2)
        self.assertEqual(recognize.call_args_list[1].kwargs["match_mode"], "strict")
        web_search.assert_called_once_with(
            "The Eminence in Shadow", media_type="movie", year="2024"
        )
        revalidation = result.ai_diagnostic["tmdb_revalidation"]
        self.assertTrue(revalidation["passed"])
        self.assertEqual(revalidation["resolution_mode"], "llm_tavily_tmdb")
        self.assertTrue(revalidation["tavily_corroboration"]["passed"])
        self.assertEqual(
            result.metadata["recognition_evidence"]["mode"],
            "llm_tavily_tmdb",
        )
        counters = scraper.performance_snapshot()
        self.assertEqual(counters["tavily_hint_requests"], 1)
        self.assertEqual(counters["tavily_hint_matches"], 1)

    def test_tavily_can_corroborate_ai_candidate_below_strict_local_threshold(self):
        result, scraper, recognize, web_search = self._run_tavily_ai_corroboration(
            primary_confidence=0.88,
            primary_threshold_passed=False,
        )

        self.assertEqual(result.matched_by, "ai_tavily_tmdb_revalidated")
        self.assertFalse(result.need_confirm)
        self.assertEqual(recognize.call_count, 2)
        web_search.assert_called_once()
        evidence = result.metadata["recognition_evidence"]
        self.assertEqual(evidence["mode"], "llm_tavily_tmdb")
        self.assertEqual(scraper.performance_snapshot()["tavily_hint_matches"], 1)

    def test_tavily_does_not_override_second_search_ambiguity(self):
        result, _scraper, recognize, web_search = self._run_tavily_ai_corroboration(
            primary_confidence=0.88,
            primary_threshold_passed=False,
            primary_rejected_constraints=["ambiguous_near_tie"],
        )

        self.assertTrue(result.need_confirm)
        self.assertEqual(recognize.call_count, 1)
        web_search.assert_not_called()
        corroboration = result.ai_diagnostic["tmdb_revalidation"][
            "tavily_corroboration"
        ]
        self.assertEqual(corroboration["status"], "deterministic_conflict")

    def test_source_year_can_match_tv_target_season_but_not_movie(self):
        from app.modules.scraper import _source_year_matches_tmdb

        detail = {
            "first_air_date": "2023-01-01",
            "seasons": [
                {"season_number": 3, "air_date": "2026-07-01", "episode_count": 20}
            ],
        }
        self.assertEqual(
            _source_year_matches_tmdb(detail, "tv", "2026", target_season=3),
            (True, "target_season_year"),
        )
        self.assertEqual(
            _source_year_matches_tmdb(detail, "movie", "2026", target_season=None),
            (False, "year_mismatch"),
        )

    def test_tavily_ai_tmdb_corroboration_rejects_different_strict_id(self):
        result, scraper, recognize, _ = self._run_tavily_ai_corroboration(
            strict_tmdb_id="99",
        )

        self.assertTrue(result.need_confirm)
        self.assertEqual(result.status, "low_confidence")
        self.assertEqual(recognize.call_count, 2)
        corroboration = result.ai_diagnostic["tmdb_revalidation"][
            "tavily_corroboration"
        ]
        self.assertEqual(corroboration["status"], "strict_tmdb_mismatch")
        self.assertFalse(corroboration["passed"])
        self.assertEqual(scraper.performance_snapshot()["tavily_hint_matches"], 0)

    def test_tavily_ai_tmdb_corroboration_rejects_unrelated_web_title(self):
        result, _scraper, recognize, _ = self._run_tavily_ai_corroboration(
            hint_titles=("Completely Unrelated Program",),
        )

        self.assertTrue(result.need_confirm)
        self.assertEqual(recognize.call_count, 1)
        corroboration = result.ai_diagnostic["tmdb_revalidation"][
            "tavily_corroboration"
        ]
        self.assertEqual(corroboration["status"], "insufficient_bridge_evidence")
        self.assertFalse(corroboration["passed"])

    def test_tavily_ai_tmdb_corroboration_does_not_override_conflicts(self):
        result, _scraper, recognize, web_search = self._run_tavily_ai_corroboration(
            rejected_constraints=["ambiguous_near_tie"],
        )

        self.assertTrue(result.need_confirm)
        self.assertEqual(recognize.call_count, 1)
        web_search.assert_not_called()
        corroboration = result.ai_diagnostic["tmdb_revalidation"][
            "tavily_corroboration"
        ]
        self.assertEqual(corroboration["status"], "deterministic_conflict")
        self.assertFalse(corroboration["passed"])

    def test_tavily_ai_tmdb_corroboration_unavailable_stays_manual(self):
        result, _scraper, recognize, web_search = self._run_tavily_ai_corroboration(
            hint_error=RuntimeError("offline"),
        )

        self.assertTrue(result.need_confirm)
        self.assertEqual(recognize.call_count, 1)
        web_search.assert_called_once()
        corroboration = result.ai_diagnostic["tmdb_revalidation"][
            "tavily_corroboration"
        ]
        self.assertEqual(corroboration["status"], "unavailable")
        self.assertFalse(corroboration["passed"])
        self.assertNotIn("offline", corroboration["error"])

    def test_only_temporarily_unavailable_ai_invokes_tavily_title_hints(self):
        from app.clients.ai_recognition import (
            AIRecognitionProviderError,
            AIRecognitionUnavailableError,
        )
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        values = {
            "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
            "AGENT_LLM_MODEL": "compatible-model",
            "AGENT_LLM_TIMEOUT_SECONDS": "8",
            "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
        }

        def failed_result():
            return RecognitionResult(
                media_type="movie",
                confidence=0.0,
                status="no_result",
                need_confirm=True,
                matched_by="search",
                error="TMDB 无搜索结果",
                context=RecognitionContext(
                    filename="Unknown.Release.2024.mkv",
                    normalized_title="Unknown Release",
                    filename_title="Unknown Release",
                    filename_year="2024",
                    media_type="movie",
                    title_variants=["Unknown Release"],
                ),
            )

        scraper = TMDBScraper(client=_TMDBClient())
        unavailable = failed_result()
        with patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch.object(
            scraper,
            "_recognize_with_ai_cache",
            side_effect=AIRecognitionUnavailableError("offline"),
        ), patch.object(
            scraper, "_tavily_title_hint_fallback", return_value=unavailable
        ) as tavily:
            result = scraper._ai_fallback(
                "Unknown.Release.2024.mkv", "", unavailable
            )
        self.assertIs(result, unavailable)
        tavily.assert_called_once()

        provider_error = failed_result()
        with patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch.object(
            scraper,
            "_recognize_with_ai_cache",
            side_effect=AIRecognitionProviderError("invalid provider response"),
        ), patch.object(scraper, "_tavily_title_hint_fallback") as tavily:
            result = scraper._ai_fallback(
                "Unknown.Release.2024.mkv", "", provider_error
            )
        self.assertIs(result, provider_error)
        tavily.assert_not_called()

    def test_tavily_title_hint_always_revalidates_with_strict_tmdb_mode(self):
        from app.modules.recognition_web_hints import RecognitionWebHintResult
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        scraper = TMDBScraper(client=_TMDBClient())
        scraper.match_mode = "loose"
        failed = RecognitionResult(
            media_type="movie",
            confidence=0.0,
            status="no_result",
            need_confirm=True,
            matched_by="search",
            error="TMDB 无搜索结果",
            context=RecognitionContext(
                filename="Corrected.Movie.2024.mkv",
                normalized_title="Corrected Movie",
                filename_title="Corrected Movie",
                filename_year="2024",
                media_type="movie",
                title_variants=["Corrected Movie"],
            ),
        )
        second = RecognitionResult(
            media_type="movie",
            confidence=0.0,
            status="no_result",
            need_confirm=True,
            matched_by="tavily_hint_search",
        )
        with patch(
            "app.modules.recognition_web_hints.search_recognition_titles",
            return_value=RecognitionWebHintResult(
                titles=("Corrected Movie",),
                attempted=True,
                status="matched",
            ),
        ), patch.object(
            scraper, "_recognize_context", return_value=second
        ) as recognize:
            result = scraper._tavily_title_hint_fallback(
                "Corrected.Movie.2024.mkv", "", failed
            )

        self.assertIs(result, failed)
        recognize.assert_called_once()
        self.assertEqual(recognize.call_args.kwargs["match_mode"], "strict")


class AIPreviewAndSettingsTests(unittest.TestCase):
    def test_preview_serializes_safe_ai_diagnostics(self):
        from app.modules.scraper import RecognitionResult
        from app.routes import tools_api

        result = RecognitionResult(
            tmdb_id="42", title="Corrected Movie", year="2024",
            media_type="movie", confidence=0.96, status="matched",
            matched_by="ai_search", ai_diagnostic={
                "attempted": True,
                "input": {"normalized_title": "Unknown Release"},
                "output": {
                    "title": "Corrected Movie", "year": 2024,
                    "media_type": "movie", "confidence": 0.95,
                },
                "confidence_threshold": 0.8,
                "position_guard": {"kept_season": 1},
                "tmdb_revalidation": {
                    "passed": False,
                    "source_anchor": {
                        "passed": False, "reason": "source_anchor_unverified",
                    },
                    "tavily_corroboration": {
                        "attempted": True,
                        "status": "matched",
                        "passed": False,
                        "source_web_score": 0.88,
                        "web_tmdb_score": 0.84,
                    },
                },
                "error": "",
            },
        )

        class PreviewScraper:
            match_mode = "strict"

            def parse_media(self, filename, parent_path="", match=None):
                return release_parse_result(
                    {"title": "Unknown", "year": "2024", "type": "movie"},
                    filename=filename, parent_path=parent_path,
                )

            def parse_resource_tags(self, filename):
                return {}

            def match(self, filename, parent_path=""):
                return result

            def get_detail(self, tmdb_id, media_type):
                return {}

        with patch.object(
            tools_api, "require_api_login", return_value=None
        ), patch.object(tools_api, "TMDBScraper", return_value=PreviewScraper()):
            response = tools_api.scrape_preview(
                Mock(), {"filename": "Unknown.Release.2024.mkv"}
            )

        payload = json.loads(response.body)
        self.assertTrue(payload["recognition"]["ai"]["attempted"])
        self.assertEqual(
            payload["recognition"]["ai"]["position_guard"]["kept_season"], 1
        )
        self.assertEqual(
            payload["recognition"]["ai"]["tmdb_revalidation"]["source_anchor"]["reason"],
            "source_anchor_unverified",
        )
        corroboration = payload["recognition"]["ai"]["tmdb_revalidation"][
            "tavily_corroboration"
        ]
        self.assertTrue(corroboration["attempted"])
        self.assertEqual(corroboration["status"], "matched")
        self.assertFalse(corroboration["passed"])
        self.assertEqual(corroboration["source_web_score"], 0.88)
        self.assertTrue(payload["need_confirm"])
        self.assertEqual(payload["diagnostic"]["status"], "low_confidence")
        self.assertFalse(payload["locked"])
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("api_key", rendered.lower())
        self.assertNotIn("secret", rendered.lower())

    def test_settings_and_logs_have_stable_ai_sections_without_key_rendering(self):
        settings = (Path("app/templates/settings.html").read_text(encoding="utf-8") + Path("app/static/js/settings.js").read_text(encoding="utf-8"))
        logs = (Path("app/templates/logs.html").read_text(encoding="utf-8") + Path("app/static/js/logs.js").read_text(encoding="utf-8"))
        css = Path("app/static/css/scrape-preview.css").read_text(encoding="utf-8")

        for key in (
            "AI_RECOGNITION_ENABLED",
            "AI_RECOGNITION_CONFIDENCE_THRESHOLD",
            "AI_RECOGNITION_REQUESTS_PER_MINUTE",
            "AI_RECOGNITION_DAILY_REQUEST_LIMIT",
            "AI_RECOGNITION_MAX_CONCURRENCY",
            "AI_RECOGNITION_CIRCUIT_BREAKER_SECONDS",
            "ORGANIZE_TAVILY_HINTS_ENABLED",
            "ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT",
        ):
            self.assertIn(f'data-key="{key}"', settings)
        for duplicate_key in (
            "AI_RECOGNITION_API_URL",
            "AI_RECOGNITION_API_KEY",
            "AI_RECOGNITION_MODEL",
            "AI_RECOGNITION_TIMEOUT_SECONDS",
        ):
            self.assertNotIn(f'data-key="{duplicate_key}"', settings)
        self.assertNotIn("复用 Media Agent 模型连接", settings)
        self.assertIn('type="password"', settings)
        self.assertIn("scrapeAiDiagnostic", logs)
        self.assertIn("二次 TMDB 严格评分与详情复核", logs)
        self.assertIn("scrape-lab-ai", css)
        self.assertIn("min-height", css)
        self.assertNotIn("AI_RECOGNITION_API_KEY", logs)
        self.assertIn("settings-agent.css') }}?v=20260829b", settings)


class AIRecognitionConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = SimpleNamespace(
            session={"logged_in": True},
            app=SimpleNamespace(
                state=SimpleNamespace(background_services_enabled=False)
            ),
        )

    @staticmethod
    def _payload(response):
        if isinstance(response, JSONResponse):
            return json.loads(response.body)
        return response

    def _save(self, payload):
        from app.routes.api import save_config

        # 该测试验证 API 归一化与持久化调用契约，不依赖开发机或 CI 的
        # 当前有效配置；否则相同值会被生产代码按 no-op 正确过滤。
        with patch(
            "app.routes.api.config.get",
            side_effect=lambda _key, default="": default,
        ), patch(
            "app.routes.api.config.has_external_override", return_value=False
        ), patch("app.routes.api.config.set_and_save") as persist, patch(
            "app.services.clear_dashboard_cache"
        ), patch(
            "app.modules.ai_recognition_governance.clear_ai_recognition_governance"
        ), patch(
            "app.modules.recognition_web_hints.clear_recognition_web_hint_cache"
        ), patch("app.modules.scheduler.get_scheduler"):
            response = save_config(self.request, payload)
        return response, persist

    def test_ai_governance_configuration_is_normalized(self):
        response, persist = self._save({
            "AI_RECOGNITION_ENABLED": "true",
            "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.82",
            "AI_RECOGNITION_REQUESTS_PER_MINUTE": "7",
            "AI_RECOGNITION_DAILY_REQUEST_LIMIT": "120",
            "AI_RECOGNITION_MAX_CONCURRENCY": "3",
            "AI_RECOGNITION_CIRCUIT_BREAKER_SECONDS": "75",
            "ORGANIZE_TAVILY_HINTS_ENABLED": "true",
            "ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT": "25",
        })

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({
            "AI_RECOGNITION_ENABLED": "1",
            "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.82",
            "AI_RECOGNITION_REQUESTS_PER_MINUTE": "7",
            "AI_RECOGNITION_DAILY_REQUEST_LIMIT": "120",
            "AI_RECOGNITION_MAX_CONCURRENCY": "3",
            "AI_RECOGNITION_CIRCUIT_BREAKER_SECONDS": "75",
            "ORGANIZE_TAVILY_HINTS_ENABLED": "1",
            "ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT": "25",
        })

    def test_removed_ai_provider_keys_are_rejected(self):
        for key in (
            "AI_RECOGNITION_API_URL",
            "AI_RECOGNITION_API_KEY",
            "AI_RECOGNITION_MODEL",
            "AI_RECOGNITION_TIMEOUT_SECONDS",
        ):
            with self.subTest(key=key):
                response, persist = self._save({key: "removed"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("不允许的配置项", self._payload(response)["error"])
                persist.assert_not_called()

    def test_invalid_ai_governance_limits_are_rejected(self):
        cases = [
            ({"AI_RECOGNITION_CONFIDENCE_THRESHOLD": "1.2"}, "0.5 到 0.99"),
            ({"AI_RECOGNITION_REQUESTS_PER_MINUTE": "0"}, "1 到 30"),
            ({"AI_RECOGNITION_DAILY_REQUEST_LIMIT": "0"}, "1 到 100000"),
            ({"AI_RECOGNITION_MAX_CONCURRENCY": "9"}, "1 到 8"),
            ({"AI_RECOGNITION_CIRCUIT_BREAKER_SECONDS": "9"}, "10 到 600"),
            ({"ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT": "0"}, "1 到 100000"),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload):
                response, persist = self._save(payload)
                body = self._payload(response)
                self.assertIn(message, body["error"])
                persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
