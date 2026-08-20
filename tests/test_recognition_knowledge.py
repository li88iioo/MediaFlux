"""本地识别知识库、AI 双重验证学习与 UI/API 契约。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import database as db
from tests.support import IsolatedDatabaseTestCase


class RecognitionKnowledgeTestCase(IsolatedDatabaseTestCase):
    def setUp(self):
        from app.modules import recognition_knowledge as knowledge

        knowledge.reset_runtime_state_for_tests()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM recognition_knowledge")
        knowledge.reset_runtime_state_for_tests()


class RecognitionKnowledgePersistenceTests(RecognitionKnowledgeTestCase):
    def test_seed_is_idempotent_and_exposes_existing_release_groups(self):
        from app.modules import recognition_knowledge as knowledge

        knowledge.ensure_seed_knowledge()
        first = knowledge.list_entries()
        knowledge.ensure_seed_knowledge()
        second = knowledge.list_entries()
        self.assertGreater(first["summary"]["builtin"], 20)
        self.assertEqual(first["summary"]["total"], second["summary"]["total"])
        self.assertTrue(knowledge.is_known("Loli-House"))
        self.assertTrue(knowledge.is_known("Dynamis_One", "release_group"))
        self.assertTrue(knowledge.is_known("Shridhuu", "release_group"))
        self.assertTrue(knowledge.is_known("DreamHD", "release_suffix"))

    def test_user_crud_hot_reloads_and_rejects_alias_collision(self):
        from app.modules import recognition_knowledge as knowledge

        created = knowledge.create_entry({
            "knowledge_type": "release_group",
            "canonical_value": "ExampleSub",
            "aliases": ["Example-Sub"],
            "source": "user",
        })
        self.assertTrue(knowledge.is_known("Example-Sub"))
        updated = knowledge.update_entry(created["id"], {"disabled": True})
        self.assertTrue(updated["disabled"])
        self.assertFalse(knowledge.is_known("Example-Sub"))
        knowledge.update_entry(created["id"], {"disabled": False})
        with self.assertRaisesRegex(ValueError, "已被词条"):
            knowledge.create_entry({
                "knowledge_type": "release_group",
                "canonical_value": "OtherSub",
                "aliases": ["Example-Sub"],
                "source": "user",
            })
        self.assertTrue(knowledge.delete_entry(created["id"]))
        self.assertFalse(knowledge.is_known("ExampleSub"))

    def test_builtin_can_be_disabled_but_seed_upgrade_does_not_overwrite_user_choice(self):
        from app.modules import recognition_knowledge as knowledge

        builtin = next(
            item for item in knowledge.list_entries()["items"]
            if item["source"] == "builtin"
        )
        knowledge.update_entry(builtin["id"], {"disabled": True})
        with self.assertRaisesRegex(ValueError, "不能删除"):
            knowledge.delete_entry(builtin["id"])
        knowledge.reset_runtime_state_for_tests()
        knowledge.ensure_seed_knowledge()
        self.assertTrue(knowledge.get_entry(builtin["id"])["disabled"])


    def test_disabled_entry_blocks_ai_relearning_and_preserves_user_choice(self):
        from app.modules import recognition_knowledge as knowledge
        from app.modules.scraper import TMDBScraper

        created = knowledge.create_entry({
            "knowledge_type": "release_group",
            "canonical_value": "BlockedGroup",
            "aliases": ["Blocked-Group"],
            "source": "user",
            "disabled": True,
        })
        self.assertIsNotNone(knowledge.lookup_any("Blocked-Group"))
        self.assertIsNone(
            TMDBScraper._unknown_release_group_candidate(
                "[Blocked-Group] Correct Anime S01E01.mkv"
            )
        )
        learned = knowledge.record_learned_release_group(
            "BlockedGroup", confidence=0.99,
            evidence={"sample_key": "sample-a", "tmdb_id": "42"},
        )
        self.assertEqual(learned["id"], created["id"])
        self.assertEqual(learned["source"], "user")
        self.assertTrue(learned["disabled"])
        self.assertEqual(learned["success_count"], 0)
        self.assertEqual(
            [item for item in knowledge.list_entries()["items"] if item["source"] == "learned"],
            [],
        )

    def test_boolean_input_is_parsed_strictly(self):
        from app.modules import recognition_knowledge as knowledge

        created = knowledge.create_entry({
            "knowledge_type": "release_group",
            "canonical_value": "BooleanGroup",
            "disabled": "false",
        })
        self.assertFalse(created["disabled"])
        with self.assertRaisesRegex(ValueError, "布尔值"):
            knowledge.update_entry(created["id"], {"disabled": "not-a-bool"})

    def test_ai_learning_rejects_missing_sample_identity(self):
        from app.modules import recognition_knowledge as knowledge

        with self.assertRaisesRegex(ValueError, "样本标识"):
            knowledge.record_learned_release_group(
                "NoEvidenceGroup", confidence=0.99, evidence={}
            )
        self.assertEqual(
            [item for item in knowledge.list_entries()["items"] if item["source"] == "learned"],
            [],
        )

    def test_ai_learning_requires_two_distinct_verified_samples(self):
        from app.modules import recognition_knowledge as knowledge

        first = knowledge.record_learned_release_group(
            "NewGroup", confidence=0.98, aliases=["New Group"],
            evidence={"sample_key": "sample-a", "tmdb_id": "1"},
        )
        self.assertTrue(first["disabled"])
        self.assertFalse(knowledge.is_known("NewGroup"))
        duplicate = knowledge.record_learned_release_group(
            "NewGroup", confidence=0.99,
            evidence={"sample_key": "sample-a", "tmdb_id": "1"},
        )
        self.assertEqual(duplicate["success_count"], 1)
        second = knowledge.record_learned_release_group(
            "NewGroup", confidence=0.99,
            evidence={"sample_key": "sample-b", "tmdb_id": "2"},
        )
        self.assertFalse(second["disabled"])
        self.assertEqual(second["success_count"], 2)
        self.assertTrue(knowledge.is_known("New Group"))


class RecognitionKnowledgeScraperTests(RecognitionKnowledgeTestCase):
    def test_seeded_group_is_removed_but_bracketed_title_is_preserved(self):
        from app.modules.recognition_knowledge import ensure_seed_knowledge
        from app.modules.scraper import extract_recognition_context

        ensure_seed_knowledge()
        known = extract_recognition_context(
            "[LoliHouse] Example Show - 03 [1080p].mkv", "/Anime"
        )
        title = extract_recognition_context(
            "[The] Last of Us S01E01 [1080p].mkv", "/TV"
        )
        self.assertIn("[LoliHouse]", known.cleaned_components["release_prefixes"])
        self.assertNotIn("LoliHouse", known.normalized_title)
        self.assertEqual(title.cleaned_components["release_prefixes"], [])
        self.assertIn("The Last of Us", title.normalized_title)

    def test_manual_confirmation_learns_release_group_from_distinct_packages_only(self):
        from app.modules import recognition_knowledge as knowledge
        from app.modules.scraper import TMDBScraper

        scraper = TMDBScraper(client=Mock())
        first_parent = "/Anime/Fresh Manual Package A"
        second_parent = "/Anime/Fresh Manual Package B"
        scraper.confirm(
            "[NekoEncodeX] Correct Anime - 01 [1080p].mkv",
            "42", "正确动画", "2024", "tv", first_parent,
        )
        scraper.confirm(
            "[NekoEncodeX] Correct Anime - 02 [1080p].mkv",
            "42", "正确动画", "2024", "tv", first_parent,
        )
        learned = next(
            item for item in knowledge.list_entries()["items"]
            if item["source"] == "learned"
            and item["canonical_value"] == "NekoEncodeX"
        )
        self.assertEqual(learned["success_count"], 1)
        self.assertTrue(learned["disabled"])

        scraper.confirm(
            "[NekoEncodeX] Correct Anime - 01 [BDRip].mkv",
            "42", "正确动画", "2024", "tv", second_parent,
        )
        learned = next(
            item for item in knowledge.list_entries()["items"]
            if item["source"] == "learned"
            and item["canonical_value"] == "NekoEncodeX"
        )
        self.assertEqual(learned["success_count"], 2)
        self.assertFalse(learned["disabled"])
        self.assertTrue(knowledge.is_known("NekoEncodeX"))

    def test_ai_group_retry_is_immediate_but_learning_remains_pending(self):
        from app.clients.ai_recognition import AIReleaseGroupResult
        from app.modules.recognition_knowledge import list_entries
        from app.modules.scraper import RecognitionContext, RecognitionResult, TMDBScraper

        scraper = TMDBScraper(client=Mock())
        initial = RecognitionResult(status="no_result")
        matched = RecognitionResult(
            tmdb_id="42", title="正确动画", year="2024", media_type="tv",
            confidence=0.97, status="matched", need_confirm=False,
            context=RecognitionContext(filename="Correct Anime S01E01.mkv", normalized_title="Correct Anime", media_type="tv"),
        )
        values = {
            "AGENT_LLM_API_URL": "https://ai.example/v1/chat/completions",
            "AGENT_LLM_MODEL": "test-model",
            "AGENT_LLM_API_KEY": "",
            "PROXY_URL": "",
        }
        with patch("app.modules.scraper.get_bool", return_value=True), patch(
            "app.modules.scraper.get", side_effect=lambda key, default="": values.get(key, default)
        ), patch.object(
            scraper, "_classify_release_group_with_ai_cache",
            return_value=AIReleaseGroupResult(True, "New Fansub", ("New-Fansub",), 0.99),
        ), patch.object(scraper, "deterministic_recognize", return_value=matched):
            result = scraper._release_group_fallback(
                "[NekoEncode] Correct Anime S01E01.mkv", "/Anime", initial,
                media_type_hint="tv",
            )
        self.assertEqual(result.matched_by, "ai_release_group")
        learned = [item for item in list_entries()["items"] if item["source"] == "learned"]
        self.assertEqual(len(learned), 1)
        self.assertTrue(learned[0]["disabled"])
        self.assertEqual(learned[0]["canonical_value"], "NekoEncode")


class RecognitionKnowledgeApiTests(RecognitionKnowledgeTestCase):
    @staticmethod
    def request():
        request = Mock()
        request.session = {"logged_in": True}
        return request

    @staticmethod
    def payload(response):
        return json.loads(response.body.decode("utf-8"))

    def test_authenticated_api_crud_and_builtin_delete_guard(self):
        from app.routes.recognition_knowledge_api import (
            create_recognition_knowledge_api, delete_recognition_knowledge_api,
            list_recognition_knowledge_api, update_recognition_knowledge_api,
        )

        request = self.request()
        listed = self.payload(list_recognition_knowledge_api(request, q="", knowledge_type="", limit=50))
        self.assertGreater(listed["summary"]["builtin"], 0)
        created_response = create_recognition_knowledge_api(request, {
            "knowledge_type": "release_group", "canonical_value": "ApiSub",
            "aliases": ["Api-Sub"], "source": "builtin",
        })
        self.assertEqual(created_response.status_code, 201)
        created = self.payload(created_response)
        self.assertEqual(created["source"], "user")
        updated = self.payload(update_recognition_knowledge_api(
            request, created["id"], {"disabled": True, "source": "builtin"}
        ))
        self.assertEqual(updated["source"], "user")
        self.assertTrue(updated["disabled"])
        self.assertEqual(delete_recognition_knowledge_api(request, created["id"]).status_code, 200)
        builtin = listed["items"][0]
        if builtin["source"] != "builtin":
            builtin = next(item for item in listed["items"] if item["source"] == "builtin")
        guarded = delete_recognition_knowledge_api(request, builtin["id"])
        self.assertEqual(guarded.status_code, 400)


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload


class _Session:
    def __init__(self, result):
        self.result = result
        self.kwargs = None
        self.proxies = {}

    def post(self, *args, **kwargs):
        self.kwargs = kwargs
        return _Response({"choices": [{"message": {"content": json.dumps(self.result)}}]})


class AIReleaseGroupClientTests(unittest.TestCase):
    def test_release_group_classifier_uses_strict_independent_schema(self):
        from app.clients.ai_recognition import AIRecognitionClient, AIReleaseGroupInput

        session = _Session({
            "is_release_group": True,
            "canonical_name": "ExampleSub",
            "aliases": ["Example-Sub"],
            "confidence": 0.98,
        })
        result = AIRecognitionClient(
            api_url="https://ai.example/v1/chat/completions", model="test", session=session,
        ).classify_release_group(AIReleaseGroupInput("Example-Sub", "Example Show S01E01"))
        schema = session.kwargs["json"]["response_format"]["json_schema"]
        self.assertEqual(schema["name"], "release_group_classification")
        self.assertTrue(schema["strict"])
        self.assertTrue(result.is_release_group)
        self.assertEqual(result.confidence, 0.98)


class RecognitionKnowledgeUiContractTests(unittest.TestCase):
    def test_rules_page_exposes_local_knowledge_ledger_without_layout_replacement(self):
        html = (Path("app/templates/organize.html").read_text(encoding="utf-8") + Path("app/static/js/organize.js").read_text(encoding="utf-8") + Path("app/static/css/organize.css").read_text(encoding="utf-8"))
        for marker in (
            'id="openRecognitionKnowledgeBtn"',
            'id="recognitionKnowledgeModal"',
            'id="recognitionKnowledgeTableBody"',
            'id="recognitionKnowledgeForm"',
            "正在刷新，保留当前列表",
            "recognitionKnowledgeRequestSerial",
            "/api/tools/recognition-knowledge",
        ):
            self.assertIn(marker, html)
