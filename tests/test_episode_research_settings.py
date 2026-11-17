"""受控季集研究设置的真实配置 API、持久化与静态 UI 回归。"""
from __future__ import annotations

import os
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config
from app.agent.feature_gate import current_agent_runtime_generation
from app.defaults import DEFAULT_AGENT_EPISODE_RESEARCH_DAILY_LIMIT, DEFAULT_BOOL_CONFIG_VALUES
from app.main import create_app
from tests.support import initialized_web_runtime
from tests.test_config_empty_values_api import login

ROOT = Path(__file__).resolve().parents[1]
ENABLED = "AGENT_EPISODE_RESEARCH_ENABLED"
LIMIT = "AGENT_EPISODE_RESEARCH_DAILY_LIMIT"
PARENT = "AGENT_RECOGNITION_REVIEW_ENABLED"


class EpisodeResearchSettingsTests(unittest.TestCase):
    def setUp(self):
        # 真实发布会同步进程环境，用例退出时恢复之前的变量。
        self.enterContext(patch.dict(os.environ))
        for key in (ENABLED, LIMIT, PARENT, "AGENT_LLM_ENABLED", "TAVILY_DAILY_CREDIT_LIMIT", "WEB_SEARCH_ENABLED"):
            os.environ.pop(key, None)
        self.env_file = self.enterContext(initialized_web_runtime()).env_file
        self.client = TestClient(create_app())
        self.addCleanup(self.client.close)
        self.headers = login(self.client)
        self.wake = self.enterContext(patch("app.modules.organize_confirmations.wake_recognition_review_dispatcher"))

    def _post_config(self, payload: dict[str, object]):
        return self.client.post("/api/config", json=payload, headers=self.headers)

    def _assert_saved(self, payload: dict[str, object]) -> None:
        response = self._post_config(payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"success": True})

    def _get_config(self) -> dict[str, Any]:
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _file_values(self) -> dict[str, str]:
        return config.read_env_snapshot(self.env_file)[1]

    def _publication_state(self) -> tuple[bytes, int]:
        return self.env_file.read_bytes(), current_agent_runtime_generation()

    def test_shared_defaults_and_missing_config_are_independently_off(self):
        self.assertIs(DEFAULT_BOOL_CONFIG_VALUES[ENABLED], False)
        self.assertEqual(DEFAULT_AGENT_EPISODE_RESEARCH_DAILY_LIMIT, 10)
        config.set_and_save({PARENT: "1", "AGENT_LLM_ENABLED": "1"})
        before = self._publication_state()
        payload = self._get_config()
        self.assertEqual(payload[ENABLED], "0")
        self.assertEqual(payload[LIMIT], "10")
        self.assertTrue({ENABLED, LIMIT}.isdisjoint(self._file_values()))
        self.assertEqual(self._publication_state(), before)

    def test_boolean_normalization_roundtrips_without_enabling_dependencies(self):
        config.set_and_save({"AGENT_LLM_ENABLED": None})
        before = self._file_values()
        initial_generation = current_agent_runtime_generation()
        for raw, expected in ((True, "1"), (False, "0"), (" on ", "1"), ("off", "0"), ("YES", "1"), ("n", "0")):
            with self.subTest(value=raw):
                self._assert_saved({ENABLED: raw})
                self.assertEqual(self._get_config()[ENABLED], expected)
                self.assertEqual(self._file_values(), before | {ENABLED: expected})
        self.assertEqual(current_agent_runtime_generation(), initial_generation + 6)
        self.assertEqual(self.wake.call_count, 6)

    def test_valid_daily_limits_roundtrip_as_canonical_integers(self):
        before = self._file_values()
        for raw, expected in ((1, "1"), (100, "100"), (10, "10"), (" 07 ", "7")):
            with self.subTest(value=raw):
                self._assert_saved({LIMIT: raw})
                self.assertEqual(self._get_config()[LIMIT], expected)
                self.assertEqual(self._file_values(), before | {LIMIT: expected})

    def test_invalid_boolean_never_publishes(self):
        before = self._publication_state()
        for raw in ("", "enabled", "2", 2, None, [], {}):
            with self.subTest(value=raw):
                response = self._post_config({ENABLED: raw, LIMIT: 10})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn(ENABLED, response.json()["error"])
                self.assertEqual(self._publication_state(), before)
        self.wake.assert_not_called()

    def test_invalid_daily_limit_rejects_entire_update(self):
        before = self._publication_state()
        for raw in (0, -1, 101, "", " ", None, False, True, 1.5, 10.0, "1.0", "NaN", "inf", "1e1", [], {}):
            with self.subTest(value=raw):
                response = self._post_config({ENABLED: "1", LIMIT: raw})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn(LIMIT, response.json()["error"])
                self.assertEqual(self._publication_state(), before)
        self.wake.assert_not_called()

    def test_parent_toggle_and_existing_quota_do_not_grant_child_permission(self):
        config.set_and_save({"TAVILY_DAILY_CREDIT_LIMIT": "23", "WEB_SEARCH_ENABLED": "0", "AGENT_LLM_ENABLED": None})
        before = self._file_values()
        self._assert_saved({PARENT: "1"})
        self.assertEqual(self._get_config()[ENABLED], "0")
        self.assertEqual(self._file_values(), before | {PARENT: "1"})
        self._assert_saved({ENABLED: "1", LIMIT: "10"})
        self.assertEqual(self._file_values(), before | {PARENT: "1", ENABLED: "1", LIMIT: "10"})

    def test_unchanged_normalized_values_do_not_publish_or_wake(self):
        config.set_and_save({ENABLED: "0", LIMIT: "10"})
        before = self._publication_state()
        self._assert_saved({ENABLED: "false", LIMIT: " 10 "})
        self.assertEqual(self._publication_state(), before)
        self.wake.assert_not_called()

    def test_environment_overrides_are_visible_and_locked_for_both_keys(self):
        config.set_and_save({ENABLED: "0", LIMIT: "10"})
        before = self._publication_state()
        for key, deployed, attempted in ((ENABLED, "1", "0"), (LIMIT, "25", "10")):
            with (
                self.subTest(key=key),
                patch.dict(os.environ, {key: deployed}),
                patch.object(config, "_STARTUP_ENV_OVERRIDES", config._STARTUP_ENV_OVERRIDES | {key}),
            ):
                payload = self._get_config()
                self.assertEqual(payload[key], deployed)
                self.assertIn(key, payload["__managed_fields"])
                response = self._post_config({key: attempted})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn(key, response.json()["error"])
                self.assertEqual(self._publication_state(), before)
        self.wake.assert_not_called()


class InputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and "data-key" in attrs:
            self.inputs[attrs["data-key"]] = attrs


class EpisodeResearchSettingsUiTests(unittest.TestCase):
    def test_controls_reuse_settings_rows_and_keep_stable_accessible_placeholders(self):
        html = (ROOT / "app/templates/settings.html").read_text("utf-8")
        metadata = html.split('id="settings-panel-metadata"', 1)[1].split('id="settings-panel-discovery"', 1)[0]
        parser = InputParser()
        parser.feed(metadata)
        self.assertIn(ENABLED, parser.inputs)
        self.assertIn(LIMIT, parser.inputs)
        toggle, limit = parser.inputs[ENABLED], parser.inputs[LIMIT]
        self.assertEqual(toggle["type"], "checkbox")
        self.assertNotIn("checked", toggle)
        self.assertIn("disabled", toggle)
        self.assertEqual(toggle["aria-describedby"], "episodeResearchDescription episodeResearchDependency")
        for key, value in (("type", "number"), ("min", "1"), ("max", "100"), ("step", "1")):
            self.assertEqual(limit[key], value)
        self.assertNotIn("value", limit, "数值由后端配置回填，模板不持有另一份初值")
        self.assertIn("disabled", limit)
        self.assertIn("aria-label", toggle)
        self.assertIn("aria-describedby", limit)
        self.assertLess(metadata.index(f'data-key="{PARENT}"'), metadata.index(f'data-key="{ENABLED}"'))
        self.assertLess(metadata.index(f'data-key="{ENABLED}"'), metadata.index(f'data-key="{LIMIT}"'))
        for key in (ENABLED, LIMIT):
            self.assertEqual(html.count(f'data-key="{key}"'), 1)
        option = metadata.split('class="metadata-option metadata-episode-research"', 1)[1].split('</div>', 1)[0]
        self.assertIn('class="toggle"', option)
        self.assertIn('.metadata-episode-research .toggle { width: 44px; height: 44px; }', html)
        self.assertIn('.metadata-episode-research input:focus-visible + .toggle-slider', html)
        self.assertNotIn(" hidden", option)
        self.assertNotIn('style=', option)
        for text in ("默认关闭", "光鸭待确认剧集", "冻结案例", "无冲突新归档", "不自动覆盖或删除目标", "本地媒体继续旧复核", "首版不执行本地文件映射", "TMDB 剧集组", "唯一", "可验证", "歧义", "待确认", "Tavily", "可选", "现有额度", "Agent 主动复核", "模型地址", "模型名"):
            self.assertIn(text, option)



    def test_formal_configuration_guide_explains_authorization_and_shared_search_budget(self):
        guide = (ROOT / "docs/配置参考.md").read_text("utf-8")
        self.assertIn(f"| `{ENABLED}` | `0` |", guide)
        self.assertIn(f"| `{LIMIT}` | `10` |", guide)
        section = guide.split(f"| `{ENABLED}`", 1)[1].split("光鸭 NSFW 子授权位于", 1)[0]
        for text in ("1–100", "默认关闭", "光鸭待确认剧集", "冻结案例", "无冲突新归档", "不自动覆盖或删除目标", "local_media", "继续旧复核", "首版未实现本地文件映射执行", "AGENT_RECOGNITION_REVIEW_ENABLED", "AGENT_LLM_ENABLED", "模型地址", "模型名", "TMDB 剧集组", "唯一", "稳定 episode ID", "Tavily", "可选", "TAVILY_DAILY_CREDIT_LIMIT", "待确认", "网页"):
            self.assertIn(text, section)


if __name__ == "__main__":
    unittest.main()
