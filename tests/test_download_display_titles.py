"""下载展示标题与业务标题隔离的回归测试。"""
from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.indexers import downloads as indexer_downloads
from app.indexers.models import ResolvedDownload
from app.modules import download_dispatcher
from app.modules.download_tracker import DownloadTracker
from app.repositories.download_requests import (
    apply_download_tracker_update,
    download_display_title,
)
from tests.support import IsolatedDatabaseTestCase, isolated_test_database


class DownloadDisplayTitleTests(IsolatedDatabaseTestCase):
    """真实隔离 SQLite 覆盖请求、分发、跟踪和公开 DTO。"""

    def setUp(self) -> None:
        # 每个用例使用独立新库；测试类仍继承统一 Web/数据库隔离基类。
        self.enterContext(isolated_test_database())
        self._serial = 0
        self.tracker = DownloadTracker()

    def _create_request(
        self,
        *,
        title: str = "磁力任务",
        display_title: str = "",
        source_value: str | None = None,
    ) -> tuple[int, str]:
        self._serial += 1
        infohash = f"{self._serial:040x}"
        source = source_value or f"magnet:?xt=urn:btih:{infohash}"
        request_id, created = db.create_download_request(
            f"display-title-test:{self._serial}",
            "magnet",
            title=title,
            display_title=display_title,
            source_value=source,
        )
        self.assertTrue(created)
        return request_id, source

    @staticmethod
    def _gy_task(task_id: str, name: str) -> dict:
        from app.clients.guangya import GuangYaClient

        return GuangYaClient._to_offline_task(
            {"taskId": task_id, "name": name, "status": "downloading", "progress": 20}
        )

    def _track_guangya(self, request_id: int, task_ids: list[str], tasks: list[dict]) -> None:
        db.update_download_request(
            request_id,
            status="submitted",
            targets="guangya",
            gy_status="submitted",
            gy_task_id=task_ids[0] if task_ids else "",
            gy_task_ids=json.dumps(task_ids),
            gy_batch_count=len(task_ids),
        )
        self.tracker._update_request(
            db.get_download_request(request_id), [], tasks, qb_available=False
        )

    def test_fresh_database_has_display_title_column(self) -> None:
        with db.get_conn() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(download_requests)")}
        self.assertIn("display_title", columns)
        self.assertEqual(db.SCHEMA_VERSION, 33)

    def test_log_enrichment_uses_request_index_instead_of_full_table_scan(self) -> None:
        with db.get_conn() as conn:
            plan = conn.execute("EXPLAIN QUERY PLAN UPDATE download_log SET title=? WHERE request_id=? AND title=?",
                                ("新名称", 1, "磁力任务")).fetchall()
        self.assertTrue(any("SEARCH" in str(row[3]) and "idx_download_log_request_source_id" in str(row[3]) for row in plan), plan)

    def test_v31_migration_preserves_business_data_and_is_idempotent(self) -> None:
        request_id, _ = self._create_request(title="业务标题不变")
        db.update_download_request(
            request_id,
            status="completed",
            targets="qb",
            qb_status="completed",
        )
        with db.get_conn() as conn:
            conn.execute("DROP INDEX idx_download_log_request_source_id")
            conn.execute("ALTER TABLE download_requests DROP COLUMN display_title")
            before_columns = [
                row[1] for row in conn.execute("PRAGMA table_info(download_requests)")
            ]
            conn.execute("PRAGMA user_version=31")
            before = tuple(conn.execute(
                "SELECT title,status,qb_status FROM download_requests WHERE id=?",
                (request_id,),
            ).fetchone())

        db.init_db()
        with db.get_conn() as conn:
            after_columns = [
                row[1] for row in conn.execute("PRAGMA table_info(download_requests)")
            ]
            self.assertIn("idx_download_log_request_source_id",
                          {row[1] for row in conn.execute("PRAGMA index_list(download_log)")})
            migrated = tuple(conn.execute(
                "SELECT title,status,qb_status FROM download_requests WHERE id=?",
                (request_id,),
            ).fetchone())
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 33)
        self.assertEqual(after_columns, [*before_columns, "display_title"])
        self.assertEqual(migrated, before)

        db.init_db()
        with db.get_conn() as conn:
            self.assertEqual(
                [row[1] for row in conn.execute("PRAGMA table_info(download_requests)")],
                after_columns,
            )
            self.assertEqual(tuple(conn.execute(
                "SELECT title,status,qb_status FROM download_requests WHERE id=?",
                (request_id,),
            ).fetchone()), before)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 33)

    def test_indexer_display_name_does_not_change_business_title_or_dispatch_input(self) -> None:
        magnet = "magnet:?xt=urn:btih:" + "a" * 40
        stored = SimpleNamespace(title="资源站解析出的展示名称", site_id="fixture")
        item = asyncio.run(indexer_downloads._resolved_download_input(
            None, stored, ResolvedDownload(kind="magnet", value=magnet)
        ))
        self.assertEqual(item.title, "磁力任务")
        self.assertEqual(item.display_title, stored.title)
        expected_key = download_dispatcher.request_key(item)
        backend_rows: list[dict] = []

        def accept(row, **_kwargs):
            backend_rows.append(dict(row))
            return {"ok": True, "task_id": "gy-indexer-1", "task_ids": ["gy-indexer-1"], "batch_count": 1}

        with patch.object(download_dispatcher, "_submit_guangya", side_effect=accept):
            result = indexer_downloads.submit_download_input(
                item, "guangya", origin="indexer", log_path="[magnet]"
            )

        request_id = result["request_id"]
        row = db.get_download_request(request_id)
        self.assertEqual(row["title"], item.title)
        self.assertEqual(row["display_title"], stored.title)
        self.assertEqual(row["source_value"], magnet)
        self.assertEqual(row["request_key"], expected_key)
        self.assertEqual(backend_rows[0]["title"], item.title)
        logs = [log for log in db.list_download_logs() if log["request_id"] == request_id]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["title"], stored.title)

    def test_exact_guangya_task_match_enriches_title_and_backfills_existing_log(self) -> None:
        request_id, _ = self._create_request()
        log_id = db.add_download_log(
            "guangya", title="磁力任务", request_id=request_id, status="submitted",
            backend_task_id="gy-exact",
        )

        self._track_guangya(
            request_id, ["gy-exact"], [self._gy_task("gy-exact", "光鸭实际名称")]
        )

        row = db.get_download_request(request_id)
        self.assertEqual(row["display_title"], "光鸭实际名称")
        self.assertEqual(row["title"], "磁力任务")
        logs = [log for log in db.list_download_logs() if log["request_id"] == request_id]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["id"], log_id)
        self.assertEqual(logs[0]["title"], "光鸭实际名称")

    def test_guangya_match_never_replaces_an_existing_usable_display_title(self) -> None:
        request_id, _ = self._create_request(display_title="用户确认的标题")
        self._track_guangya(
            request_id, ["gy-kept"], [self._gy_task("gy-kept", "云端另一个名称")]
        )
        row = db.get_download_request(request_id)
        self.assertEqual(row["display_title"], "用户确认的标题")
        self.assertEqual(row["title"], "磁力任务")

    def test_unknown_guangya_task_name_is_not_guessed(self) -> None:
        request_id, _ = self._create_request()
        self._track_guangya(
            request_id, ["expected-id"], [self._gy_task("unrelated-id", "不可认领名称")]
        )
        row = db.get_download_request(request_id)
        self.assertEqual(row["display_title"], "")
        self.assertEqual(download_display_title(row), "磁力任务")

    def test_only_complete_same_name_guangya_batch_enriches_title(self) -> None:
        cases = (
            ("same", ["same-1", "same-2"], ["同一批次名称", "同一批次名称"], "同一批次名称"),
            ("different", ["diff-1", "diff-2"], ["名称甲", "名称乙"], ""),
            ("incomplete", ["part-1", "part-2"], ["只匹配到一项"], ""),
        )
        for label, ids, names, expected in cases:
            with self.subTest(batch=label):
                request_id, _ = self._create_request()
                tasks = [self._gy_task(task_id, name) for task_id, name in zip(ids, names)]
                self._track_guangya(request_id, ids, tasks)
                self.assertEqual(db.get_download_request(request_id)["display_title"], expected)

    def test_qb_task_name_is_used_only_after_exact_hash_match(self) -> None:
        infohash = "b" * 40
        request_id, _ = self._create_request(
            source_value=f"magnet:?xt=urn:btih:{infohash}"
        )
        db.update_download_request(
            request_id,
            status="submitted",
            targets="qb",
            qb_status="submitted",
            qb_task_id=infohash,
        )
        task = SimpleNamespace(
            hash=infohash, name="qB 任务名称", progress=0.25, state="downloading",
            content_path="",
        )
        self.tracker._update_request(
            db.get_download_request(request_id), [task], [], gy_available=False
        )
        row = db.get_download_request(request_id)
        self.assertEqual(row["display_title"], "qB 任务名称")
        self.assertEqual(row["title"], "磁力任务")

    def test_stale_snapshot_and_cancelled_request_reject_display_metadata(self) -> None:
        stale_id, _ = self._create_request()
        stale_snapshot = db.get_download_request(stale_id)
        db.update_download_request(stale_id, error="state advanced")
        self.assertIsNone(apply_download_tracker_update(
            stale_snapshot, display_title="过期观察名称", status="downloading"
        ))
        self.assertEqual(db.get_download_request(stale_id)["display_title"], "")

        cancelled_id, _ = self._create_request()
        cancelled_snapshot = db.get_download_request(cancelled_id)
        db.update_download_request(cancelled_id, status="cancelled")
        self.assertIsNone(apply_download_tracker_update(
            cancelled_snapshot, display_title="取消后的名称", status="downloading"
        ))
        self.assertEqual(db.get_download_request(cancelled_id)["display_title"], "")

    def test_copied_download_input_keeps_known_display_title(self) -> None:
        original = download_dispatcher.DownloadInput(
            kind="magnet",
            title="磁力任务",
            source_value="magnet:?xt=urn:btih:" + "c" * 40,
            display_title="复制前展示名",
        )
        copied = replace(original)
        self.assertEqual(copied.display_title, original.display_title)
        created = download_dispatcher.create_request(copied, "", "", origin="indexer")
        row = db.get_download_request(created["id"])
        self.assertEqual(row["title"], original.title)
        self.assertEqual(row["display_title"], original.display_title)

    def test_duplicate_input_backfills_placeholder_log_without_new_log_or_dispatch(self) -> None:
        magnet = "magnet:?xt=urn:btih:" + "d" * 40
        item = download_dispatcher.DownloadInput(
            kind="magnet", title="磁力任务", source_value=magnet,
            display_title="重复请求展示名",
        )
        first = download_dispatcher.create_request(replace(item, display_title=""), "", "", origin="indexer")
        request_id = first["id"]
        db.update_download_request(
            request_id,
            status="submitted",
            targets="guangya",
            gy_status="submitted",
            gy_task_id="gy-existing",
            gy_task_ids='["gy-existing"]',
            gy_batch_count=1,
        )
        log_id = db.add_download_log(
            "guangya", title="磁力任务", request_id=request_id,
            backend_task_id="gy-existing", status="submitted",
        )

        with patch.object(download_dispatcher, "_submit_guangya") as backend:
            indexer_downloads.submit_download_input(item, "guangya", origin="indexer")

        backend.assert_not_called()
        with db.get_conn() as conn:
            request_count = conn.execute(
                "SELECT COUNT(*) FROM download_requests WHERE request_key=?",
                (download_dispatcher.request_key(item),),
            ).fetchone()[0]
        logs = [log for log in db.list_download_logs() if log["request_id"] == request_id]
        self.assertEqual(request_count, 1)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["id"], log_id)
        self.assertEqual(logs[0]["title"], item.display_title)
        self.assertEqual(db.get_download_request(request_id)["title"], item.title)

    def test_explicit_failed_retry_inherits_display_without_changing_business_title(self) -> None:
        item = download_dispatcher.DownloadInput(
            kind="magnet", title="磁力任务", display_title="重试显示名",
            source_value="magnet:?xt=urn:btih:" + "e" * 40,
        )
        old_id = download_dispatcher.create_request(item, "", "")["id"]
        db.update_download_request(old_id, status="failed", targets="guangya", gy_status="failed")
        with (
            patch.object(download_dispatcher, "analyze_offline_url", return_value=SimpleNamespace(allowed=True, reason="")),
            patch.object(download_dispatcher, "_submit_guangya", return_value={
                "ok": True, "task_id": "retry-task", "task_ids": ["retry-task"],
                "batch_count": 1, "selected_count": 1,
            }) as submit,
        ):
            result = download_dispatcher.resubmit_download_request(old_id, "guangya")
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(result["request_id"], old_id)
        row = db.get_download_request(result["request_id"])
        self.assertEqual((row["title"], row["display_title"]), (item.title, item.display_title))
        self.assertEqual(row["source_value"], item.source_value)
        self.assertEqual(submit.call_args.args[0]["title"], item.title)
        submit.assert_called_once()

    def test_placeholder_notification_uses_enriched_title_without_redelivery(self) -> None:
        from app.modules.telegram_download_lifecycle import build_download_lifecycle_event

        request_id, _ = self._create_request(display_title="通知中的媒体名")
        db.update_download_request(request_id, status="completed", gy_status="completed",
                                   notification_payload_json=json.dumps({"title": "磁力任务"}),
                                   notification_delivery_status="sent")
        with patch("app.modules.telegram_download_lifecycle.get_notification_thread_event", return_value=None):
            event = build_download_lifecycle_event(db.get_download_request(request_id))
        self.assertEqual(dict(event.fields)["媒体"], "通知中的媒体名")
        self.assertEqual(db.get_download_request(request_id)["notification_delivery_status"], "sent")

    def test_prepared_http_torrent_uses_cached_metadata_without_changing_route_title(self) -> None:
        payload = b"d4:infod6:lengthi1e4:name4:test12:piece lengthi16384e6:pieces20:xxxxxxxxxxxxxxxxxxxxee"
        item = download_dispatcher.DownloadInput(kind="http", title="链接任务",
            source_value="https://fixture.invalid/entry.torrent", torrent_data=payload)
        prepared = download_dispatcher.prepare_download_input(item, "guangya")
        self.assertEqual(prepared.display_title, "test")
        self.assertEqual((prepared.title, prepared.source_value), (item.title, item.source_value))
        self.assertEqual(download_dispatcher.request_keys(prepared), download_dispatcher.request_keys(item))

    def test_web_attention_and_agent_public_title_use_display_projection(self) -> None:
        request_id, _ = self._create_request(display_title="公开展示标题")
        db.update_download_request(
            request_id, status="manual_review", error="需要人工核对"
        )
        row = db.get_download_request(request_id)

        from app.agent import download_actions
        from app.routes import downloads_api

        self.assertEqual(downloads_api._attention_json(row)["title"], "公开展示标题")
        # Agent 的公开 DTO 对标题使用同一展示投影，并经过公开文本安全清洗。
        self.assertEqual(
            download_actions._safe_title(download_actions.download_display_title(row)),
            "公开展示标题",
        )


if __name__ == "__main__":
    unittest.main()
