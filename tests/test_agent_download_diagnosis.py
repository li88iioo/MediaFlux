from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import Mock, patch

from app import database as db, sdk
from app.clients.qbittorrent import QBittorrentClient, TorrentTask, TransferInfo
from app.repositories import download_requests
from tests.support import IsolatedDatabaseTestCase


def _task(
    *,
    name: str = "Example.Show.S01E01",
    state: str = "downloading",
    progress: float = 0.5,
    dlspeed: int = 1024,
    eta: int = 60,
    hash_value: str = "a" * 40,
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


_SETTINGS = {
    "QB_URL": "http://private-qb.example:8080",
    "QB_USERNAME": "private-user", "QB_PASSWORD": "private-password", "QB_API_KEY": "",
}


def _diagnose(tasks=(), *, transfer="connected", queue_error=None, transfer_error=None, settings=None):
    client = Mock(spec=QBittorrentClient)
    client.list_torrents.return_value = list(tasks)
    client.list_torrents.side_effect = queue_error
    client.get_transfer_info.return_value = TransferInfo(
        connection_status=transfer, dl_info_speed=400, dl_info_data=999,
        up_info_speed=20, up_info_data=888, dht_nodes=42,
    )
    client.get_transfer_info.side_effect = transfer_error
    with (patch("app.modules.download_diagnosis.config.get_many", return_value=_SETTINGS if settings is None else settings),
          patch("app.modules.qb_control.QBittorrentClient", return_value=client) as factory):
        result = sdk.downloads.diagnose()
    if factory.called:
        client.close.assert_called_once_with()
        assert [call[0] for call in client.method_calls] == ["list_torrents", "get_transfer_info", "close"]
    else:
        assert not client.method_calls
    return result


class DownloadDiagnosisUnitTests(unittest.TestCase):
    def test_sdk_signature_rejects_extra_fields(self):
        arguments: dict[str, Any] = {"limit": 1}
        with self.assertRaises(TypeError):
            sdk.downloads.diagnose(**arguments)

    def test_not_configured_and_incomplete_do_not_create_client(self):
        for state, settings in (
            ("not_configured", dict.fromkeys(_SETTINGS, "")),
            ("incomplete", {**_SETTINGS, "QB_PASSWORD": ""}),
        ):
            with self.subTest(state=state):
                result = _diagnose(settings=settings)
                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], state)
                self.assertEqual(result["data"]["queue_state"], state)
                self.assertEqual(result["data"]["summary"], {})
                self.assertNotIn("private-qb.example", str(result))

    def test_classifies_snapshot_with_real_ids_without_credentials_or_private_fields(self):
        tasks = [
            _task(name="magnet:?xt=urn:btih:private", state="stalledDL", progress=0.4, dlspeed=0),
            _task(name="missing", state="missingFiles"),
            _task(name="error", state="error"), _task(name="paused", state="pausedDL"),
            _task(name="queued", state="queuedDL"), _task(name="checking", state="checkingDL"),
            _task(name="metadata", state="metaDL"), _task(name="zero", dlspeed=0),
            _task(name="active", dlspeed=400), _task(name="done", state="uploading", progress=1, dlspeed=0),
        ]
        for index, task in enumerate(tasks):
            task.hash = f"{index:064x}" if index == 1 else f"{index:040x}"
        result = _diagnose(tasks)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "attention")
        summary = result["data"]["summary"]
        for key, count in (("total", 10), ("suspected_stuck", 1), ("failed", 2), ("completed", 1), ("downloading", 1)):
            self.assertEqual(summary[key], count)
        attention = result["data"]["attention_tasks"]
        self.assertEqual([item["hash"] for item in attention], [tasks[i].hash for i in (0, 1, 2, 7)])
        self.assertEqual(attention[0]["reason_codes"], ["stalled_download", "zero_download_speed"])
        self.assertEqual(attention[0]["progress_percent"], 40.0)
        self.assertEqual(attention[0]["size_bytes"], 4096)
        self.assertEqual(attention[0]["downloaded_bytes"], 2048)
        self.assertEqual(attention[0]["name"], "下载任务")
        self.assertEqual(result["data"]["connection"]["transfer_status"], "connected")
        self.assertEqual(result["data"]["connection"]["download_speed_bytes_per_sec"], 400)
        self.assertEqual(result["data"]["connection"]["dht_nodes"], 42)
        for private in ("/private/downloads", "private-category", "private-qb.example", "private-user",
                        "private-password", "magnet:?xt=urn:btih:private"):
            self.assertNotIn(private, str(result))
        self.assertIn("当前快照疑似停滞", str(result))
        self.assertNotIn("已持续", str(result))
        self.assertEqual(set(result["evidence"][0]), {"source", "description", "collected_at"})

    def test_healthy_and_empty_queues_are_successful_observations(self):
        for tasks in ([], [_task(), _task(state="stalledUP", progress=1, dlspeed=0)]):
            with self.subTest(count=len(tasks)):
                result = _diagnose(tasks)
                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], "healthy")
                self.assertFalse(result["partial"])
                self.assertEqual(result["data"]["summary"]["total"], len(tasks))
                self.assertEqual(result["data"]["queue_state"], "reachable")
                self.assertEqual(result["data"]["attention_tasks"], [])
                self.assertEqual(result["data"]["unavailable_sources"], [])

    def test_modern_and_transitional_states_are_classified_consistently(self):
        for state, progress, expected in (
            ("stoppedDL", 0.4, "paused"), ("stoppedUP", 1, "completed"),
            ("forcedMetaDL", 0, "metadata"), ("checkingResumeData", 1, "checking"),
            ("moving", 1, "processing"), ("allocating", 0, "processing"),
        ):
            with self.subTest(state=state):
                result = _diagnose([_task(state=state, progress=progress, dlspeed=0)])
                self.assertEqual(result["data"]["summary"][expected], 1)
                self.assertEqual(result["data"]["attention_tasks"], [])
                self.assertEqual(result["status"], "healthy")

    def test_disconnected_transfer_degrades_top_level_status(self):
        result = _diagnose(transfer="disconnected")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "attention")
        self.assertEqual(result["data"]["connection"]["api_state"], "reachable")
        self.assertEqual(result["data"]["connection"]["transfer_status"], "disconnected")
        self.assertIn("传输连接状态需要关注", result["summary"])

    def test_sensitive_title_markers_are_redacted(self):
        for marker in (
            "api_key=TOPSECRET", "authkey=TOPSECRET", "password=TOPSECRET", "cookie=TOPSECRET",
            "session: TOPSECRET", "Authorization: Bearer TOPSECRET",
            "/private/downloads/secret.mkv", "C:\\private\\secret.mkv",
        ):
            with self.subTest(marker=marker):
                result = _diagnose([_task(name=f"release {marker}", state="error")])
                self.assertEqual(result["data"]["attention_tasks"][0]["name"], "下载任务")
                self.assertNotIn("TOPSECRET", str(result))

    def test_eta_preserves_long_values_and_maps_zero_to_unknown(self):
        result = _diagnose([_task(state="stalledDL", eta=31536000), _task(state="error", eta=0)])
        self.assertEqual(result["data"]["attention_tasks"][0]["eta_seconds"], 31536000)
        self.assertIsNone(result["data"]["attention_tasks"][1]["eta_seconds"])

    def test_attention_list_marks_truncation_and_preserves_all_counts(self):
        result = _diagnose([_task(state="error", hash_value=f"{i:040x}") for i in range(25)])
        self.assertEqual(result["data"]["summary"]["failed"], 25)
        self.assertEqual([item["hash"] for item in result["data"]["attention_tasks"]], [f"{i:040x}" for i in range(20)])
        self.assertTrue(result["data"]["attention_truncated"])

    def test_failures_keep_successful_facts_and_never_leak_exception_details(self):
        private_error = RuntimeError("private-qb.example private-password")
        for queue_failed, transfer_failed in ((True, False), (False, True), (True, True)):
            with self.subTest(queue_failed=queue_failed, transfer_failed=transfer_failed):
                result = _diagnose([_task(state="error")],
                                   queue_error=private_error if queue_failed else None,
                                   transfer_error=private_error if transfer_failed else None)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "unavailable" if queue_failed and transfer_failed else "partial")
                self.assertEqual(result["partial"], not (queue_failed and transfer_failed))
                data = result["data"]
                self.assertEqual(data["queue_state"], "unavailable" if queue_failed else "reachable")
                self.assertEqual(data["connection"]["api_state"], "unavailable" if transfer_failed else "reachable")
                self.assertEqual(data["unavailable_sources"], [source for source, failed in
                                 (("queue", queue_failed), ("transfer", transfer_failed)) if failed])
                if queue_failed:
                    self.assertEqual(data["summary"], {})
                else:
                    self.assertEqual(data["summary"]["failed"], 1)
                    self.assertEqual(data["attention_tasks"][0]["hash"], "a" * 40)
                for private in ("private-qb.example", "private-password"):
                    self.assertNotIn(private, str(result))


class DownloadRequestSummaryScopeTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM download_log")
            conn.execute("DELETE FROM download_requests")

    def test_empty_and_populated_results_report_local_sqlite_scope(self):
        empty = download_requests.list_download_request_summaries(scope="recent")
        self.assertEqual(empty["count"], 0)
        self.assertEqual(empty["items"], [])
        self.assertFalse(empty["has_more"])
        self.assertIsNone(empty["next_offset"])

        request_id, created = db.create_download_request(
            "request-summary-local-scope", "magnet", title="测试本地下载记录"
        )
        self.assertTrue(created)

        populated = download_requests.list_download_request_summaries(scope="recent")
        self.assertEqual(populated["count"], 1)
        self.assertEqual(populated["items"][0]["request_number"], request_id)
        self.assertEqual(populated["items"][0]["title"], "测试本地下载记录")

        for result in (empty, populated):
            with self.subTest(count=result["count"]):
                scope_note = result["scope_note"]
                self.assertIn("当前 MediaFlux 实例本地 SQLite", scope_note)
                self.assertIn("不查询光鸭", scope_note)
                self.assertIn("不代表光鸭实时全账户离线任务队列", scope_note)

    def test_public_query_validates_arguments_without_agent_validator(self):
        invalid: tuple[dict[str, Any], ...] = (
            {"scope": "all"}, {"scope": []}, {"limit": True}, {"limit": 0},
            {"limit": 31}, {"limit": "12"}, {"offset": -1}, {"offset": True},
            {"offset": "0"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                download_requests.list_download_request_summaries(**arguments)

    def test_recent_pagination_returns_actual_request_ids_without_omissions(self):
        ids = [db.create_download_request(f"summary-page-{i}", "magnet")[0] for i in range(3)]
        first = download_requests.list_download_request_summaries(scope="recent", limit=2)
        second = download_requests.list_download_request_summaries(
            scope="recent", limit=2, offset=first["next_offset"],
        )
        self.assertEqual([item["request_number"] for item in first["items"]], ids[:0:-1])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 2)
        self.assertEqual([item["request_number"] for item in second["items"]], ids[:1])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_offset"])

    def test_active_and_attention_preserve_shared_filters_and_do_not_write(self):
        ids = {}
        for key, state, backend in (
            ("running", "downloading", "downloading"),
            ("done", "completed", "completed"),
            ("failed", "failed", "failed"),
            ("unknown", "manual_review", "outcome_unknown"),
            ("cleared", "failed", "failed"),
            ("cancelled", "cancelled", "submitted"),
            ("notifying", "completed", "completed"),
        ):
            request_id, _ = db.create_download_request(f"summary-{key}", "magnet")
            ids[key] = request_id
            db.update_download_request(request_id, status=state, targets="qb", qb_status=backend,
                                       notification_delivery_status="sent")
        db.clear_download_request_attentions([ids["cleared"]])
        db.update_download_request(ids["notifying"], notification_delivery_status="pending")
        def snapshot():
            rows = []
            for value in ids.values():
                row = db.get_download_request(value)
                assert row is not None
                rows.append(dict(row))
            return rows

        before = snapshot()

        active = download_requests.list_download_request_summaries()
        attention = download_requests.list_download_request_summaries(scope="attention")
        self.assertEqual([item["request_number"] for item in active["items"]],
                         [ids["running"], ids["unknown"], ids["notifying"]])
        self.assertEqual({item["request_number"] for item in attention["items"]},
                         {ids["failed"], ids["unknown"]})
        self.assertEqual([row["id"] for row in db.list_active_download_requests()],
                         [item["request_number"] for item in active["items"]])
        self.assertEqual([row["id"] for row in db.list_download_requests_requiring_attention()],
                         [item["request_number"] for item in attention["items"]])
        first = download_requests.list_download_request_summaries(limit=2)
        second = download_requests.list_download_request_summaries(offset=first["next_offset"], limit=2)
        self.assertEqual([item["request_number"] for item in first["items"] + second["items"]],
                         [item["request_number"] for item in active["items"]])
        self.assertFalse(second["has_more"])
        first = download_requests.list_download_request_summaries(scope="attention", limit=1)
        second = download_requests.list_download_request_summaries(scope="attention", limit=1, offset=first["next_offset"])
        self.assertEqual([item["request_number"] for item in first["items"] + second["items"]],
                         [item["request_number"] for item in attention["items"]])
        self.assertFalse(second["has_more"])
        self.assertEqual(snapshot(), before)

    def test_summary_preserves_unknown_and_partial_facts_without_private_values(self):
        request_id, _ = db.create_download_request(
            "summary-private", "magnet", title="magnet:?xt=private-source",
            source_value="https://private.invalid/file?token=SECRET",
        )
        db.update_download_request(request_id, targets="both", status="manual_review",
                                   qb_status="completed", gy_status="outcome_unknown",
                                   error="guangya: token=SECRET", organize_status="partial",
                                   organize_error="整理部分完成，仍有文件需要处理",
                                   local_import_status="failed", local_import_error="/private/media/file.mkv",
                                   strm_status="partial", strm_error="STRM 已完成，媒体库刷新等待后台重试")
        item = download_requests.list_download_request_summaries(scope="attention")["items"][0]
        self.assertEqual(item["status"], "manual_review")
        self.assertEqual(item["qb_status"], "completed")
        self.assertEqual(item["guangya_status"], "outcome_unknown")
        self.assertEqual(item["title"], "下载任务")
        self.assertIn("勿直接重复提交", item["error"])
        self.assertIn("媒体库刷新等待后台重试", item["error"])
        self.assertEqual({issue["key"] for issue in item["postprocess_issues"]},
                         {"organize", "local_import", "strm"})
        for value in ("SECRET", "private-source", "private.invalid", "/private/media", "source_value"):
            self.assertNotIn(value, str(item))
