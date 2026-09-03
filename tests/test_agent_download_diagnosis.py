from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.agent.download_actions import (
    diagnose_download_queue,
    download_diagnosis_arguments,
)
from app.agent.errors import AgentToolError
from app.clients.qbittorrent import TorrentTask, TransferInfo


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
        with (
            patch("app.agent.download_actions.config.get", return_value=""),
            patch("app.agent.download_actions.QBittorrentClient") as client,
        ):
            missing = diagnose_download_queue({})
        self.assertTrue(missing.ok)
        self.assertEqual(missing.status, "not_configured")
        client.assert_not_called()
        values = {
            "QB_URL": "http://secret",
            "QB_USERNAME": "user",
            "QB_PASSWORD": "",
            "QB_API_KEY": "",
        }
        with (
            patch(
                "app.agent.download_actions.config.get",
                side_effect=lambda key, default="": values.get(key, default),
            ),
            patch("app.agent.download_actions.QBittorrentClient") as client,
        ):
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
        with (
            patch(
                "app.agent.download_actions.config.get", side_effect=_configured_value
            ),
            patch("app.agent.download_actions.QBittorrentClient", return_value=client),
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
            "private-hash",
            "/private/downloads",
            "private-category",
            "private-qb.example",
            "private-user",
            "private-password",
            secret_url,
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
        client.get_transfer_info.return_value = TransferInfo(
            connection_status="connected"
        )
        with (
            patch(
                "app.agent.download_actions.config.get", side_effect=_configured_value
            ),
            patch("app.agent.download_actions.QBittorrentClient", return_value=client),
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
                client.get_transfer_info.return_value = TransferInfo(
                    connection_status="connected"
                )
                with (
                    patch(
                        "app.agent.download_actions.config.get",
                        side_effect=_configured_value,
                    ),
                    patch(
                        "app.agent.download_actions.QBittorrentClient",
                        return_value=client,
                    ),
                ):
                    result = diagnose_download_queue({})
                self.assertEqual(result.data["summary"][expected], 1)
                self.assertEqual(result.data["attention_tasks"], [])
                self.assertEqual(result.status, "healthy")

    def test_disconnected_transfer_degrades_top_level_status(self):
        client = Mock()
        client.list_torrents.return_value = []
        client.get_transfer_info.return_value = TransferInfo(
            connection_status="disconnected"
        )
        with (
            patch(
                "app.agent.download_actions.config.get", side_effect=_configured_value
            ),
            patch("app.agent.download_actions.QBittorrentClient", return_value=client),
        ):
            result = diagnose_download_queue({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["connection"]["api_state"], "reachable")
        self.assertEqual(result.data["connection"]["transfer_status"], "disconnected")
        self.assertIn("传输连接状态需要关注", result.summary)

    def test_sensitive_title_markers_are_redacted(self):
        markers = (
            "api_key=TOPSECRET",
            "authkey=TOPSECRET",
            "password=TOPSECRET",
            "cookie=TOPSECRET",
            "session: TOPSECRET",
            "Authorization: Bearer TOPSECRET",
            "/private/downloads/secret.mkv",
            "C:\\private\\secret.mkv",
        )
        for marker in markers:
            with self.subTest(marker=marker):
                client = Mock()
                client.list_torrents.return_value = [
                    _task(name=f"release {marker}", state="error")
                ]
                client.get_transfer_info.return_value = TransferInfo(
                    connection_status="connected"
                )
                with (
                    patch(
                        "app.agent.download_actions.config.get",
                        side_effect=_configured_value,
                    ),
                    patch(
                        "app.agent.download_actions.QBittorrentClient",
                        return_value=client,
                    ),
                ):
                    result = diagnose_download_queue({})
                self.assertEqual(result.data["attention_tasks"][0]["name"], "下载任务")
                self.assertNotIn("TOPSECRET", str(result.to_dict()))

    def test_eta_preserves_long_values_and_maps_zero_to_unknown(self):
        client = Mock()
        client.list_torrents.return_value = [
            _task(name="long", state="stalledDL", eta=31536000),
            _task(name="unknown", state="error", eta=0),
        ]
        client.get_transfer_info.return_value = TransferInfo(
            connection_status="connected"
        )
        with (
            patch(
                "app.agent.download_actions.config.get", side_effect=_configured_value
            ),
            patch("app.agent.download_actions.QBittorrentClient", return_value=client),
        ):
            result = diagnose_download_queue({})
        self.assertEqual(result.data["attention_tasks"][0]["eta_seconds"], 31536000)
        self.assertIsNone(result.data["attention_tasks"][1]["eta_seconds"])

    def test_attention_list_is_bounded_and_marks_truncation(self):
        client = Mock()
        client.list_torrents.return_value = [
            _task(name=f"failed-{index}", state="error") for index in range(25)
        ]
        client.get_transfer_info.return_value = TransferInfo(
            connection_status="connected"
        )
        with (
            patch(
                "app.agent.download_actions.config.get", side_effect=_configured_value
            ),
            patch("app.agent.download_actions.QBittorrentClient", return_value=client),
        ):
            result = diagnose_download_queue({})
        self.assertEqual(result.data["summary"]["failed"], 25)
        self.assertEqual(len(result.data["attention_tasks"]), 20)
        self.assertTrue(result.data["attention_truncated"])

    def test_client_exception_is_fixed_and_sanitized(self):
        client = Mock()
        client.list_torrents.side_effect = RuntimeError(
            "private-qb.example private-password"
        )
        with (
            patch(
                "app.agent.download_actions.config.get", side_effect=_configured_value
            ),
            patch("app.agent.download_actions.QBittorrentClient", return_value=client),
        ):
            result = diagnose_download_queue({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        serialized = str(result.to_dict())
        self.assertNotIn("private-qb.example", serialized)
        self.assertNotIn("private-password", serialized)
