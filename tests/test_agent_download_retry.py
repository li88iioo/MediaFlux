"""下载重试公共预检、冻结校验与真实请求状态机。"""
from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db, sdk
from app.modules import download_dispatcher as dispatcher
from tests.support import IsolatedDatabaseTestCase


class DownloadRetryTests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"QB_URL": "http://qb.invalid"}))
        self.enterContext(patch.object(dispatcher, "analyze_offline_url",
                                      return_value=SimpleNamespace(allowed=True, reason="")))
        for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection"):
            self.enterContext(patch(name, side_effect=AssertionError("no real download")))

    @staticmethod
    def request(**updates):
        identity = uuid.uuid4().hex + "a" * 8
        request_id, _ = db.create_download_request(
            "private-key-" + identity, "magnet", title="Private.Show",
            source_value="magnet:?xt=urn:btih:" + identity,
            chat_id="private-chat", user_id="private-user", origin="agent",
        )
        db.update_download_request(request_id, **{
            "status": "manual_review", "targets": "both",
            "qb_status": "failed", "gy_status": "failed", "error": "private backend error", **updates,
        })
        return request_id

    def test_prepare_is_read_only_and_public(self):
        request_id = self.request()
        stored_row_1 = db.get_download_request(request_id)
        assert stored_row_1 is not None
        before = dict(stored_row_1)
        with patch.object(dispatcher, "_submit_qb") as qb, patch.object(dispatcher, "_submit_guangya") as gy:
            preview = dispatcher.prepare_download_retry(request_id, "qb")
        qb.assert_not_called()
        gy.assert_not_called()
        stored_row_2 = db.get_download_request(request_id)
        assert stored_row_2 is not None
        self.assertEqual(dict(stored_row_2), before)
        self.assertEqual(preview["request_id"], request_id)
        self.assertEqual(preview["target"], "qb")
        self.assertTrue(preview["fingerprint"])
        for secret in ("private-key", "Private.Show", "magnet:?", "private-chat",
                       "private-user", "private backend error"):
            self.assertNotIn(secret, json.dumps(preview))
        for request_id, target in ((True, "qb"), (0, "qb"), (request_id, "preferred")):
            with self.subTest(request_id=request_id, target=target), self.assertRaises(ValueError):
                dispatcher.prepare_download_retry(request_id, target)

    def test_native_registry_keeps_danger_confirmation_and_retires_old_entry(self):
        from app.agent.kernel.bootstrap import build_agent_kernel_runtime
        from app.agent.kernel.capabilities import ToolEffect

        catalog = build_agent_kernel_runtime(model=SimpleNamespace()).session.catalog
        self.assertFalse(catalog.has("downloads.retry_submission"))
        self.assertFalse(catalog.has("downloads__retry_submission"))
        tool = catalog.get("downloads.retry")
        self.assertEqual(tool.effect, ToolEffect.DANGER)
        self.assertIsNotNone(tool.prepare)
        self.assertIsNotNone(tool.execute_confirmed)
        self.assertIsNone(tool.read)

    def test_frozen_source_state_and_configuration_changes_reject_before_dispatch(self):
        for change in ("title", "source_value", "torrent_data", "qb_status", "config"):
            with self.subTest(change=change):
                request_id = self.request()
                snapshot = dispatcher.prepare_download_retry(request_id, "qb")
                if change != "config":
                    db.update_download_request(request_id, **{
                        change: {"torrent_data": b"changed", "qb_status": "outcome_unknown"}.get(change, "changed"),
                    })
                with patch.dict(os.environ, {"TG_QB_SAVE_PATH": "/changed"} if change == "config" else {}), \
                     patch.object(dispatcher, "_submit_qb") as backend, \
                     self.assertRaises(dispatcher.DownloadRetryChanged):
                    dispatcher.resubmit_download_request(
                        request_id, "qb", expected_fingerprint=snapshot["fingerprint"],
                    )
                backend.assert_not_called()

    def test_unknown_selected_target_cannot_be_retried(self):
        request_id = self.request(qb_status="outcome_unknown")
        with patch.object(dispatcher, "_submit_qb") as backend:
            with self.assertRaises(ValueError):
                dispatcher.prepare_download_retry(request_id, "qb")
            result = dispatcher.resubmit_download_request(request_id, "qb")
        self.assertFalse(result["ok"])
        backend.assert_not_called()
        stored_row_3 = db.get_download_request(request_id)
        assert stored_row_3 is not None
        self.assertEqual(stored_row_3["qb_status"], "outcome_unknown")

    def test_sdk_retry_only_resubmits_failed_target_and_keeps_unknown_peer(self):
        request_id = self.request(status="submitted", qb_status="failed", gy_status="outcome_unknown")
        with patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "a" * 40}) as qb, \
             patch.object(dispatcher, "_submit_guangya") as gy:
            result = sdk.downloads.retry(request_id, "qb")
        qb.assert_called_once()
        gy.assert_not_called()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["request_id"], request_id)
        self.assertEqual(result["source_request_id"], request_id)
        self.assertEqual(result["succeeded"], ["qb"])
        self.assertTrue(result["source_attention_preserved"])
        stored_row_4 = db.get_download_request(request_id)
        assert stored_row_4 is not None
        self.assertEqual(stored_row_4["gy_status"], "outcome_unknown")

    def test_public_receipt_keeps_partial_unknown_and_successor_facts_without_backend_payload(self):
        for extra, status in (({"failed": ["guangya"]}, "partial"),
                              ({"outcome_unknown": True}, "manual_review")):
            with self.subTest(status=status):
                result = dispatcher.public_download_retry_result({
                    "ok": True, "created": True, "request_id": 999, "succeeded": ["qb"],
                    "source_attention_preserved": True, "results": {"qb": {"task_id": "private-task"}},
                    "error": "token=SECRET https://private.invalid", **extra,
                }, source_request_id=1, target="both")
                self.assertEqual(result["status"], status)
                self.assertEqual(result["request_id"], 999)
                self.assertEqual(result["source_request_id"], 1)
                self.assertEqual(result["succeeded"], ["qb"])
                self.assertTrue(result["source_attention_preserved"])
                for secret in ("private-task", "SECRET", "private.invalid"):
                    self.assertNotIn(secret, json.dumps(result))
