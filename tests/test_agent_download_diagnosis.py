from __future__ import annotations

import re
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent.download_actions import diagnose_download_queue, download_diagnosis_arguments
from app.agent.models import ToolContext
from app.agent.orchestrator import AgentOrchestrator, is_download_queue_diagnosis_message
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.clients.qbittorrent import TorrentTask, TransferInfo
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _task(
    *,
    name: str = "Example.Show.S01E01",
    state: str = "downloading",
    progress: float = 0.5,
    dlspeed: int = 1024,
    eta: int = 60,
    hash_value: str = "private-hash",
    save_path: str = "/private/downloads",
    content_path: str = "/private/downloads/file.mkv",
    category: str = "private-category",
) -> TorrentTask:
    return TorrentTask(
        hash=hash_value,
        name=name,
        progress=progress,
        state=state,
        save_path=save_path,
        content_path=content_path,
        size=4096,
        downloaded=2048,
        dlspeed=dlspeed,
        upspeed=32,
        eta=eta,
        ratio=1.5,
        category=category,
        added_on=123456,
    )


def _configured_value(key: str, default: str = "") -> str:
    values = {
        "QB_URL": "http://private-qb.example:8080",
        "QB_USERNAME": "private-user",
        "QB_PASSWORD": "private-password",
        "QB_API_KEY": "",
    }
    return values.get(key, default)


