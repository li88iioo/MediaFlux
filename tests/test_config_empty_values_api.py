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
    """走真实发布器写入 user.env；进程环境在退出后恢复，后续读取只看文件。"""
    with patch.dict(os.environ):
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
