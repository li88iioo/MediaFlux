from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from unittest.mock import patch

from tests.support import IsolatedDatabaseTestCase
from tools import agent_doctor


class AgentDoctorTests(IsolatedDatabaseTestCase):
    def test_doctor_accepts_configured_local_http_provider(self) -> None:
        with patch.dict(os.environ, {
            "AGENT_LLM_ENABLED": "1",
            "AGENT_LLM_API_URL": "http://127.0.0.1:8080/v1",
            "AGENT_LLM_MODEL": "local-model",
            "AGENT_LLM_PROTOCOL": "chat_completions",
        }, clear=False):
            check = agent_doctor._provider_check()
        self.assertEqual(check.status, "ok")
        self.assertIn("本次未联网", check.message)

    def test_offline_doctor_reports_ready_without_echoing_secrets(self) -> None:
        secret = "web-secret-" + ("x" * 32)
        provider_key = "provider-key-not-for-output"
        scrape_key = "metrics-key-" + ("y" * 32)
        with patch.dict(os.environ, {
            "AGENT_ENABLED": "1",
            "AGENT_LLM_ENABLED": "1",
            "AGENT_LLM_API_URL": "https://api.example.test/v1",
            "AGENT_LLM_PROTOCOL": "chat_completions",
            "AGENT_LLM_MODEL": "model-test",
            "AGENT_LLM_API_KEY": provider_key,
            "AGENT_METRICS_SCRAPE_KEY": scrape_key,
            "WEB_SECRET_KEY": secret,
        }, clear=False), patch.object(agent_doctor, "workspace_unavailable_reason", return_value=""):
            report = agent_doctor.run_agent_diagnostics()

        statuses = {check.key: check.status for check in report.checks}
        self.assertEqual(statuses, {
            "agent.config": "ok",
            "agent.provider": "ok",
            "agent.database": "ok",
            "agent.web_secret": "ok",
            "agent.keys": "ok",
            "agent.sdk": "ok",
            "agent.workspace": "ok",
        })
        serialized = json.dumps(report.as_dict(), ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(provider_key, serialized)
        self.assertNotIn(scrape_key, serialized)
        self.assertIn("本次未联网", serialized)

    def test_doctor_rejects_unavailable_workspace_even_when_sdk_is_available(self) -> None:
        reason = "代码工作区需要容器 root 入口"
        with patch.object(agent_doctor, "workspace_unavailable_reason", return_value=reason):
            report = agent_doctor.run_agent_diagnostics()
        self.assertEqual(report.check("agent.sdk").status, "ok")
        check = report.check("agent.workspace")
        self.assertEqual(check.status, "error")
        self.assertEqual(check.message, reason)
        self.assertIn("工作区运行条件", check.suggestion)
        self.assertEqual(agent_doctor._exit_code(report), 1)

    def test_doctor_flags_invalid_provider_and_weak_scrape_key_safely(self) -> None:
        with patch.dict(os.environ, {
            "AGENT_ENABLED": "1",
            "AGENT_LLM_ENABLED": "1",
            "AGENT_LLM_API_URL": "http://127.0.0.1:8000/v1?token=private",
            "AGENT_LLM_PROTOCOL": "chat_completions",
            "AGENT_LLM_MODEL": "model-test",
            "AGENT_METRICS_SCRAPE_KEY": "too-short",
            "WEB_SECRET_KEY": "z" * 40,
        }, clear=False):
            report = agent_doctor.run_agent_diagnostics()

        self.assertEqual(report.check("agent.provider").status, "error")
        self.assertEqual(report.check("agent.keys").status, "error")
        serialized = json.dumps(report.as_dict(), ensure_ascii=False)
        self.assertNotIn("token=private", serialized)
        self.assertNotIn("too-short", serialized)

    def test_json_cli_has_stable_shape_and_exit_code(self) -> None:
        with patch.object(
            agent_doctor,
            "run_agent_diagnostics",
            return_value=agent_doctor.DiagnosticReport((
                agent_doctor.DiagnosticCheck(
                    "agent.example", "warning", "示例告警", "示例建议"
                ),
            )),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = agent_doctor.main(["--json"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "ok": True,
            "checks": [{
                "key": "agent.example",
                "status": "warning",
                "message": "示例告警",
                "suggestion": "示例建议",
            }],
        })
