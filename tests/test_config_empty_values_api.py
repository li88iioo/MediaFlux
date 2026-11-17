"""配置 GET/POST 对缺失、显式空值与 Cron 的契约：真实安装、真实 user.env 与接口。

缺失键回填后端唯一默认；布尔开关与 Cron 的显式空值在运行时就是「关闭 / 无计划」，
页面必须如实看到空值；通用保存接口拒绝提交空或无效的 Cron，且整单不发布、不重载。
"""
from __future__ import annotations

import os
import re
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config
from app.defaults import DEFAULT_SCHEDULE_CRON
from app.main import create_app
from tests.support import initialized_web_runtime

DEFAULT_ON = (
    "GY_ORGANIZE_EMBY_REFRESH", "GY_ORGANIZE_REGION_SPLIT", "GY_ORGANIZE_CLEAN_EMPTY",
    "GY_ORGANIZE_NOTIFY_ENABLED", "STRM_NOTIFY_ENABLED",
)
CRON_KEYS = ("STRM_SCHEDULE_CRON", "GY_ORGANIZE_SCHEDULE_CRON")


def _csrf_token(response) -> str:
    match = re.search(r'name="csrf[-_]token" (?:content|value)="([^"]+)"', response.text)
    assert match, "missing csrf token"
    return match.group(1)


def login(client: TestClient) -> dict[str, str]:
    """真实登录流程，返回后续写请求需要的 CSRF 头。"""
    username, password = config.web_credentials()
    response = client.post("/login", data={
        "csrf_token": _csrf_token(client.get("/login")), "username": username, "password": password,
    }, follow_redirects=False)
    assert response.status_code == 302, response.text
    return {"X-CSRF-Token": _csrf_token(client.get("/settings"))}


def write_raw(**values: str) -> None:
    """走真实发布器同步文件与运行时；由外层安装夹具恢复环境。"""
    config.set_and_save(values)


