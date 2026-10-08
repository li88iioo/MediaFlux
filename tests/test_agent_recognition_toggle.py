"""单条识别规则启停的参数、确认、陈旧快照与自然语言路由回归。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app import database as db
from app.agent.rate_limit import agent_rate_limiter
from app.modules.recognition import formats
from app.modules import (
    recognition_knowledge,
    recognition_preprocess_rules,
    tmdb_regex_rules,
)
from tests.agent_kernel_test_harness import (
    get_kernel_test_service as get_agent_service,
)
from tests.agent_kernel_test_harness import (
    reset_kernel_test_service as reset_agent_service_for_tests,
)
from tests.support import IsolatedDatabaseTestCase


class AgentRecognitionToggleTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        recognition_preprocess_rules.invalidate_active_cache()
        recognition_knowledge.reset_runtime_state_for_tests()
        recognition_knowledge.ensure_seed_knowledge()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM recognition_preprocess_rules")
            conn.execute("DELETE FROM tmdb_regex_rules")
            conn.execute("DELETE FROM recognition_knowledge")
            conn.execute("DELETE FROM agent_action_history")
            conn.execute("DELETE FROM recognition_format_rules")
        formats.invalidate_cache()
        recognition_knowledge.invalidate_active_cache()
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    def tearDown(self) -> None:
        formats.invalidate_cache()
        recognition_preprocess_rules.invalidate_active_cache()
        recognition_knowledge.reset_runtime_state_for_tests()
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _preprocess_rule(*, disabled: bool = False) -> dict:
        return recognition_preprocess_rules.create_rule(
            {
                "name": "SECRET preprocess rule",
                "matcher_type": "text",
                "pattern": "SECRET_PATTERN",
                "scope": "filename",
                "action": "replace",
                "replacement": "SECRET_REPLACEMENT",
                "numeric_value": None,
                "priority": 50,
                "disabled": disabled,
            }
        )

    @staticmethod
    def _regex_rule(*, disabled: bool = False) -> dict:
        return tmdb_regex_rules.create_rule(
            {
                "name": "SECRET regex rule",
                "pattern": "SECRET_REGEX",
                "match_target": "filename",
                "tmdb_id": "987654",
                "media_type": "tv",
                "season_override": 2,
                "priority": 100,
                "disabled": disabled,
            }
        )

    @staticmethod
    def _knowledge_entry(*, disabled: bool = False) -> dict:
        return recognition_knowledge.create_entry(
            {
                "knowledge_type": "release_group",
                "canonical_value": "SecretGroup",
                "aliases": ["Secret-Group"],
                "source": "user",
                "disabled": disabled,
            }
        )

    @staticmethod
    def _serialized(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    def _assert_private_values_absent(self, value: object) -> None:
        serialized = self._serialized(value)
        for private in (
            "SECRET preprocess rule",
            "SECRET_PATTERN",
            "SECRET_REPLACEMENT",
            "SECRET regex rule",
            "SECRET_REGEX",
            "987654",
            "SecretGroup",
            "Secret-Group",
        ):
            self.assertNotIn(private, serialized)

    def test_confirmed_changes_are_single_row_and_outputs_are_safe(self):
        preprocess = self._preprocess_rule()
        regex = self._regex_rule()
        knowledge = self._knowledge_entry()
        service = get_agent_service()
        cases = (
            ("preprocess_rule", preprocess["id"], "recognition_preprocess_rules"),
            ("tmdb_regex_rule", regex["id"], "tmdb_regex_rules"),
            ("knowledge_entry", knowledge["id"], "recognition_knowledge"),
        )
        for rule_type, rule_id, table in cases:
            with self.subTest(rule_type=rule_type):
                prepared = service.prepare(
                    "recognition.set_rule_enabled",
                    {"rule_type": rule_type, "rule_id": rule_id, "enabled": False},
                    owner=f"owner-{rule_type}",
                )
                with db.get_conn() as conn:
                    before = conn.execute(
                        f"SELECT disabled FROM {table} WHERE id=?", (rule_id,)
                    ).fetchone()
                self.assertEqual(int(before["disabled"]), 0)
                confirmed = service.confirm(
                    prepared["action_plan"]["plan_id"], owner=f"owner-{rule_type}"
                )
                with db.get_conn() as conn:
                    after = conn.execute(
                        f"SELECT disabled, * FROM {table} WHERE id=?", (rule_id,)
                    ).fetchone()
                self.assertEqual(int(after["disabled"]), 1)
                if rule_type == "knowledge_entry":
                    self.assertEqual(int(after["user_modified"]), 1)
                self.assertEqual(confirmed["result"]["status"], "completed")
                self.assertEqual(confirmed["result"]["data"]["affected"], 1)
                self._assert_private_values_absent(
                    {"prepared": prepared, "confirmed": confirmed}
                )

    def test_runtime_cache_is_invalidated_for_cached_rule_types(self):
        preprocess = self._preprocess_rule()
        service = get_agent_service()
        prepared = service.prepare(
            "recognition.set_rule_enabled",
            {
                "rule_type": "preprocess_rule",
                "rule_id": preprocess["id"],
                "enabled": False,
            },
            owner="owner-preprocess",
        )
        with patch(
            "app.agent.recognition_toggle_actions.recognition_preprocess_rules.invalidate_active_cache"
        ) as invalidate:
            service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner-preprocess"
            )
        invalidate.assert_called_once_with()
        knowledge = self._knowledge_entry()
        reset_agent_service_for_tests()
        service = get_agent_service()
        prepared = service.prepare(
            "recognition.set_rule_enabled",
            {
                "rule_type": "knowledge_entry",
                "rule_id": knowledge["id"],
                "enabled": False,
            },
            owner="owner-knowledge",
        )
        with patch(
            "app.agent.recognition_toggle_actions.recognition_knowledge.invalidate_active_cache"
        ) as invalidate:
            service.confirm(prepared["action_plan"]["plan_id"], owner="owner-knowledge")
        invalidate.assert_called_once_with()

    def test_confirmation_rejects_stale_identity_change(self):
        rule = self._regex_rule()
        service = get_agent_service()
        prepared = service.prepare(
            "recognition.set_rule_enabled",
            {"rule_type": "tmdb_regex_rule", "rule_id": rule["id"], "enabled": False},
            owner="owner",
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE tmdb_regex_rules SET name=?,updated_at=? WHERE id=?",
                ("SECRET changed name", db.now(), rule["id"]),
            )
        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(confirmed["result"]["status"], "conflict")
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT disabled FROM tmdb_regex_rules WHERE id=?", (rule["id"],)
            ).fetchone()
        self.assertEqual(int(row["disabled"]), 0)
        self.assertNotIn("SECRET changed name", self._serialized(confirmed))

    def test_release_format_toggle_uses_native_change_and_refreshes_parser(self):
        from tests.test_release_formats import save_teaching, filename, PARENT
        from app.modules.scraper import extract_recognition_context
        item, _ = save_teaching()
        unrelated = self._preprocess_rule()
        service = get_agent_service()
        self.assertEqual(extract_recognition_context(filename(15), PARENT).episode, 15)
        for enabled, expected_episode in ((False, None), (True, 15)):
            prepared = service.prepare("recognition.set_rule_enabled", {
                "rule_type": "release_format", "rule_id": item["id"], "enabled": enabled,
            }, owner="format-owner")
            revision = formats.list_rules()[0]["revision"]
            with patch.object(formats, "change", wraps=formats.change) as change:
                result = service.confirm(prepared["action_plan"]["plan_id"], owner="format-owner")
            self.assertTrue(result["result"]["ok"])
            change.assert_called_once_with(item["id"], {"revision": revision, "disabled": not enabled})
            self.assertEqual(formats.list_rules()[0]["revision"], revision + 1)
            self.assertEqual(extract_recognition_context(filename(15), PARENT).episode, expected_episode)
            with db.get_conn() as conn:
                self.assertEqual(conn.execute("SELECT disabled FROM recognition_preprocess_rules WHERE id=?", (unrelated["id"],)).fetchone()[0], 0)

    def test_release_format_stale_confirmation_does_not_replay(self):
        from tests.test_release_formats import save_teaching
        item, _ = save_teaching()
        service = get_agent_service()
        prepared = service.prepare("recognition.set_rule_enabled", {
            "rule_type": "release_format", "rule_id": item["id"], "enabled": False,
        }, owner="format-owner")
        formats.change(item["id"], {"revision": item["revision"], "disabled": True})
        result = service.confirm(prepared["action_plan"]["plan_id"], owner="format-owner")
        self.assertFalse(result["result"]["ok"])
        self.assertEqual(result["result"]["status"], "conflict")
        self.assertTrue(formats.list_rules()[0]["disabled"])
        self.assertEqual(formats.list_rules()[0]["revision"], item["revision"] + 1)

    def test_release_format_change_after_agent_check_is_rejected_by_native_revision(self):
        from tests.test_release_formats import save_teaching
        item, _ = save_teaching()
        service = get_agent_service()
        prepared = service.prepare("recognition.set_rule_enabled", {
            "rule_type": "release_format", "rule_id": item["id"], "enabled": False,
        }, owner="format-owner")
        real_change = formats.change
        def concurrent_change(rule_id, value):
            real_change(rule_id, {"revision": item["revision"], "disabled": True})
            return real_change(rule_id, value)
        with patch.object(formats, "change", side_effect=concurrent_change):
            result = service.confirm(prepared["action_plan"]["plan_id"], owner="format-owner")
        self.assertEqual(result["result"]["status"], "conflict")
        self.assertTrue(formats.list_rules()[0]["disabled"])
        self.assertEqual(formats.list_rules()[0]["revision"], item["revision"] + 1)

    def test_release_format_reenable_obeys_native_conflict_replay(self):
        from tests.test_release_formats import save_teaching, teaching
        item, _ = save_teaching()
        formats.change(item["id"], {"revision": item["revision"], "disabled": True})
        alternate = teaching()
        alternate["draft"]["template"] = alternate["draft"]["template"].replace("track{episode}r{version}", "track0{version}r{episode}")
        alternate["examples"][0]["episode"] = 2
        alternate["examples"][1]["filename"] = alternate["examples"][1]["filename"].replace("r2]", "r3]")
        alternate["examples"][1]["episode"] = 3
        alternate["filenames"] = []
        other, _ = save_teaching(alternate)
        service = get_agent_service()
        prepared = service.prepare("recognition.set_rule_enabled", {
            "rule_type": "release_format", "rule_id": item["id"], "enabled": True,
        }, owner="format-owner")
        result = service.confirm(prepared["action_plan"]["plan_id"], owner="format-owner")
        self.assertEqual(result["result"]["status"], "conflict")
        stored = {r["id"]: r for r in formats.list_rules()}
        self.assertTrue(stored[item["id"]]["disabled"])
        self.assertFalse(stored[other["id"]]["disabled"])
