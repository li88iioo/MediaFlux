from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from app.agent.action_history import action_history_owner_digest
from app.agent.download_control_actions import download_task_arguments
from app.agent.orchestrator import AgentOrchestrator, download_task_control_request
from app.agent.registry import AgentToolError
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.clients.qbittorrent import TorrentTask
from tests.support import IsolatedDatabaseTestCase
from app import database as db


def _task(*, name: str = "Example.Show.S01E01", state: str = "downloading", progress: float = 0.5) -> TorrentTask:
    return TorrentTask(
        hash="a" * 40,
        name=name,
        progress=progress,
        state=state,
        save_path="/private/path",
        content_path="/private/path/file.mkv",
        size=100,
        downloaded=50,
        dlspeed=10,
        upspeed=0,
        eta=30,
        ratio=0.0,
        category="private",
        added_on=1,
    )




class DownloadControlTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        reset_agent_service_for_tests()

    def tearDown(self) -> None:
        reset_agent_service_for_tests()

    def test_arguments_and_natural_language_are_strict(self):
        self.assertEqual(download_task_arguments({"task_name": " Example.Show.S01E01 "}), {
            "task_name": "Example.Show.S01E01",
        })
        for invalid in ({}, {"task_name": ""}, {"task_name": "x", "hash": "a" * 40}, {
            "task_name": "magnet:?xt=urn:btih:secret",
        }):
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                download_task_arguments(invalid)
        self.assertEqual(
            download_task_control_request("暂停下载任务《Example.Show.S01E01》"),
            ("downloads.pause_task", {"task_name": "Example.Show.S01E01"}),
        )
        self.assertEqual(
            download_task_control_request("恢复 qBittorrent 任务『Example.Show.S01E01』"),
            ("downloads.resume_task", {"task_name": "Example.Show.S01E01"}),
        )
        self.assertIsNone(download_task_control_request("删除下载任务"))

    def test_registry_requires_confirmation(self):
        tools = {item["name"]: item for item in get_agent_service().registry.capabilities()}
        self.assertEqual(tools["downloads.pause_task"]["risk"], "low_write")
        self.assertEqual(tools["downloads.resume_task"]["risk"], "low_write")
        self.assertEqual(tools["downloads.delete_task"]["risk"], "danger")
        self.assertTrue(tools["downloads.delete_task"]["requires_confirmation"])
        with self.assertRaises(AgentToolError):
            get_agent_service().registry.execute(
                "downloads.pause_task", {"task_name": "Example.Show.S01E01"}
            )

    def test_pause_confirm_uses_frozen_snapshot_without_leaking_private_fields(self):
        client = Mock()
        client.list_torrents.return_value = [_task()]
        with patch("app.agent.download_control_actions._client", return_value=client):
            service = get_agent_service()
            prepared = service.prepare(
                "downloads.pause_task", {"task_name": "Example.Show.S01E01"}, owner="owner"
            )
            confirmed = service.confirm(prepared["confirmation"]["confirmation_id"], owner="owner")

        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertTrue(confirmed["result"]["ok"])
        client.pause_torrents.assert_called_once_with("a" * 40)
        serialized = json.dumps({"prepared": prepared, "confirmed": confirmed}, ensure_ascii=False)
        for secret in ("a" * 40, "/private/path", "private-password", "private-qb.invalid"):
            self.assertNotIn(secret, serialized)
        history = db.list_agent_action_history(
            owner_digest=action_history_owner_digest("owner"), limit=1
        )[0]
        self.assertEqual(history["tool_name"], "downloads.pause_task")
        details = json.loads(history["safe_details"])
        self.assertEqual(details["operation"], "pause")
        self.assertEqual(details["affected"], 1)

    def test_state_change_makes_confirmation_conflict(self):
        client = Mock()
        client.list_torrents.side_effect = [
            [_task(state="downloading", progress=0.5)],
            [_task(state="downloading", progress=0.8)],
        ]
        with patch("app.agent.download_control_actions._client", return_value=client):
            service = get_agent_service()
            prepared = service.prepare(
                "downloads.pause_task", {"task_name": "Example.Show.S01E01"}, owner="owner"
            )
            confirmed = service.confirm(prepared["confirmation"]["confirmation_id"], owner="owner")
        self.assertFalse(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["status"], "conflict")
        client.pause_torrents.assert_not_called()

    def test_delete_never_deletes_files(self):
        client = Mock()
        client.list_torrents.return_value = [_task()]
        with patch("app.agent.download_control_actions._client", return_value=client):
            service = get_agent_service()
            prepared = service.prepare(
                "downloads.delete_task", {"task_name": "Example.Show.S01E01"}, owner="owner"
            )
            confirmed = service.confirm(prepared["confirmation"]["confirmation_id"], owner="owner")
        self.assertTrue(confirmed["result"]["ok"])
        client.delete_torrents.assert_called_once_with("a" * 40, delete_files=False)
        self.assertFalse(confirmed["result"]["data"]["delete_files"])

    def test_duplicate_names_are_rejected_before_action(self):
        client = Mock()
        client.list_torrents.return_value = [_task(), _task(progress=0.7)]
        with patch("app.agent.download_control_actions._client", return_value=client), self.assertRaises(
            AgentToolError
        ) as caught:
            get_agent_service().prepare(
                "downloads.pause_task", {"task_name": "Example.Show.S01E01"}, owner="owner"
            )
        self.assertEqual(caught.exception.code, "precondition_failed")
        client.pause_torrents.assert_not_called()

    def test_orchestrator_prepares_exact_quoted_task(self):
        registry = Mock()
        registry.prepare_confirmation.return_value = (
            Mock(risk=Mock(value="low_write")),
            {"task_name": "Example.Show.S01E01"},
            "fingerprint",
            Mock(to_dict=lambda: {"ok": True, "status": "confirmation_required", "summary": "确认"}),
            1,
        )
        # 这里只验证确定性分支优先级；完整票据行为由 service 测试覆盖。
        agent = AgentOrchestrator(registry=registry)
        with patch.object(agent, "prepare", return_value={"mode": "confirmation_required"}) as prepare:
            result = agent.query("暂停下载任务《Example.Show.S01E01》", owner="owner")
        self.assertEqual(result["mode"], "confirmation_required")
        prepare.assert_called_once_with(
            "downloads.pause_task", {"task_name": "Example.Show.S01E01"}, owner="owner"
        )


if __name__ == "__main__":
    unittest.main()