class DownloadDiagnosisUnitTests(unittest.TestCase):
    def test_arguments_reject_extra_fields(self):
        self.assertEqual(download_diagnosis_arguments({}), {})
        with self.assertRaises(AgentToolError):
            download_diagnosis_arguments({"limit": 1})

    def test_not_configured_and_incomplete_do_not_create_client(self):
        with patch("app.agent.download_actions.config.get", return_value=""), patch(
            "app.agent.download_actions.QBittorrentClient"
        ) as client:
            missing = diagnose_download_queue({})
        self.assertTrue(missing.ok)
        self.assertEqual(missing.status, "not_configured")
        client.assert_not_called()

        values = {"QB_URL": "http://secret", "QB_USERNAME": "user", "QB_PASSWORD": "", "QB_API_KEY": ""}
        with patch(
            "app.agent.download_actions.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.agent.download_actions.QBittorrentClient") as client:
            incomplete = diagnose_download_queue({})
        self.assertTrue(incomplete.ok)
        self.assertEqual(incomplete.status, "incomplete")
        client.assert_not_called()
        self.assertNotIn("http://secret", str(incomplete.to_dict()))

    def test_classifies_snapshot_and_never_exposes_private_task_fields(self):
        secret_url = "magnet:?xt=urn:btih:private"
        tasks = [
            _task(name=secret_url, state="stalledDL", progress=0.4, dlspeed=0),
            _task(name="missing", state="missingFiles"),
            _task(name="error", state="error"),
            _task(name="paused", state="pausedDL"),
            _task(name="queued", state="queuedDL"),
            _task(name="checking", state="checkingDL"),
            _task(name="metadata", state="metaDL"),
            _task(name="zero", state="downloading", dlspeed=0),
            _task(name="active", state="downloading", dlspeed=400),
            _task(name="done", state="uploading", progress=1.0, dlspeed=0),
        ]
        client = Mock()
        client.list_torrents.return_value = tasks
        client.get_transfer_info.return_value = TransferInfo(
            connection_status="connected",
            dl_info_speed=400,
            dl_info_data=999,
            up_info_speed=20,
            up_info_data=888,
            dht_nodes=42,
        )
        with patch("app.agent.download_actions.config.get", side_effect=_configured_value), patch(
            "app.agent.download_actions.QBittorrentClient", return_value=client
        ):
            result = diagnose_download_queue({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        summary = result.data["summary"]
        self.assertEqual(summary["total"], 10)
        self.assertEqual(summary["suspected_stuck"], 1)
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["downloading"], 1)
        serialized = str(result.to_dict())
        for private_value in (
            "private-hash", "/private/downloads", "private-category", "private-qb.example",
            "private-user", "private-password", secret_url,
        ):
            self.assertNotIn(private_value, serialized)
        self.assertIn("当前快照疑似停滞", serialized)
        self.assertNotIn("已持续", serialized)
        self.assertEqual(result.data["connection"]["transfer_status"], "connected")

    def test_healthy_queue_returns_no_attention(self):
        client = Mock()
        client.list_torrents.return_value = [
            _task(name="active", state="downloading", dlspeed=10),
            _task(name="done", state="stalledUP", progress=1.0, dlspeed=0),
        ]
        client.get_transfer_info.return_value = TransferInfo(connection_status="connected")
        with patch("app.agent.download_actions.config.get", side_effect=_configured_value), patch(
            "app.agent.download_actions.QBittorrentClient", return_value=client
        ):
            result = diagnose_download_queue({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "healthy")
        self.assertEqual(result.data["attention_tasks"], [])


    def test_modern_and_transitional_states_are_classified_consistently(self):
        states = [
            ("stoppedDL", 0.4, "paused"),
            ("stoppedUP", 1.0, "completed"),
            ("forcedMetaDL", 0.0, "metadata"),
            ("checkingResumeData", 1.0, "checking"),
            ("moving", 1.0, "processing"),
            ("allocating", 0.0, "processing"),
        ]
        for state, progress, expected in states:
            with self.subTest(state=state):
                client = Mock()
                client.list_torrents.return_value = [
                    _task(name=state, state=state, progress=progress, dlspeed=0)
                ]
                client.get_transfer_info.return_value = TransferInfo(connection_status="connected")
                with patch("app.agent.download_actions.config.get", side_effect=_configured_value), patch(
                    "app.agent.download_actions.QBittorrentClient", return_value=client
                ):
                    result = diagnose_download_queue({})
                self.assertEqual(result.data["summary"][expected], 1)
                self.assertEqual(result.data["attention_tasks"], [])
                self.assertEqual(result.status, "healthy")

    def test_disconnected_transfer_degrades_top_level_status(self):
        client = Mock()
        client.list_torrents.return_value = []
        client.get_transfer_info.return_value = TransferInfo(connection_status="disconnected")
        with patch("app.agent.download_actions.config.get", side_effect=_configured_value), patch(
            "app.agent.download_actions.QBittorrentClient", return_value=client
        ):
            result = diagnose_download_queue({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["connection"]["api_state"], "reachable")
        self.assertEqual(result.data["connection"]["transfer_status"], "disconnected")
        self.assertIn("传输连接状态需要关注", result.summary)

    def test_sensitive_title_markers_are_redacted(self):
        markers = (
            "api_key=TOPSECRET", "authkey=TOPSECRET", "password=TOPSECRET",
            "cookie=TOPSECRET", "session: TOPSECRET", "Authorization: Bearer TOPSECRET",
            "/private/downloads/secret.mkv", r"C:\private\secret.mkv",
        )
        for marker in markers:
            with self.subTest(marker=marker):
                client = Mock()
                client.list_torrents.return_value = [
                    _task(name=f"release {marker}", state="error")
                ]
                client.get_transfer_info.return_value = TransferInfo(connection_status="connected")
                with patch("app.agent.download_actions.config.get", side_effect=_configured_value), patch(
                    "app.agent.download_actions.QBittorrentClient", return_value=client
                ):
                    result = diagnose_download_queue({})
                self.assertEqual(result.data["attention_tasks"][0]["name"], "下载任务")
                self.assertNotIn("TOPSECRET", str(result.to_dict()))

    def test_eta_preserves_long_values_and_maps_zero_to_unknown(self):
        client = Mock()
        client.list_torrents.return_value = [
            _task(name="long", state="stalledDL", eta=31_536_000),
            _task(name="unknown", state="error", eta=0),
        ]
        client.get_transfer_info.return_value = TransferInfo(connection_status="connected")
        with patch("app.agent.download_actions.config.get", side_effect=_configured_value), patch(
            "app.agent.download_actions.QBittorrentClient", return_value=client
        ):
            result = diagnose_download_queue({})
        self.assertEqual(result.data["attention_tasks"][0]["eta_seconds"], 31_536_000)
        self.assertIsNone(result.data["attention_tasks"][1]["eta_seconds"])


    def test_attention_list_is_bounded_and_marks_truncation(self):
        client = Mock()
        client.list_torrents.return_value = [
            _task(name=f"failed-{index}", state="error") for index in range(25)
        ]
        client.get_transfer_info.return_value = TransferInfo(connection_status="connected")
        with patch("app.agent.download_actions.config.get", side_effect=_configured_value), patch(
            "app.agent.download_actions.QBittorrentClient", return_value=client
        ):
            result = diagnose_download_queue({})
        self.assertEqual(result.data["summary"]["failed"], 25)
        self.assertEqual(len(result.data["attention_tasks"]), 20)
        self.assertTrue(result.data["attention_truncated"])

    def test_client_exception_is_fixed_and_sanitized(self):
        client = Mock()
        client.list_torrents.side_effect = RuntimeError("private-qb.example private-password")
        with patch("app.agent.download_actions.config.get", side_effect=_configured_value), patch(
            "app.agent.download_actions.QBittorrentClient", return_value=client
        ):
            result = diagnose_download_queue({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        serialized = str(result.to_dict())
        self.assertNotIn("private-qb.example", serialized)
        self.assertNotIn("private-password", serialized)

    def test_registry_exposes_read_only_tool(self):
        capabilities = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        spec = capabilities["downloads.diagnose_queue"]
        self.assertEqual(spec["risk"], "read")
        self.assertFalse(spec["requires_confirmation"])

    def test_natural_language_routes_only_explicit_queue_diagnosis(self):
        self.assertTrue(is_download_queue_diagnosis_message("检查 qB 下载状态"))
        self.assertTrue(is_download_queue_diagnosis_message("哪些下载任务卡住了"))
        self.assertTrue(is_download_queue_diagnosis_message("检查qB下载状态"))
        self.assertTrue(is_download_queue_diagnosis_message("查看下载队列"))
        self.assertTrue(is_download_queue_diagnosis_message("下载完成了吗"))
        self.assertTrue(is_download_queue_diagnosis_message("下载进度怎么样"))
        self.assertFalse(is_download_queue_diagnosis_message("检查下载器配置"))
        self.assertFalse(is_download_queue_diagnosis_message("搜索《沙丘2》的下载源"))
        self.assertFalse(is_download_queue_diagnosis_message("刚才下载为什么失败，帮我重试"))

        registry = Mock()
        registry.execute.return_value = (
            diagnose_download_queue({}),
            1,
        )
        agent = AgentOrchestrator(registry)
        response = agent.query("诊断下载队列")
        self.assertEqual(response["tool_call"]["name"], "downloads.diagnose_queue")
        registry.execute.assert_called_once_with(
            "downloads.diagnose_queue", {}, context=ToolContext(owner="")
        )


class DownloadDiagnosisAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.agent_gate_patch = patch(
            "app.routes.agent_api.is_agent_enabled", return_value=True
        )
        self.agent_gate_patch.start()
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.agent_gate_patch.stop()
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        matched = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not matched:
            matched = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not matched:
            raise AssertionError("CSRF token missing")
        return matched.group(1)

    def _login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def test_api_auth_csrf_and_shared_rate_limit(self):
        path = "/api/agent/tools/downloads.diagnose_queue"
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 403)
        headers = {"X-CSRF-Token": csrf}

        with patch("app.agent.download_actions.config.get", return_value=""):
            for _ in range(4):
                response = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {}})
                self.assertEqual(response.status_code, 200, response.text)
            limited = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "检查 qB 下载状态"},
            )
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    unittest.main()