class ConfigEmptyValueApiTests(unittest.TestCase):
    def setUp(self):
        # 每个用例一套全新的真实安装，用例之间不改写文件、不绕过发布器快照。
        self.enterContext(initialized_web_runtime())
        self.client = TestClient(create_app(), raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.headers = login(self.client)

    _write_raw = staticmethod(write_raw)

    def _config(self) -> dict:
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_telegram_revocation_and_reenable_follow_the_effective_agent_switch(self):
        self._write_raw(
            AGENT_LLM_ENABLED="1", TG_AGENT_ENABLED="1", TG_NOTIFICATION_ENABLED="0",
            TG_BOT_TOKEN="123:fixture-token", TG_CHAT_ID="456", TG_AGENT_ALLOWED_USER_IDS="9",
        )
        revoke = {"__clear_secrets": ["TG_BOT_TOKEN"], "TG_AGENT_ALLOWED_USER_IDS": ""}
        refused = self.client.post("/api/config", json=revoke, headers=self.headers)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("Bot Token", refused.json()["error"])

        disabled = self.client.post("/api/config", json={"AGENT_LLM_ENABLED": "0", **revoke}, headers=self.headers)
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(config.get("TG_BOT_TOKEN"), "")
        self.assertEqual(config.get("TG_AGENT_ALLOWED_USER_IDS"), "")
        self.assertTrue(config.get_bool("TG_AGENT_ENABLED"))

        for restore, missing in (({}, "Bot Token"), ({"TG_BOT_TOKEN": "123:fixture-token"}, "允许的用户 ID")):
            with self.subTest(missing=missing):
                refused = self.client.post(
                    "/api/config", json={"AGENT_LLM_ENABLED": "1", **restore}, headers=self.headers,
                )
                self.assertEqual(refused.status_code, 400)
                self.assertIn(missing, refused.json()["error"])
                self.assertFalse(config.get_bool("AGENT_LLM_ENABLED"))
                self.assertEqual(config.get("TG_BOT_TOKEN"), "")

        enabled = self.client.post(
            "/api/config", json={"AGENT_LLM_ENABLED": "1", "TG_AGENT_ENABLED": "0"}, headers=self.headers,
        )
        self.assertEqual(enabled.status_code, 200)
        self.assertTrue(config.get_bool("AGENT_LLM_ENABLED"))
        self.assertFalse(config.get_bool("TG_AGENT_ENABLED"))

    def test_indexer_switch_without_a_site_rejects_publish_and_accepts_site_in_same_save(self):
        from app.indexers.config import current_indexer_site_ids

        self._write_raw(INDEXER_SEARCH_ENABLED="0", INDEXER_ENABLED_SITES="nyaa")
        cleared = self.client.post("/api/config", json={"INDEXER_ENABLED_SITES": ""}, headers=self.headers)
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(current_indexer_site_ids(), ())
        before = config.ENV_FILE.read_bytes()
        refused = self.client.post("/api/config", json={"INDEXER_SEARCH_ENABLED": "1"}, headers=self.headers)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("至少选择一个资源站点", refused.json()["error"])
        self.assertEqual(config.ENV_FILE.read_bytes(), before)
        self.assertEqual(self.client.get("/api/indexers/sites").status_code, 404)
        enabled = self.client.post(
            "/api/config", json={"INDEXER_SEARCH_ENABLED": "1", "INDEXER_ENABLED_SITES": "nyaa"}, headers=self.headers,
        )
        self.assertEqual(enabled.status_code, 200)
        self.assertTrue(config.get_bool("INDEXER_SEARCH_ENABLED"))
        self.assertEqual(current_indexer_site_ids(), ("nyaa",))
        self.assertEqual(self.client.get("/api/indexers/sites").status_code, 200)

    def test_indexer_switch_validates_effective_legacy_and_managed_sites(self):
        from app.indexers.config import current_indexer_site_ids

        self._write_raw(INDEXER_SEARCH_ENABLED="0", INDEXER_ENABLED_SITES="1lou,aipan", INDEXER_SUKEBEI_ENABLED="1")
        enable = {"INDEXER_SEARCH_ENABLED": "1"}
        enabled = self.client.post("/api/config", json=enable, headers=self.headers)
        self.assertEqual(enabled.status_code, 200)
        self.assertEqual(current_indexer_site_ids(), ("sukebei",))
        refused = self.client.post("/api/config", json={"INDEXER_SUKEBEI_ENABLED": "0"}, headers=self.headers)
        self.assertEqual(refused.status_code, 400)
        disabled = self.client.post(
            "/api/config", json={"INDEXER_SUKEBEI_ENABLED": "0", "INDEXER_SEARCH_ENABLED": "0"}, headers=self.headers,
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(current_indexer_site_ids(), ())
        key = "INDEXER_ENABLED_SITES"
        with patch.dict(os.environ, {key: "mikan"}), patch.object(
            config, "_STARTUP_ENV_OVERRIDES", config._STARTUP_ENV_OVERRIDES | {key},
        ):
            enabled = self.client.post("/api/config", json=enable, headers=self.headers)
            self.assertEqual(enabled.status_code, 200)
            self.assertEqual(current_indexer_site_ids(), ("mikan",))
            self.assertEqual(config.all_items()[key], "1lou,aipan")

    def test_strm_extension_conflicts_reject_partial_and_default_updates_atomically(self):
        self._write_raw(STRM_VIDEO_EXTS="mkv,mp4", STRM_METADATA_EXTS="nfo,srt", STRM_METADATA_ENABLED="1")
        for updates in (
            {"STRM_METADATA_EXTS": ".MKV，nfo"},
            {"STRM_VIDEO_EXTS": "mkv, .NFO"},
            {"STRM_VIDEO_EXTS": "", "STRM_METADATA_EXTS": "mkv"},
        ):
            with self.subTest(updates=updates):
                before = config.ENV_FILE.read_bytes()
                response = self.client.post("/api/config", json={**updates, "STRM_SKIP_THRESHOLD_MB": "20"}, headers=self.headers)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("不能重复", response.json()["error"])
                self.assertEqual(config.ENV_FILE.read_bytes(), before)

    def test_legacy_strm_conflict_blocks_running_and_enabling_but_allows_switching_off(self):
        from app.modules.scheduler import get_scheduler
        from app.modules import feature_config, strm_metadata_management

        self._write_raw(STRM_VIDEO_EXTS="mkv", STRM_METADATA_EXTS="mkv,nfo", STRM_METADATA_ENABLED="1",
                        GY_STRM_SOURCE_DIRS='[{"id":"source","name":"Source"}]', GY_STRM_BASE_URL="http://play.invalid")
        with patch.object(get_scheduler(), "trigger", return_value={"ok": True}) as trigger:
            response = self.client.post("/api/strm/run/full", headers=self.headers)
        self.assertEqual(response.status_code, 400, response.text)
        trigger.assert_not_called()
        response = self.client.post("/api/config", json={
            "STRM_VIDEO_EXTS": "mkv", "STRM_METADATA_EXTS": "mkv,nfo", "STRM_METADATA_ENABLED": "0",
        }, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(config.get_bool("STRM_METADATA_ENABLED"))
        before = config.ENV_FILE.read_bytes()
        with self.assertRaisesRegex(ValueError, "不能重复"):
            strm_metadata_management.prepare_policy(True, "owner")
        preview = feature_config.prepare_feature("strm_metadata", True)
        self.assertFalse(preview["preview"]["ok"])
        self.assertIn("不能重复", preview["preview"]["error"])
        response = self.client.post("/api/config", json={"STRM_METADATA_ENABLED": "1"}, headers=self.headers)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(config.ENV_FILE.read_bytes(), before)

    def test_strm_extensions_save_and_runtime_share_normalization(self):
        from app.modules.scheduler import STRMScheduler

        response = self.client.post("/api/config", json={
            "STRM_VIDEO_EXTS": ".MKV，mp4 MKV", "STRM_METADATA_EXTS": ".NFO，srt", "STRM_METADATA_ENABLED": "1",
        }, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(config.get("STRM_VIDEO_EXTS"), "mkv,mp4")
        self.assertEqual(config.get("STRM_METADATA_EXTS"), "nfo,srt")
        self.assertEqual(STRMScheduler._video_exts(), {"mkv", "mp4"})
        self.assertEqual(STRMScheduler._metadata_exts(), {"nfo", "srt"})
        response = self.client.post("/api/config", json={"STRM_METADATA_ENABLED": ""}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(config.get("STRM_METADATA_ENABLED"), "")
        self.assertEqual(STRMScheduler._metadata_exts(), set())

    def test_rss_save_checks_current_qb_setup_without_changing_existing_subscription(self):
        from app.repositories import rss as rss_repo

        self._write_raw(QB_URL="")
        payload = {"name": "test", "urls": "https://example.invalid/rss", "download_method": "qb"}
        rejected = self.client.post("/api/rss/subscriptions", json=payload, headers=self.headers)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["code"], "qb_not_configured")
        self.assertEqual(rss_repo.list_rss_subscriptions(), [])
        with patch("app.modules.rss_subscriptions.wake_rss_scheduler"):
            created = self.client.post("/api/rss/subscriptions", json={**payload, "download_method": "guangya"}, headers=self.headers)
            self.assertEqual(created.status_code, 200, created.text)
            sid = created.json()["id"]
            original_row = rss_repo.get_rss_subscription(sid)
            assert original_row is not None
            original = dict(original_row)
            rejected = self.client.put(f"/api/rss/subscriptions/{sid}", json=payload, headers=self.headers)
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(rejected.json()["code"], "qb_not_configured")
            unchanged = rss_repo.get_rss_subscription(sid)
            assert unchanged is not None
            self.assertEqual(dict(unchanged), original)
            self._write_raw(QB_URL="http://qb.invalid:8080")
            saved = self.client.put(f"/api/rss/subscriptions/{sid}", json=payload, headers=self.headers)
            self.assertEqual(saved.status_code, 200, saved.text)
            saved_row = rss_repo.get_rss_subscription(sid)
            assert saved_row is not None
            self.assertEqual(saved_row["download_method"], "qb")
            self._write_raw(QB_URL="")
            paused = self.client.put(f"/api/rss/subscriptions/{sid}", json={**payload, "enabled": False}, headers=self.headers)
            self.assertEqual(paused.status_code, 200, paused.text)
            paused_row = rss_repo.get_rss_subscription(sid)
            assert paused_row is not None
            self.assertFalse(paused_row["enabled"], "下载器未配置也应允许停用已有订阅")

    def test_new_defaults_and_saved_values_match_execution_without_get_writes(self):
        from app.modules.offline import OfflineRules

        defaults = {
            "OFFLINE_MIN_FILE_MB": "50", "OFFLINE_HTTP_ENABLED": "1",
            "STRM_VIDEO_EXTS": "mkv,mp4,ts,m2ts,iso,mov,avi,flv",
            "AGENT_LLM_TIMEOUT_SECONDS": "120", "INDEXER_SEARCH_ENABLED": "0",
        }
        saved = {
            "OFFLINE_MIN_FILE_MB": "0", "OFFLINE_HTTP_ENABLED": "0",
            "STRM_VIDEO_EXTS": "mp4", "AGENT_LLM_TIMEOUT_SECONDS": "60",
            "INDEXER_SEARCH_ENABLED": "1",
        }
        for updates, expected in (({}, defaults), (saved, saved)):
            with self.subTest(updates=updates):
                if updates:
                    self._write_raw(**updates)
                before = config.ENV_FILE.read_bytes()
                payload = self._config()
                self.assertEqual({key: payload[key] for key in expected}, expected)
                rules = OfflineRules.from_config()
                self.assertEqual(rules.min_file_mb, int(expected["OFFLINE_MIN_FILE_MB"]))
                self.assertEqual(rules.http_enabled, expected["OFFLINE_HTTP_ENABLED"] == "1")
                self.assertEqual(config.get_int_setting("AGENT_LLM_TIMEOUT_SECONDS"), int(expected["AGENT_LLM_TIMEOUT_SECONDS"]))

                offline_page = self.client.get("/guangya/offline")
                self.assertEqual(offline_page.status_code, 200)
                self.assertIn(f'id="offlineMinFileMb" value="{expected["OFFLINE_MIN_FILE_MB"]}"', offline_page.text)
                http_input = re.search(r'<input[^>]+id="offlineHttpEnabled"[^>]*>', offline_page.text)
                self.assertIsNotNone(http_input)
                self.assertEqual(" checked" in http_input.group(), rules.http_enabled)
                strm_page = self.client.get("/guangya/strm")
                self.assertEqual(strm_page.status_code, 200)
                self.assertIn(f'data-video-ext-preset="{defaults["STRM_VIDEO_EXTS"]}">全量', strm_page.text)
                self.assertEqual(config.ENV_FILE.read_bytes(), before, "读取页面与缺省值不能回写配置")

        self._write_raw(OFFLINE_HTTP_ENABLED="", INDEXER_SEARCH_ENABLED="")
        before = config.ENV_FILE.read_bytes()
        payload = self._config()
        self.assertEqual((payload["OFFLINE_HTTP_ENABLED"], payload["INDEXER_SEARCH_ENABLED"]), ("", ""))
        self.assertFalse(OfflineRules.from_config().http_enabled)
        self.assertEqual(config.ENV_FILE.read_bytes(), before)

    def test_console_username_is_public_while_passwords_and_secrets_stay_protected(self):
        from app.security import redact_config

        self._write_raw(AGENT_LLM_API_KEY="synthetic-llm-key", TG_BOT_TOKEN="synthetic-bot-token")
        before = config.ENV_FILE.read_bytes()
        username, _password = config.web_credentials()
        payload = self._config()
        self.assertEqual(payload["ENV_WEB_PASSPORT"], username)
        for key in ("ENV_WEB_PASSWORD", "WEB_SECRET_KEY", "AGENT_LLM_API_KEY", "TG_BOT_TOKEN"):
            self.assertEqual(payload[key], "********", key)
        self.assertEqual(redact_config({"ENV_WEB_PASSPORT": username})["ENV_WEB_PASSPORT"], "********")
        self.assertEqual(config.ENV_FILE.read_bytes(), before)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/config").status_code, 401)

    def test_organize_and_strm_form_defaults_match_runtime_and_keep_saved_values(self):
        from app.modules.organize_rules import (
            DEFAULT_ORGANIZE_METADATA_EXTS, DEFAULT_ORGANIZE_VIDEO_EXTS, OrganizeRules,
        )
        from app.modules.scheduler import STRMScheduler

        self._write_raw(STRM_METADATA_ENABLED="1")

        defaults = {
            "STRM_SKIP_THRESHOLD_MB": "0",
            "STRM_METADATA_EXTS": "nfo,srt,ass,ssa,sub,idx,jpg,jpeg,png,webp",
            "GY_ORGANIZE_VIDEO_EXTS": ",".join(DEFAULT_ORGANIZE_VIDEO_EXTS),
            "GY_ORGANIZE_METADATA_EXTS": ",".join(DEFAULT_ORGANIZE_METADATA_EXTS),
            "GY_ORGANIZE_SMALL_FILE_MB": "10", "GY_ORGANIZE_CONFLICT_STRATEGY": "1",
            "GY_ORGANIZE_NSFW_CATEGORY_NAME": "成人内容", "GY_ORGANIZE_NSFW_TIMEOUT_SECONDS": "8",
            "GY_ORGANIZE_AUTOMATIC_MATCH_PRESET": "balanced",
        }
        custom = {
            "STRM_SKIP_THRESHOLD_MB": "0", "STRM_METADATA_EXTS": "nfo,webp",
            "GY_ORGANIZE_VIDEO_EXTS": "mp4", "GY_ORGANIZE_METADATA_EXTS": "nfo",
            "GY_ORGANIZE_SMALL_FILE_MB": "0", "GY_ORGANIZE_CONFLICT_STRATEGY": "3",
            "GY_ORGANIZE_NSFW_CATEGORY_NAME": "收藏", "GY_ORGANIZE_NSFW_TIMEOUT_SECONDS": "20",
            "GY_ORGANIZE_AUTOMATIC_MATCH_PRESET": "conservative",
        }
        for updates, expected in (({}, defaults), (dict.fromkeys(defaults, ""), defaults), (custom, custom)):
            with self.subTest(updates=updates):
                self._write_raw(**updates)
                before = config.ENV_FILE.read_bytes()
                payload = self._config()
                self.assertEqual({key: payload[key] for key in expected}, expected)
                rules = OrganizeRules.from_config()
                for field in ("small_file_mb", "conflict_strategy", "nsfw_category_name", "nsfw_timeout_seconds", "automatic_match_preset"):
                    self.assertEqual(str(getattr(rules, field)), payload[f"GY_ORGANIZE_{field.upper()}"])
                self.assertEqual(STRMScheduler._metadata_exts(), set(payload["STRM_METADATA_EXTS"].split(",")))
                self.assertEqual(config.ENV_FILE.read_bytes(), before)

    def test_missing_defaults_fill_while_explicit_empty_switches_and_cron_stay_empty(self):
        from app.modules.organize_rules import OrganizeRules
        from app.modules.schedule_policy import current_policy

        payload = self._config()
        for key in DEFAULT_ON:
            self.assertEqual(payload[key], "1", key)
        for key in CRON_KEYS:
            self.assertEqual(payload[key], DEFAULT_SCHEDULE_CRON, key)
        rules = OrganizeRules.from_config()
        self.assertTrue(rules.emby_refresh and rules.region_split and rules.clean_empty and rules.notify_enabled)
        self.assertEqual(current_policy("strm")["cron"], DEFAULT_SCHEDULE_CRON)

        self._write_raw(**dict.fromkeys((*DEFAULT_ON, *CRON_KEYS), ""), AGENT_LLM_PROTOCOL="", TAVILY_MAX_RESULTS="")
        payload = self._config()
        for key in (*DEFAULT_ON, *CRON_KEYS):
            self.assertEqual(payload[key], "", key)
        # 其他字段空值的回填规则不属于本切片，保持原样。
        self.assertEqual((payload["AGENT_LLM_PROTOCOL"], payload["TAVILY_MAX_RESULTS"]), ("auto", "5"))
        rules = OrganizeRules.from_config()
        self.assertFalse(rules.emby_refresh or rules.region_split or rules.clean_empty or rules.notify_enabled)
        self.assertEqual((current_policy("strm")["cron"], current_policy("organize")["cron"]), ("", ""))
        self.assertFalse(current_policy("strm")["notify_enabled"])

        # 非标准真值原样回传，由页面与 get_bool 用同一真值集合解析。
        self._write_raw(GY_ORGANIZE_EMBY_REFRESH=" y ")
        self.assertEqual(self._config()["GY_ORGANIZE_EMBY_REFRESH"], " y ")
        self.assertTrue(OrganizeRules.from_config().emby_refresh)

    def test_generic_save_rejects_empty_or_malformed_cron_without_publishing(self):
        before = config.ENV_FILE.read_bytes()
        with patch("app.routes.api.reload_runtime") as reload:
            for key in CRON_KEYS:
                for value in ("", "   ", "0 4 * *", "0 0 4 * * *"):
                    with self.subTest(key=key, value=value):
                        response = self.client.post(
                            "/api/config", json={key: value, "GY_ORGANIZE_REGION_SPLIT": "0"}, headers=self.headers,
                        )
                        self.assertEqual(response.status_code, 400, response.text)
                        self.assertIn(key, response.json()["error"])
            reload.assert_not_called()
        self.assertEqual(config.ENV_FILE.read_bytes(), before)

        # 未提交 Cron 时其他合法字段照常保存；有效 Cron 正常发布。
        saved = self.client.post("/api/config", json={"GY_ORGANIZE_REGION_SPLIT": "0"}, headers=self.headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        for key in CRON_KEYS:
            saved = self.client.post("/api/config", json={key: "30 5 * * *"}, headers=self.headers)
            self.assertEqual(saved.status_code, 200, saved.text)
        payload = self._config()
        self.assertEqual(payload["GY_ORGANIZE_REGION_SPLIT"], "0")
        self.assertEqual([payload[key] for key in CRON_KEYS], ["30 5 * * *", "30 5 * * *"])

    def test_historical_empty_cron_stays_visible_after_unrelated_saves(self):
        self._write_raw(STRM_SCHEDULE_CRON="", GY_ORGANIZE_SCHEDULE_CRON="")
        saved = self.client.post("/api/config", json={"STRM_NOTIFY_ENABLED": "1"}, headers=self.headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        payload = self._config()
        self.assertEqual([payload[key] for key in CRON_KEYS], ["", ""])
        self.assertIn("STRM_SCHEDULE_CRON=''", config.ENV_FILE.read_text(encoding="utf-8"))

    def test_explicit_empty_overrides_missing_inherited_values_and_is_idempotent(self):
        from app.modules.offline import OfflineRules

        self._write_raw(RSS_QB_CATEGORY="rss", RSS_QB_SAVE_PATH="/rss", RSS_GY_TARGET_DIR="123")
        updates = dict.fromkeys(("QB_CATEGORY", "OFFLINE_TARGET_DIR"), "")
        self.assertTrue(set(updates).isdisjoint(config.all_items()))
        self.assertEqual(OfflineRules.from_config().target_dir_id, "123")
        saved = self.client.post("/api/config", json=updates, headers=self.headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual({key: config.all_items().get(key) for key in updates}, updates)
        self.assertEqual(config.get("QB_CATEGORY"), "")
        self.assertEqual(OfflineRules.from_config().target_dir_id, "0")
        with patch("app.routes.api.config.set_and_save") as persist:
            repeated = self.client.post("/api/config", json=updates, headers=self.headers)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        persist.assert_not_called()

    def test_unified_qb_category_reads_legacy_values_without_publishing_or_exposing_old_controls(self):
        from app.modules.rss import capture_rss_qb_runtime_config

        self._write_raw(QB_URL="http://synthetic.invalid", RSS_QB_CATEGORY="rss", TG_QB_SAVE_PATH="/old-tg", RSS_QB_SAVE_PATH="/old-rss")
        for updates, expected in (({}, "rss"), ({"TG_QB_CATEGORY": "tg"}, "tg"), ({"TG_QB_CATEGORY": ""}, ""), ({"QB_CATEGORY": "unified"}, "unified"), ({"QB_CATEGORY": ""}, "")):
            with self.subTest(updates=updates):
                self._write_raw(**updates)
                before = config.ENV_FILE.read_bytes()
                payload = self._config()
                runtime, error = capture_rss_qb_runtime_config()
                self.assertEqual(error, "")
                self.assertEqual(payload["QB_CATEGORY"], expected)
                self.assertEqual(runtime["category"], expected)
                self.assertNotIn("default_save_path", runtime)
                self.assertTrue(set(payload).isdisjoint({"TG_QB_CATEGORY", "RSS_QB_CATEGORY", "TG_QB_SAVE_PATH", "RSS_QB_SAVE_PATH", "RSS_DOWNLOAD_METHOD"}))
                self.assertEqual(config.ENV_FILE.read_bytes(), before)

    def test_legacy_deployment_category_locks_the_unified_field_until_explicitly_overridden(self):
        import os

        with patch.dict(os.environ, {"TG_QB_CATEGORY": "deployed"}), patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset({"TG_QB_CATEGORY"})):
            payload = self._config()
            self.assertEqual(payload["QB_CATEGORY"], "deployed")
            self.assertIn("QB_CATEGORY", payload["__managed_fields"])
            response = self.client.post("/api/config", json={"QB_CATEGORY": "edited"}, headers=self.headers)
            self.assertNotEqual(response.status_code, 200)
        self._write_raw(QB_CATEGORY="")
        with patch.dict(os.environ, {"TG_QB_CATEGORY": "deployed"}), patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset({"TG_QB_CATEGORY"})):
            payload = self._config()
            self.assertEqual(payload["QB_CATEGORY"], "")
            self.assertNotIn("QB_CATEGORY", payload["__managed_fields"])

    def test_failed_publication_restores_missing_keys_without_changing_inheritance(self):
        from app.modules.offline import OfflineRules

        self._write_raw(RSS_QB_CATEGORY="rss", RSS_QB_SAVE_PATH="/rss", RSS_GY_TARGET_DIR="123")
        before = config.all_items()
        updates = {"QB_CATEGORY": "tg", "OFFLINE_TARGET_DIR": "456"}
        with patch("app.agent.feature_gate.invalidate_agent_runtime_generation", side_effect=RuntimeError("abort")):
            response = self.client.post("/api/config", json=updates, headers=self.headers)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(config.all_items(), before)
        self.assertEqual(config.get("QB_CATEGORY"), "rss")
        self.assertEqual(OfflineRules.from_config().target_dir_id, "123")
        saved = self.client.post("/api/config", json=updates, headers=self.headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(OfflineRules.from_config().target_dir_id, "456")
