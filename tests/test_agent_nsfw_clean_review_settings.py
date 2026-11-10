"""光鸭 NSFW 清洗入库独立授权的配置与浏览器回归。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse
from starlette.requests import Request

from app import config
from app.routes.api import get_config, save_config

ROOT = Path(__file__).resolve().parents[1]
PARENT_KEY = "AGENT_RECOGNITION_REVIEW_ENABLED"
KEY = "AGENT_NSFW_CLEAN_REVIEW_ENABLED"
WAKE = "app.modules.organize_confirmations.wake_recognition_review_dispatcher"


class NsfwCleanReviewSettingsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.env_file = Path(directory) / "user.env"
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(config, "ENV_FILE", self.env_file))
        self.stack.enter_context(patch.object(config, "_cache", None))
        self.stack.enter_context(patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()))
        self.stack.enter_context(patch("app.modules.backup.config_snapshot_guard", side_effect=lambda _paths: nullcontext()))
        self.stack.enter_context(patch("app.services.clear_dashboard_cache"))
        self.invalidate = self.stack.enter_context(patch("app.agent.feature_gate.invalidate_agent_runtime_generation"))
        self.wake = self.stack.enter_context(patch(WAKE))
        config.write_env_file(self.env_file, {}, replace=False)
        from app.agent.feature_gate import initialize_runtime_fence
        from tests.support import isolated_test_database

        self.stack.enter_context(isolated_test_database())
        initialize_runtime_fence()
        self.request = Request({
            "type": "http",
            "session": {"logged_in": True},
            "app": SimpleNamespace(state=SimpleNamespace(
                background_services_enabled=False, media_proxy_manager=None,
            )),
        })

    def test_missing_child_permission_defaults_off_even_when_parent_enabled(self):
        config.set_and_save({PARENT_KEY: "1"})
        self.assertFalse(config.get_bool(KEY))
        self.assertEqual(get_config(self.request)[KEY], "0")
        self.assertNotIn(KEY, config.read_env_snapshot(self.env_file)[1])

    def test_child_boolean_is_persisted_and_hot_effective_before_dispatcher_wakes(self):
        config.set_and_save({PARENT_KEY: "1"})
        observed = []
        self.wake.side_effect = lambda: observed.append(config.get_bool(KEY))
        for raw, expected in ((True, "1"), ("off", "0"), ("yes", "1"), (False, "0")):
            with self.subTest(value=raw):
                response = save_config(self.request, {KEY: raw})
                self.assertEqual(response, {"success": True})
                self.assertEqual(config.read_env_snapshot(self.env_file)[1][KEY], expected)
                self.assertEqual(get_config(self.request)[KEY], expected)
                self.assertEqual(config.get(KEY), expected)
        self.assertEqual(observed, [True, False, True, False])
        self.assertEqual(self.invalidate.call_count, 4)

    def test_invalid_child_permission_cannot_persist_or_wake_dispatcher(self):
        before = self.env_file.read_bytes()
        for invalid in ("", "enabled", "2", None, [], {}):
            with self.subTest(value=invalid):
                response = save_config(self.request, {KEY: invalid})
                assert isinstance(response, JSONResponse)
                self.assertEqual(response.status_code, 400)
                self.assertIn(KEY, json.loads(bytes(response.body))["error"])
                self.assertEqual(self.env_file.read_bytes(), before)
        self.wake.assert_not_called()
        self.invalidate.assert_not_called()

    def test_unchanged_child_permission_does_not_republish_or_wake_dispatcher(self):
        config.set_and_save({KEY: "0"})
        with patch.object(config, "set_and_save", wraps=config.set_and_save) as persist:
            self.assertEqual(save_config(self.request, {KEY: "false"}), {"success": True})
        persist.assert_not_called()
        self.wake.assert_not_called()
        self.invalidate.assert_not_called()

    def test_saving_existing_parent_toggle_never_grants_child_permission(self):
        self.assertEqual(save_config(self.request, {PARENT_KEY: "1"}), {"success": True})
        self.assertEqual(get_config(self.request)[KEY], "0")
        self.assertNotIn(KEY, config.read_env_snapshot(self.env_file)[1])
        self.wake.assert_called_once_with()

    def test_child_setting_does_not_enable_parent_or_any_local_or_metatube_setting(self):
        config.set_and_save({PARENT_KEY: "0"})
        self.assertEqual(save_config(self.request, {KEY: "1"}), {"success": True})
        # 子开关仅保存独立授权，不暗中开启主开关；执行端还必须同时检查主开关。
        self.assertEqual(config.read_env_snapshot(self.env_file)[1], {PARENT_KEY: "0", KEY: "1"})
        self.wake.assert_called_once_with()

    def test_deployment_override_is_visible_locked_and_cannot_be_overwritten(self):
        config.set_and_save({KEY: "0"})
        with patch.dict(os.environ, {KEY: "1"}), patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset({KEY})):
            payload = get_config(self.request)
            self.assertEqual(payload[KEY], "1")
            self.assertIn(KEY, payload["__managed_fields"])
            response = save_config(self.request, {KEY: "0"})
        assert isinstance(response, JSONResponse)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(config.read_env_snapshot(self.env_file)[1][KEY], "0")
        self.wake.assert_not_called()

    def test_control_is_scoped_explained_accessible_and_not_conditionally_hidden(self):
        html = (ROOT / "app/templates/settings.html").read_text("utf-8")
        script = (ROOT / "app/static/js/settings.js").read_text("utf-8")
        metadata = html.split('id="settings-panel-metadata"', 1)[1].split('id="settings-panel-discovery"', 1)[0]
        option = metadata.split('class="metadata-option metadata-nsfw-clean-review"', 1)[1].split("</div>", 1)[0]
        for text in ("允许光鸭 NSFW 清洗入库", "无完整元数据", "授权 Agent", "自动执行", "不确定则保留人工确认", "不影响本地媒体或 MetaTube", "需先启用「Agent 主动复核」", "阻止后续文件开写", "已开写文件按原流程收尾"):
            self.assertIn(text, option)
        self.assertIn('aria-describedby="nsfwCleanReviewDescription nsfwCleanReviewDependency"', option)
        self.assertIn('aria-label="允许光鸭 NSFW 清洗入库"', option)
        from app.routes.api import _UI_DEFAULTS

        self.assertEqual(_UI_DEFAULTS[KEY], "0")
        self.assertNotIn(f"{KEY}:'1'", script)
        self.assertNotIn(" hidden", option)
        self.assertLess(metadata.index(f'data-key="{PARENT_KEY}"'), metadata.index(f'data-key="{KEY}"'))
        self.assertEqual(html.count(f'data-key="{KEY}"'), 1)


if __name__ == "__main__":
    unittest.main()
