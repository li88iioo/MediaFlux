from __future__ import annotations

import hashlib
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import Mock, patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules import strm as strm_module
from tests.support import IsolatedDatabaseTestCase


class _TreeClient:
    def __init__(self, tree):
        self.tree = tree
        self.download_urls: list[str] = []
        self.list_calls: list[str] = []

    def list_dir(self, file_id):
        self.list_calls.append(str(file_id))
        value = self.tree[file_id]
        if isinstance(value, Exception):
            raise value
        return value

    def file_info(self, file_id):
        for files in self.tree.values():
            if isinstance(files, Exception):
                continue
            for item in files:
                if item.file_id == file_id:
                    return item
        return None

    def get_download_url(self, file_id):
        self.download_urls.append(file_id)
        return f"https://download.invalid/{file_id}?signature=super-secret"


class _Response:
    def __init__(self, payload: bytes = b"metadata"):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield self.payload


class StrmOwnedClientLifecycleTests(unittest.TestCase):
    @staticmethod
    def _client_with_failing_close():
        client = Mock()
        client.close.side_effect = RuntimeError("close exploded")
        return client

    def test_full_sync_close_failure_does_not_replace_success_result(self):
        expected = {"ok": True, "generated": 1}
        client = self._client_with_failing_close()
        with patch(
            "app.modules.strm.GuangYaClient", return_value=client,
        ), patch(
            "app.modules.strm._sync_strm_impl", return_value=expected,
        ):
            result = strm_module.sync_strm(
                "source", "http://mediaflux.invalid", "/tmp/strm",
            )

        self.assertIs(result, expected)
        client.close.assert_called_once_with()

    def test_incremental_sync_close_failure_does_not_replace_success_result(self):
        expected = {"ok": True, "generated": 1}
        client = self._client_with_failing_close()
        with patch(
            "app.modules.strm.GuangYaClient", return_value=client,
        ), patch(
            "app.modules.strm._sync_strm_incremental_impl", return_value=expected,
        ):
            result = strm_module.sync_strm_incremental(
                "source", [], "http://mediaflux.invalid", "/tmp/strm",
            )

        self.assertIs(result, expected)
        client.close.assert_called_once_with()

    def test_close_failure_does_not_replace_sync_exception(self):
        client = self._client_with_failing_close()
        with patch(
            "app.modules.strm.GuangYaClient", return_value=client,
        ), patch(
            "app.modules.strm._sync_strm_impl",
            side_effect=ValueError("sync exploded"),
        ), self.assertRaisesRegex(ValueError, "sync exploded"):
            strm_module.sync_strm(
                "source", "http://mediaflux.invalid", "/tmp/strm",
            )

        client.close.assert_called_once_with()


class StrmFailureLedgerTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_index")
            try:
                conn.execute("DELETE FROM strm_failures")
            except Exception:
                pass

    def test_generation_and_metadata_failures_are_redacted_and_persisted(self):
        from app.modules.strm_metadata_worker import STRMMetadataWorker

        source_id = "source-ledger"
        tree = {
            source_id: [
                GuangYaFile("video-1", "Movie.mkv", False, 100, "video-etag", source_id),
                GuangYaFile("meta-1", "Movie.nfo", False, 8, "meta-etag", source_id),
            ]
        }
        client = _TreeClient(tree)

        def fail_generate(*args, **kwargs):
            raise RuntimeError(
                "write failed token=plain-secret "
                "https://storage.invalid/file?signature=signed-secret"
            )

        with tempfile.TemporaryDirectory() as root, patch(
            "app.modules.strm.generate_strm", side_effect=fail_generate
        ), patch(
            "app.modules.strm.requests.get",
            side_effect=RuntimeError(
                "download failed https://storage.invalid/meta?access_token=meta-secret"
            ),
        ):
            result = strm_module.sync_strm(
                source_id,
                "http://mediaflux.invalid",
                root,
                client=client,
                metadata_exts={"nfo"},
                source_name="主媒体",
            )
            worker = STRMMetadataWorker()
            worker._client = client
            with patch(
                "app.modules.strm_metadata_worker.get_bool", return_value=True
            ), patch(
                "app.modules.strm_metadata_worker.get",
                side_effect=lambda key, default="": {
                    "STRM_ROOT": root,
                    "STRM_METADATA_EXTS": "nfo",
                }.get(key, default),
            ), patch.object(worker, "_flush_media_refresh"):
                self.assertTrue(worker._process_one())

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["metadata_queued"], 1)
        self.assertEqual(result["metadata_failed"], 0)
        rows = db.list_strm_failures(status="open", limit=20)
        self.assertEqual({row["action"] for row in rows}, {"generate", "metadata"})
        self.assertEqual({row["file_id"] for row in rows}, {"video-1", "meta-1"})
        serialized = "\n".join(str(dict(row)) for row in rows)
        self.assertNotIn("plain-secret", serialized)
        self.assertNotIn("signed-secret", serialized)
        self.assertNotIn("meta-secret", serialized)
        self.assertNotIn("https://storage.invalid", serialized)
        self.assertIn("********", serialized)

    def test_successful_retry_resolves_only_selected_failure(self):
        source_id = "source-retry"
        first = GuangYaFile("video-1", "One.mkv", False, 100, "e1", source_id)
        second = GuangYaFile("video-2", "Two.mkv", False, 200, "e2", source_id)
        client = _TreeClient({source_id: [first, second]})

        with tempfile.TemporaryDirectory() as root:
            with patch(
                "app.modules.strm.generate_strm",
                side_effect=RuntimeError("write failed password=do-not-store"),
            ):
                strm_module.sync_strm(
                    source_id,
                    "http://mediaflux.invalid",
                    root,
                    client=client,
                    source_name="待重试源",
                )
            failures = db.list_strm_failures(status="open", limit=20)
            selected_id = next(row["id"] for row in failures if row["file_id"] == "video-1")

            values = {
                "GY_STRM_SOURCE_DIRS": '[{"id":"source-retry","name":"待重试源"}]',
                "GY_STRM_BASE_URL": "http://mediaflux.invalid",
                "STRM_ROOT": root,
                "STRM_VIDEO_EXTS": "mkv",
                "STRM_METADATA_EXTS": "",
            }
            with patch(
                "app.modules.strm.get",
                create=True,
                side_effect=lambda key, default="": values.get(key, default),
            ), patch(
                "app.modules.strm.get_int",
                create=True,
                side_effect=lambda key, default=0: int(values.get(key, default) or 0),
            ):
                retried = strm_module.retry_strm_failures(
                    [selected_id], "manual", client=client
                )

            self.assertEqual(retried["resolved"], 1)
            self.assertEqual(retried["failed"], 0)
            open_rows = db.list_strm_failures(status="open", limit=20)
            self.assertEqual([row["file_id"] for row in open_rows], ["video-2"])
            resolved = db.list_strm_failures(status="resolved", limit=20)
            self.assertEqual([row["file_id"] for row in resolved], ["video-1"])
            self.assertTrue((Path(root) / strm_module.STRM_SUBDIR / "One.strm").is_file())

    def test_retry_re_resolves_file_moved_to_a_new_configured_source(self):
        old_source = "source-old"
        new_source = "source-new"
        file_id = "video-moved"
        failure_id = db.record_strm_failure(
            source_id=old_source,
            source_name="旧来源",
            file_id=file_id,
            parent_id=old_source,
            filename="Old.mkv",
            action="generate",
            rel_dir="",
            target_rel_path=f"{strm_module.STRM_SUBDIR}/Old.mkv.strm",
            error="stale source",
        )
        moved_dir = GuangYaFile("dir-new", "新目录", True, parent_id=new_source)
        moved_file = GuangYaFile(file_id, "Renamed.mkv", False, 300, "fresh-etag", "dir-new")
        client = _TreeClient({new_source: [moved_dir], "dir-new": [moved_file]})

        with tempfile.TemporaryDirectory() as root:
            values = {
                "GY_STRM_SOURCE_DIRS": '[{"id":"source-new","name":"新来源"}]',
                "GY_STRM_BASE_URL": "http://mediaflux.invalid",
                "STRM_ROOT": root,
                "STRM_VIDEO_EXTS": "mkv",
                "STRM_METADATA_EXTS": "",
            }
            with patch(
                "app.modules.strm.get",
                create=True,
                side_effect=lambda key, default="": values.get(key, default),
            ), patch(
                "app.modules.strm.get_int",
                create=True,
                side_effect=lambda key, default=0: int(values.get(key, default) or 0),
            ):
                result = strm_module.retry_strm_failures(
                    [failure_id], "manual", client=client
                )

            self.assertEqual(result["resolved"], 1)
            target = (
                Path(root)
                / strm_module.STRM_SUBDIR
                / "新目录"
                / "Renamed.strm"
            )
            self.assertTrue(target.is_file())
            self.assertIn("/playgy/video-moved/fresh-etag/", target.read_text("utf-8"))

    def test_base_url_change_rewrites_and_repairs_owned_strm(self):
        source_id = "source-url-change"
        video = GuangYaFile("video-url", "Movie.mkv", False, 100, "etag", source_id)
        client = _TreeClient({source_id: [video]})

        with tempfile.TemporaryDirectory() as root:
            first = strm_module.sync_strm(
                source_id, "http://old-mediaflux.invalid", root, client=client
            )
            target = next(Path(root).rglob("*.strm"))
            self.assertEqual(first["generated"], 1)
            self.assertTrue(target.read_text("utf-8").startswith("http://old-mediaflux.invalid/"))

            changed = strm_module.sync_strm(
                source_id, "http://new-mediaflux.invalid", root, client=client
            )
            self.assertEqual(changed["generated"], 1)
            self.assertEqual(changed["skipped"], 0)
            self.assertTrue(target.read_text("utf-8").startswith("http://new-mediaflux.invalid/"))

            target.write_text("corrupted", encoding="utf-8")
            repaired = strm_module.sync_strm(
                source_id, "http://new-mediaflux.invalid", root, client=client
            )
            self.assertEqual(repaired["generated"], 1)
            self.assertEqual(repaired["updated"], 1)
            self.assertEqual(repaired["failed"], 0)
            self.assertTrue(target.read_text("utf-8").startswith("http://new-mediaflux.invalid/"))

    def test_unchanged_skipped_items_never_enter_failure_ledger(self):
        source_id = "source-skipped"
        video = GuangYaFile("video-current", "Current.mkv", False, 100, "etag", source_id)
        client = _TreeClient({source_id: [video]})

        with tempfile.TemporaryDirectory() as root:
            first = strm_module.sync_strm(
                source_id,
                "http://mediaflux.invalid",
                root,
                client=client,
                source_name="稳定源",
            )
            second = strm_module.sync_strm(
                source_id,
                "http://mediaflux.invalid",
                root,
                client=client,
                source_name="稳定源",
            )

        self.assertEqual(first["generated"], 1)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(db.list_strm_failures(status="open", limit=20), [])


if __name__ == "__main__":
    unittest.main()


class StrmProgressTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_index")
            conn.execute("DELETE FROM strm_failures")

    def test_sync_progress_is_stage_based_and_bounded_for_large_scans(self):
        source_id = "source-progress"
        files = [
            GuangYaFile(f"video-{index}", f"Movie-{index}.mkv", False, 100, f"e{index}", source_id)
            for index in range(101)
        ]
        client = _TreeClient({source_id: files})
        events: list[tuple[str, int, int, str]] = []

        with tempfile.TemporaryDirectory() as root:
            result = strm_module.sync_strm(
                source_id,
                "http://mediaflux.invalid",
                root,
                client=client,
                source_name="进度源",
                clean_invalid=False,
                on_progress=lambda stage, completed, total, detail: events.append(
                    (stage, completed, total, detail)
                ),
            )

        self.assertEqual(result["generated"], 101)
        stages = [event[0] for event in events]
        self.assertIn("scan", stages)
        self.assertIn("generate", stages)
        self.assertIn("complete", stages)
        generation = [event for event in events if event[0] == "generate"]
        self.assertLessEqual(len(generation), 11)
        percentages = [int(completed * 100 / total) for _, completed, total, _ in generation]
        self.assertEqual(percentages, sorted(set(percentages)))
        self.assertFalse(any("Movie-" in detail for *_, detail in events))

    def test_retry_progress_is_bounded_instead_of_emitted_per_failure(self):
        source_id = "source-retry-progress"
        files = [
            GuangYaFile(f"video-{index}", f"Retry-{index}.mkv", False, 100, f"e{index}", source_id)
            for index in range(55)
        ]
        for file in files:
            db.record_strm_failure(
                source_id=source_id,
                source_name="重试进度源",
                file_id=file.file_id,
                parent_id=source_id,
                filename=file.name,
                action="generate",
                rel_dir="",
                target_rel_path=f"{strm_module.STRM_SUBDIR}/{file.name}.strm",
                error="write failed",
            )
        ids = [row["id"] for row in db.list_strm_failures(status="open", limit=100)]
        client = _TreeClient({source_id: files})
        events = []

        with tempfile.TemporaryDirectory() as root:
            values = {
                "GY_STRM_SOURCE_DIRS": '[{"id":"source-retry-progress","name":"重试进度源"}]',
                "GY_STRM_BASE_URL": "http://mediaflux.invalid",
                "STRM_ROOT": root,
            }
            with patch(
                "app.modules.strm.get",
                side_effect=lambda key, default="": values.get(key, default),
            ):
                result = strm_module.retry_strm_failures(
                    ids,
                    "manual",
                    client=client,
                    on_progress=lambda *event: events.append(event),
                )

        self.assertEqual(result["resolved"], 55)
        self.assertLessEqual(len(events), 11)
        self.assertGreaterEqual(len(events), 2)

    def test_scheduler_emits_complete_only_after_all_sources_finish(self):
        from app.modules import scheduler as scheduler_module

        scheduler = scheduler_module.STRMScheduler()
        events = []
        empty_stats = {
            "total": 0, "generated": 0, "skipped": 0, "failed": 0,
            "metadata_total": 0, "metadata_generated": 0, "metadata_skipped": 0,
            "metadata_failed": 0, "metadata_cleaned": 0, "cleaned": 0,
            "empty_dirs_cleaned": 0, "directories": 0, "scan_elapsed_seconds": 0.0,
            "metadata_elapsed_seconds": 0.0, "error_samples": [], "changes": [],
            "omitted_count": 0,
        }

        def fake_sync(*args, **kwargs):
            kwargs["on_progress"]("complete", 1, 1, "单源完成")
            return dict(empty_stats)

        values = {"GY_STRM_BASE_URL": "http://mediaflux.invalid", "STRM_ROOT": "/tmp/strm"}
        with patch.object(scheduler, "validate_config", return_value=""), patch.object(
            scheduler, "_source_dirs", return_value=[
                {"id": "source-a", "name": "A"}, {"id": "source-b", "name": "B"}
            ]
        ), patch.object(scheduler, "_video_exts", return_value={"mkv"}), patch.object(
            scheduler, "_metadata_exts", return_value=set()
        ), patch.object(scheduler, "_refresh_media_servers", return_value={}), patch.object(
            scheduler, "_notify_success"
        ), patch.object(scheduler, "_notify_details"), patch(
            "app.modules.scheduler.sync_strm", side_effect=fake_sync
        ), patch(
            "app.modules.scheduler.get", side_effect=lambda key, default="": values.get(key, default)
        ), patch("app.modules.scheduler.get_int", return_value=0):
            result = scheduler.run_blocking(
                "manual", on_progress=lambda *event: events.append(event)
            )

        self.assertTrue(result["ok"])
        self.assertEqual([event[0] for event in events].count("complete"), 1)

    def test_telegram_progress_editor_has_a_hard_edit_limit(self):
        from app.bot import handlers

        class Bot:
            def __init__(self):
                self.edits = []

            def edit_message_text(self, text, chat_id, message_id):
                self.edits.append((text, chat_id, message_id))

        bot = Bot()
        callback = handlers._make_strm_progress_editor(bot, "chat", 42)
        for source in range(4):
            for stage in ("scan", "generate", "metadata", "cleanup", "complete"):
                for percent in range(0, 101, 10):
                    callback(stage, percent, 100, f"source {source}")

        self.assertLessEqual(len(bot.edits), 6)
        self.assertTrue(bot.edits)
        self.assertTrue(all(item[1:] == ("chat", 42) for item in bot.edits))


class StrmFailureApiUiTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_failures")

    @staticmethod
    def _failure(source_id: str, file_id: str, action: str) -> int:
        return db.record_strm_failure(
            source_id=source_id,
            source_name=f"来源 {source_id}",
            file_id=file_id,
            parent_id=source_id,
            filename=f"{file_id}.{'mkv' if action == 'generate' else 'nfo'}",
            action=action,
            rel_dir="目录",
            target_rel_path=f"光鸭云盘/目录/{file_id}",
            error="safe failure",
        )

    def test_failure_api_filters_and_returns_per_source_summary(self):
        from tests.test_strm_index_diagnostics import _api_client

        selected = self._failure("source-a", "video-a", "generate")
        self._failure("source-a", "meta-a", "metadata")
        self._failure("source-b", "video-b", "generate")

        with tempfile.TemporaryDirectory() as root:
            with _api_client(Path(root)) as (client, _):
                response = client.get(
                    "/api/strm/failures?source_id=source-a&action=generate&status=open"
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["items"]], [selected])
        self.assertEqual(payload["summary"]["open"], 3)
        self.assertEqual(payload["summary"]["by_source"]["source-a"], 2)
        self.assertNotIn("https://", str(payload))

    def test_retry_api_supports_selected_and_all_current_filters(self):
        from tests.test_strm_index_diagnostics import _api_client

        first = self._failure("source-a", "video-a", "generate")
        self._failure("source-a", "video-b", "generate")
        self._failure("source-a", "meta-a", "metadata")
        self._failure("source-b", "video-c", "generate")

        def fake_selected(ids, trigger_type):
            return {
                "ok": True, "requested": len(ids), "attempted": len(ids),
                "batches": 1, "resolved": len(ids), "failed": 0,
            }

        def fake_all(source_id, action, trigger_type):
            return {
                "ok": True, "requested": 2, "attempted": 2, "batches": 1,
                "resolved": 2, "failed": 0,
            }

        with tempfile.TemporaryDirectory() as root, patch(
            "app.routes.strm_api.retry_strm_failures", side_effect=fake_selected
        ) as selected_retry, patch(
            "app.routes.strm_api.retry_all_strm_failures", side_effect=fake_all
        ) as all_retry:
            with _api_client(Path(root)) as (client, csrf):
                selected_response = client.post(
                    "/api/strm/failures/retry",
                    headers={"X-CSRF-Token": csrf},
                    json={"ids": [first]},
                )
                all_response = client.post(
                    "/api/strm/failures/retry",
                    headers={"X-CSRF-Token": csrf},
                    json={
                        "all": True,
                        "source_id": "source-a",
                        "action": "generate",
                    },
                )

        self.assertEqual(selected_response.status_code, 200, selected_response.text)
        self.assertEqual(all_response.status_code, 200, all_response.text)
        selected_retry.assert_called_once_with([first], "web")
        all_retry.assert_called_once_with("source-a", "generate", "web")

    def test_retry_api_returns_real_error_when_service_cannot_start(self):
        from tests.test_strm_index_diagnostics import _api_client

        failure_id = self._failure("source-a", "video-a", "generate")
        with tempfile.TemporaryDirectory() as root, patch(
            "app.routes.strm_api.retry_strm_failures",
            return_value={"ok": False, "error": "STRM 同步或重试任务正在运行"},
        ):
            with _api_client(Path(root)) as (client, csrf):
                response = client.post(
                    "/api/strm/failures/retry",
                    headers={"X-CSRF-Token": csrf},
                    json={"ids": [failure_id]},
                )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"error": "STRM 同步或重试任务正在运行"})

    def test_retry_all_api_maps_busy_and_service_errors(self):
        from app.routes import strm_api
        from tests.test_strm_index_diagnostics import _api_client

        cases = [
            ("STRM 同步或重试任务正在运行", 409),
            ("当前筛选没有可重试失败项", 400),
        ]
        for error, expected_status in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as root, patch.object(
                strm_api,
                "retry_all_strm_failures",
                return_value={"ok": False, "error": error},
            ) as retry_all:
                with _api_client(Path(root)) as (client, csrf):
                    response = client.post(
                        "/api/strm/failures/retry",
                        headers={"X-CSRF-Token": csrf},
                        json={"all": True, "source_id": "source-a", "action": "generate"},
                    )
            self.assertEqual(response.status_code, expected_status, response.text)
            self.assertEqual(response.json(), {"error": error})
            retry_all.assert_called_once_with("source-a", "generate", "web")

    def test_failure_routes_require_login_and_retry_requires_csrf(self):
        from tests.test_strm_index_diagnostics import _api_client

        failure_id = self._failure("source-a", "video-a", "generate")
        with tempfile.TemporaryDirectory() as root:
            with _api_client(Path(root), login=False) as (anonymous, _):
                self.assertEqual(anonymous.get("/api/strm/failures").status_code, 401)
                self.assertEqual(
                    anonymous.post(
                        "/api/strm/failures/clear", json={"ids": [failure_id]}
                    ).status_code,
                    401,
                )
            with _api_client(Path(root)) as (client, _):
                response = client.post(
                    "/api/strm/failures/retry", json={"ids": [failure_id]}
                )
                self.assertEqual(response.status_code, 403)
                response = client.post(
                    "/api/strm/failures/clear", json={"ids": [failure_id]}
                )
                self.assertEqual(response.status_code, 403)

    def test_clear_failures_by_ids_and_by_filter(self):
        from tests.test_strm_index_diagnostics import _api_client

        f1 = self._failure("source-a", "file-1", "generate")
        self._failure("source-a", "file-2", "generate")
        self._failure("source-b", "file-3", "metadata")
        with tempfile.TemporaryDirectory() as root:
            with _api_client(Path(root)) as (client, csrf):
                # Clear f1 by id
                response = client.post(
                    "/api/strm/failures/clear",
                    headers={"X-CSRF-Token": csrf},
                    json={"ids": [f1]},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json().get("deleted"), 1)
                self.assertEqual(db.count_strm_failures(status="open"), 2)

                # Clear remaining in source-a
                response = client.post(
                    "/api/strm/failures/clear",
                    headers={"X-CSRF-Token": csrf},
                    json={"all": True, "source_id": "source-a"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json().get("deleted"), 1)
                self.assertEqual(db.count_strm_failures(status="open"), 1)

    def test_failures_api_pagination(self):
        from tests.test_strm_index_diagnostics import _api_client

        # Create 5 failure records
        for i in range(5):
            self._failure("source-page", f"file-{i}", "generate")

        with tempfile.TemporaryDirectory() as root:
            with _api_client(Path(root)) as (client, _):
                response = client.get("/api/strm/failures?source_id=source-page&page=2&page_size=2")
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(len(data.get("items", [])), 2)
                pagination = data.get("pagination", {})
                self.assertEqual(pagination.get("page"), 2)
                self.assertEqual(pagination.get("page_size"), 2)
                self.assertEqual(pagination.get("total"), 5)
                self.assertEqual(pagination.get("total_pages"), 3)

    def test_page_keeps_diagnostics_and_reserves_stable_failure_ledger(self):
        from tests.test_strm_index_diagnostics import _api_client

        with tempfile.TemporaryDirectory() as root:
            with _api_client(Path(root)) as (client, _):
                response = client.get("/guangya/strm")

        html = response.text + Path("app/static/js/guangya-strm.js").read_text(encoding="utf-8")
        self.assertIn('id="strmIndexDiagnosticCard"', html)
        self.assertIn('id="strmFailureLedgerCard"', html)
        self.assertIn('id="strmFailureList"', html)
        self.assertIn('id="strmFailureSourceFilter"', html)
        self.assertIn('id="retrySelectedStrmFailuresBtn"', html)
        self.assertIn('id="retryAllStrmFailuresBtn"', html)
        self.assertIn('id="selectAllStrmFailures"', html)
        self.assertIn('id="clearStrmFailuresBtn"', html)
        self.assertIn('id="strmFailurePagination"', html)
        self.assertIn('id="strmFailurePrev"', html)
        self.assertIn('id="strmFailureNext"', html)
        self.assertIn('id="strmFailurePageInfo"', html)
        self.assertIn("height: 360px", html)
        self.assertIn("let failureSnapshot=[]", html)
        self.assertIn("保留上次失败列表", html)
        self.assertIn("'/api/strm/failures/retry'", html)
        self.assertIn("'/api/strm/failures/clear'", html)


class StrmRetrySourcePlanningAndConfigTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_index")
            conn.execute("DELETE FROM strm_failures")

    @staticmethod
    def _failure(source_id: str, source_name: str, file_id: str, filename: str) -> int:
        return db.record_strm_failure(
            source_id=source_id, source_name=source_name, file_id=file_id,
            parent_id=source_id, filename=filename, action="generate", rel_dir="",
            target_rel_path=f"光鸭云盘/{filename}.strm", error="write failed",
        )

    @staticmethod
    def _config(root: str, sources: str, base_url: str = "http://mediaflux.invalid"):
        values = {
            "GY_STRM_SOURCE_DIRS": sources,
            "GY_STRM_BASE_URL": base_url,
            "STRM_ROOT": root,
        }
        return patch(
            "app.modules.strm.get",
            side_effect=lambda key, default="": values.get(key, default),
        )

    @staticmethod
    def _runtime(root: str, source_id: str = "source-owned") -> dict:
        return {
            "base_url": "http://mediaflux.invalid",
            "strm_root": root,
            "sources": [{
                "id": source_id,
                "name": "Owned Client Source",
                "rel_prefix": "",
                "source_key": f"guangya:{source_id}",
                "metadata_source_key": f"guangya-meta:{source_id}",
            }],
        }

    def test_retry_closes_internally_created_client_on_success(self):
        failure_id = self._failure(
            "source-owned", "Owned Client Source", "missing", "Missing.mkv"
        )
        client = Mock()
        lookup = strm_module._RetryLookupResult(
            located={},
            directories=1,
            entries=0,
            scan_incomplete=True,
            scan_limit_reason="directory_error",
            stopped=False,
        )
        with tempfile.TemporaryDirectory() as root, patch(
            "app.modules.strm.GuangYaClient", return_value=client
        ), patch(
            "app.modules.strm._locate_retry_files", return_value=lookup
        ):
            result = strm_module.retry_strm_failures(
                [failure_id],
                "manual",
                runtime_config=self._runtime(root),
            )

        self.assertEqual(result["deferred"], 1)
        client.close.assert_called_once_with()

    def test_retry_closes_internally_created_client_on_exception(self):
        failure_id = self._failure(
            "source-owned", "Owned Client Source", "broken", "Broken.mkv"
        )
        client = Mock()
        with tempfile.TemporaryDirectory() as root, patch(
            "app.modules.strm.GuangYaClient", return_value=client
        ), patch(
            "app.modules.strm._locate_retry_files",
            side_effect=RuntimeError("injected locate failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected locate failure"):
                strm_module.retry_strm_failures(
                    [failure_id],
                    "manual",
                    runtime_config=self._runtime(root),
                )

        client.close.assert_called_once_with()

    def test_retry_does_not_close_injected_client(self):
        failure_id = self._failure(
            "source-owned", "Owned Client Source", "external", "External.mkv"
        )
        client = Mock()
        lookup = strm_module._RetryLookupResult(
            located={},
            directories=1,
            entries=0,
            scan_incomplete=True,
            scan_limit_reason="directory_error",
            stopped=False,
        )
        with tempfile.TemporaryDirectory() as root, patch(
            "app.modules.strm._locate_retry_files", return_value=lookup
        ):
            result = strm_module.retry_strm_failures(
                [failure_id],
                "manual",
                client=client,
                runtime_config=self._runtime(root),
            )

        self.assertEqual(result["deferred"], 1)
        client.close.assert_not_called()

    def test_scheduler_uses_the_shared_duplicate_name_source_plan(self):
        from app.modules import scheduler as scheduler_module

        values = {
            "GY_STRM_SOURCE_DIRS": (
                '[{"id":"source-a-abc111","name":"同名"},'
                '{"id":"source-b-xyz222","name":"同名"}]'
            ),
        }
        with patch(
            "app.modules.strm.get",
            side_effect=lambda key, default="": values.get(key, default),
        ):
            plans = scheduler_module.STRMScheduler._source_dirs()

        self.assertEqual(
            [(row["rel_prefix"], row["source_key"]) for row in plans],
            [
                ("同名 (abc111)", "guangya:source-a-abc111"),
                ("同名 (xyz222)", "guangya:source-b-xyz222"),
            ],
        )

    def test_retry_separates_same_filename_across_two_sources_like_normal_sync(self):
        source_a, source_b = "source-a-111111", "source-b-222222"
        first = GuangYaFile("video-a", "Movie.mkv", False, 100, "etag-a", source_a)
        second = GuangYaFile("video-b", "Movie.mkv", False, 200, "etag-b", source_b)
        ids = [
            self._failure(source_a, "电影一", first.file_id, first.name),
            self._failure(source_b, "电影二", second.file_id, second.name),
        ]
        client = _TreeClient({source_a: [first], source_b: [second]})
        sources = (
            '[{"id":"source-a-111111","name":"电影一"},'
            '{"id":"source-b-222222","name":"电影二"}]'
        )

        with tempfile.TemporaryDirectory() as root, self._config(root, sources):
            result = strm_module.retry_strm_failures(ids, "manual", client=client)
            first_target = Path(root) / strm_module.STRM_SUBDIR / "电影一" / "Movie.strm"
            second_target = Path(root) / strm_module.STRM_SUBDIR / "电影二" / "Movie.strm"
            self.assertTrue(first_target.is_file())
            self.assertTrue(second_target.is_file())
            first_text = first_target.read_text("utf-8")
            second_text = second_target.read_text("utf-8")

        self.assertEqual(result["resolved"], 2)
        self.assertIn("/playgy/video-a/", first_text)
        self.assertIn("/playgy/video-b/", second_text)

    def test_retry_disambiguates_duplicate_source_names_with_last_six_id_chars(self):
        source_a, source_b = "source-a-abc111", "source-b-xyz222"
        first = GuangYaFile("video-a", "A.mkv", False, 100, "a", source_a)
        second = GuangYaFile("video-b", "B.mkv", False, 100, "b", source_b)
        ids = [
            self._failure(source_a, "同名", first.file_id, first.name),
            self._failure(source_b, "同名", second.file_id, second.name),
        ]
        client = _TreeClient({source_a: [first], source_b: [second]})
        sources = (
            '[{"id":"source-a-abc111","name":"同名"},'
            '{"id":"source-b-xyz222","name":"同名"}]'
        )

        with tempfile.TemporaryDirectory() as root, self._config(root, sources):
            result = strm_module.retry_strm_failures(ids, "manual", client=client)
            self.assertTrue(
                (Path(root) / strm_module.STRM_SUBDIR / "同名 (abc111)" / "A.strm").is_file()
            )
            self.assertTrue(
                (Path(root) / strm_module.STRM_SUBDIR / "同名 (xyz222)" / "B.strm").is_file()
            )

        self.assertEqual(result["resolved"], 2)

    def test_moved_file_uses_new_source_prefix_and_namespace(self):
        old_source, new_source, peer_source = "old-source", "new-source-333333", "peer-444444"
        failure_id = self._failure(old_source, "旧来源", "moved", "Old.mkv")
        moved = GuangYaFile("moved", "Moved.mkv", False, 300, "fresh", new_source)
        client = _TreeClient({new_source: [moved], peer_source: []})
        sources = (
            '[{"id":"new-source-333333","name":"新来源"},'
            '{"id":"peer-444444","name":"其他来源"}]'
        )

        with tempfile.TemporaryDirectory() as root, self._config(root, sources):
            result = strm_module.retry_strm_failures([failure_id], "manual", client=client)
            target = Path(root) / strm_module.STRM_SUBDIR / "新来源" / "Moved.strm"
            rows = db.list_strm_index(f"guangya:{new_source}")
            target_exists = target.is_file()

        self.assertEqual(result["resolved"], 1)
        self.assertTrue(target_exists)
        self.assertEqual([row["file_id"] for row in rows], ["moved"])

    def test_invalid_or_missing_retry_config_does_not_scan_write_or_resolve(self):
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as valid_root:
            cases = [
                ("", '[{"id":"source","name":"源"}]', "http://mediaflux.invalid"),
                (valid_root, '[{"id":"source","name":"源"}]', ""),
                (valid_root, "{bad-json", "http://mediaflux.invalid"),
                (valid_root, "[]", "http://mediaflux.invalid"),
            ]
            with chdir(cwd):
                for index, (root, sources, base_url) in enumerate(cases):
                    with self.subTest(index=index):
                        with db.get_conn() as conn:
                            conn.execute("DELETE FROM strm_failures")
                        failure_id = self._failure("source", "源", f"video-{index}", "Movie.mkv")
                        client = _TreeClient({
                            "source": [GuangYaFile(f"video-{index}", "Movie.mkv", False, 1, "e")]
                        })
                        with self._config(root, sources, base_url):
                            result = strm_module.retry_strm_failures(
                                [failure_id], "manual", client=client
                            )
                        self.assertFalse(result["ok"])
                        self.assertEqual(client.list_calls, [])
                        row = db.list_strm_failures(
                            status="open", ids=[failure_id], limit=1
                        )[0]
                        self.assertEqual(row["status"], "open")
                self.assertFalse((Path(cwd) / strm_module.STRM_SUBDIR).exists())

    def test_retry_rejects_target_that_resolves_outside_strm_root(self):
        source = "source-safe"
        file = GuangYaFile("video", "Movie.mkv", False, 1, "e", source)
        failure_id = self._failure(source, "安全源", file.file_id, file.name)
        client = _TreeClient({source: [file]})
        sources = '[{"id":"source-safe","name":"安全源"}]'

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside, \
                self._config(root, sources), patch(
                    "app.modules.strm._strm_target", return_value=Path(outside) / "escape.strm"
                ), patch("app.modules.strm.generate_strm") as generate:
            result = strm_module.retry_strm_failures([failure_id], "manual", client=client)

        self.assertEqual(result["resolved"], 0)
        self.assertEqual(result["failed"], 1)
        generate.assert_not_called()
        self.assertEqual(db.list_strm_failures(status="open", ids=[failure_id], limit=1)[0]["status"], "open")


class StrmRetryConcurrencyStateTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_failures")
            conn.execute("DELETE FROM strm_index")

    @staticmethod
    def _failure(file_id: str = "video") -> int:
        return db.record_strm_failure(
            source_id="source-lock", source_name="锁来源", file_id=file_id,
            parent_id="source-lock", filename=f"{file_id}.mkv", action="generate",
            rel_dir="", target_rel_path=f"光鸭云盘/{file_id}.mkv.strm",
            error="initial failure",
        )

    def test_atomic_claim_only_transitions_open_rows_once(self):
        failure_id = self._failure()

        first = db.claim_strm_failures([failure_id])
        second = db.claim_strm_failures([failure_id])

        self.assertEqual([row["id"] for row in first], [failure_id])
        self.assertEqual(second, [])
        row = db.list_strm_failures(status="retrying", ids=[failure_id], limit=1)[0]
        self.assertEqual(row["status"], "retrying")
        self.assertEqual(row["retry_count"], 1)

    def test_init_db_recovers_interrupted_retry_claim_to_open(self):
        failure_id = self._failure("interrupted")
        claimed = db.claim_strm_failures([failure_id])
        self.assertEqual(len(claimed), 1)

        db.init_db()

        row = db.list_strm_failures(
            status="open", ids=[failure_id], limit=1
        )[0]
        self.assertEqual(row["status"], "open")
        self.assertIn("进程中断", row["error"])
        self.assertEqual(row["retry_count"], 1)

    def test_late_retry_failure_cannot_overwrite_resolved_claim(self):
        failure_id = self._failure()
        claimed = db.claim_strm_failures([failure_id])
        self.assertEqual(len(claimed), 1)
        file = GuangYaFile("video", "Movie.mkv", False, 1, "e", "source-lock")

        self.assertTrue(db.resolve_strm_failure(failure_id, expected_status="retrying"))
        updated = db.update_strm_failure_retry(
            failure_id, source_id="source-lock", source_name="锁来源", file=file,
            rel_dir="", target_rel_path="光鸭云盘/Movie.mkv.strm",
            error="late failure", expected_status="retrying",
        )

        self.assertFalse(updated)
        row = db.list_strm_failures(status="resolved", ids=[failure_id], limit=1)[0]
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["error"], "initial failure")

    def test_full_sync_and_retry_share_one_operation_mutex(self):
        from app.modules.scheduler import STRMScheduler

        failure_id = self._failure()
        client = _TreeClient({"source-lock": []})
        scheduler = STRMScheduler()
        values = {
            "GY_STRM_SOURCE_DIRS": '[{"id":"source-lock","name":"锁来源"}]',
            "GY_STRM_BASE_URL": "http://mediaflux.invalid",
        }
        with tempfile.TemporaryDirectory() as root:
            values["STRM_ROOT"] = root
            acquired = strm_module.STRM_OPERATION_LOCK.acquire(blocking=False)
            self.assertTrue(acquired)
            try:
                with patch(
                    "app.modules.strm.get",
                    side_effect=lambda key, default="": values.get(key, default),
                ):
                    retry = strm_module.retry_strm_failures(
                        [failure_id], "manual", client=client
                    )
                sync = scheduler.run_blocking("manual")
            finally:
                strm_module.STRM_OPERATION_LOCK.release()

        self.assertFalse(retry["ok"])
        self.assertIn("运行", retry["error"])
        self.assertFalse(sync["ok"])
        self.assertEqual(client.list_calls, [])
        self.assertEqual(
            db.list_strm_failures(status="open", ids=[failure_id], limit=1)[0]["status"],
            "open",
        )

    def test_retry_claim_is_released_to_open_after_processing_failure(self):
        failure_id = self._failure()
        file = GuangYaFile("video", "Movie.mkv", False, 1, "e", "source-lock")
        client = _TreeClient({"source-lock": [file]})
        values = {
            "GY_STRM_SOURCE_DIRS": '[{"id":"source-lock","name":"锁来源"}]',
            "GY_STRM_BASE_URL": "http://mediaflux.invalid",
        }
        with tempfile.TemporaryDirectory() as root:
            values["STRM_ROOT"] = root
            with patch(
                "app.modules.strm.get",
                side_effect=lambda key, default="": values.get(key, default),
            ), patch("app.modules.strm.generate_strm", side_effect=RuntimeError("retry failed")):
                result = strm_module.retry_strm_failures(
                    [failure_id], "manual", client=client
                )

        self.assertEqual(result["failed"], 1)
        row = db.list_strm_failures(status="open", ids=[failure_id], limit=1)[0]
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["retry_count"], 1)
        self.assertIn("retry failed", row["error"])


class StrmMetadataRetryTransactionAndMissingTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_failures")
            conn.execute("DELETE FROM strm_index")

    @staticmethod
    def _failure(source: str, file_id: str, filename: str, action: str = "metadata") -> int:
        return db.record_strm_failure(
            source_id=source, source_name="元数据源", file_id=file_id,
            parent_id=source, filename=filename, action=action, rel_dir="",
            target_rel_path=f"光鸭云盘/{filename}", error="initial failure",
        )

    @staticmethod
    def _config(root: str, source: str = "source-meta"):
        values = {
            "GY_STRM_SOURCE_DIRS": f'[{ {"id": source, "name": "元数据源"} }]'.replace("'", '"'),
            "GY_STRM_BASE_URL": "http://mediaflux.invalid",
            "STRM_ROOT": root,
        }
        return patch(
            "app.modules.strm.get",
            side_effect=lambda key, default="": values.get(key, default),
        )

    def test_metadata_retry_restores_old_file_and_index_when_upsert_fails(self):
        source = "source-meta"
        source_key = f"guangya-meta:{source}"
        file = GuangYaFile("meta", "poster.jpg", False, 8, "fresh", source)
        client = _TreeClient({source: [file]})
        failure_id = self._failure(source, file.file_id, file.name)

        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / strm_module.STRM_SUBDIR / "poster.jpg"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old-data")
            db.upsert_strm_index(source_key, file.file_id, "old", 8, file.name, str(target))
            original_upsert = db.upsert_strm_index

            def fail_fresh(source_name, file_id, *args, **kwargs):
                if source_name == source_key and file_id == file.file_id:
                    raise RuntimeError("metadata index failed")
                return original_upsert(source_name, file_id, *args, **kwargs)

            with self._config(root, source), patch(
                "app.modules.strm.requests.get", return_value=_Response(b"new-data")
            ), patch(
                "app.modules.strm.db.upsert_strm_index", side_effect=fail_fresh
            ):
                result = strm_module.retry_strm_failures(
                    [failure_id], "manual", client=client
                )

            row = db.list_strm_index(source_key)[0]
            self.assertEqual(target.read_bytes(), b"old-data")
            self.assertEqual(row["etag"], "old")
            self.assertEqual(row["strm_path"], str(target))

        self.assertEqual(result["failed"], 1)
        self.assertEqual(db.list_strm_failures(status="open", ids=[failure_id], limit=1)[0]["status"], "open")

    def test_metadata_retry_rename_removes_old_path_and_updates_index(self):
        source = "source-meta"
        source_key = f"guangya-meta:{source}"
        file = GuangYaFile("meta", "new.jpg", False, 8, "fresh", source)
        client = _TreeClient({source: [file]})
        failure_id = self._failure(source, file.file_id, "old.jpg")

        with tempfile.TemporaryDirectory() as root:
            old_path = Path(root) / strm_module.STRM_SUBDIR / "old.jpg"
            old_path.parent.mkdir(parents=True)
            old_path.write_bytes(b"old")
            db.upsert_strm_index(
                source_key, file.file_id, "old", 3, "old.jpg", str(old_path),
                f"sha256:{hashlib.sha256(b'old').hexdigest()}",
            )
            with self._config(root, source), patch(
                "app.modules.strm.requests.get", return_value=_Response(b"new-data")
            ):
                result = strm_module.retry_strm_failures(
                    [failure_id], "manual", client=client
                )
            new_path = Path(root) / strm_module.STRM_SUBDIR / "new.jpg"
            row = db.list_strm_index(source_key)[0]
            self.assertFalse(old_path.exists())
            self.assertEqual(new_path.read_bytes(), b"new-data")
            self.assertEqual(row["strm_path"], str(new_path))

        self.assertEqual(result["resolved"], 1)

    def test_metadata_retry_replaces_conflicting_target_index(self):
        source = "source-meta"
        source_key = f"guangya-meta:{source}"
        fresh = GuangYaFile("fresh", "poster.jpg", False, 8, "fresh", source)
        client = _TreeClient({source: [fresh]})
        failure_id = self._failure(source, fresh.file_id, fresh.name)

        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / strm_module.STRM_SUBDIR / "poster.jpg"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"conflict")
            db.upsert_strm_index(
                source_key, "stale", "old", 8, "poster.jpg", str(target),
                f"sha256:{hashlib.sha256(b'conflict').hexdigest()}",
            )
            with self._config(root, source), patch(
                "app.modules.strm.requests.get", return_value=_Response(b"fresh-data")
            ):
                result = strm_module.retry_strm_failures(
                    [failure_id], "manual", client=client
                )
            rows = db.list_strm_index(source_key)
            self.assertEqual([row["file_id"] for row in rows], ["fresh"])
            self.assertEqual(target.read_bytes(), b"fresh-data")

        self.assertEqual(result["resolved"], 1)

    def test_all_missing_items_return_open_stale_and_finish_progress(self):
        source = "source-meta"
        ids = [self._failure(source, f"missing-{index}", f"M{index}.jpg") for index in range(3)]
        client = _TreeClient({source: []})
        events = []
        with tempfile.TemporaryDirectory() as root, self._config(root, source):
            result = strm_module.retry_strm_failures(
                ids, "manual", client=client, on_progress=lambda *event: events.append(event)
            )

        self.assertEqual(result["missing"], 3)
        self.assertEqual(result["stale"], 3)
        self.assertEqual(result["deferred"], 0)
        self.assertFalse(result["scan_incomplete"])
        rows = db.list_strm_failures(status="open", ids=ids, limit=10)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["retry_count"] == 1 for row in rows))
        self.assertTrue(all(row["failure_count"] == 2 for row in rows))
        self.assertTrue(all("stale" in row["error"] for row in rows))
        self.assertEqual(events[-1][1:3], (3, 3))

    def test_partial_missing_items_resolve_found_and_finish_total_progress(self):
        source = "source-meta"
        found = GuangYaFile("found", "Movie.mkv", False, 10, "e", source)
        found_id = self._failure(source, "found", "Movie.mkv", action="generate")
        missing_id = self._failure(source, "missing", "Missing.mkv", action="generate")
        client = _TreeClient({source: [found]})
        events = []
        with tempfile.TemporaryDirectory() as root, self._config(root, source):
            result = strm_module.retry_strm_failures(
                [found_id, missing_id], "manual", client=client,
                on_progress=lambda *event: events.append(event),
            )

        self.assertEqual(result["resolved"], 1)
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["stale"], 1)
        self.assertEqual(db.list_strm_failures(status="resolved", ids=[found_id], limit=1)[0]["status"], "resolved")
        self.assertEqual(db.list_strm_failures(status="open", ids=[missing_id], limit=1)[0]["status"], "open")
        self.assertEqual(events[-1][1:3], (2, 2))

    def test_incomplete_retry_scan_defers_unlocated_failure_without_stale_mark(self):
        source = "source-meta"
        failure_id = self._failure(source, "wanted", "Episode.mkv", action="generate")
        client = _TreeClient({
            source: [GuangYaFile("child", "Child", True, parent_id=source)],
            "child": [GuangYaFile("wanted", "Episode.mkv", False, 10, "e", "child")],
        })
        with tempfile.TemporaryDirectory() as root, self._config(root, source), patch(
            "app.modules.strm._scan_limits", return_value=(1, 100, 100, 60)
        ):
            result = strm_module.retry_strm_failures(
                [failure_id], "manual", client=client
            )

        self.assertTrue(result["scan_incomplete"])
        self.assertEqual(result["scan_limit_reason"], "directories")
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["stale"], 0)
        self.assertEqual(result["missing"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(client.list_calls, [source])
        row = db.list_strm_failures(status="open", ids=[failure_id], limit=1)[0]
        self.assertEqual(row["retry_count"], 1)
        self.assertEqual(row["failure_count"], 1)
        self.assertIn("扫描不完整", row["error"])
        self.assertNotIn("stale", row["error"].lower())
        self.assertEqual(
            db.list_strm_failures(status="retrying", ids=[failure_id], limit=1),
            [],
        )

    def test_directory_error_defers_unlocated_failure_without_stale_mark(self):
        source = "source-meta"
        failure_id = self._failure(source, "wanted", "Episode.mkv", action="generate")
        client = _TreeClient({source: RuntimeError("temporary remote failure")})
        with tempfile.TemporaryDirectory() as root, self._config(root, source):
            result = strm_module.retry_strm_failures(
                [failure_id], "manual", client=client
            )

        self.assertTrue(result["scan_incomplete"])
        self.assertEqual(result["scan_limit_reason"], "directory_error")
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["stale"], 0)
        row = db.list_strm_failures(status="open", ids=[failure_id], limit=1)[0]
        self.assertEqual(row["failure_count"], 1)
        self.assertIn("扫描不完整", row["error"])

    def test_retry_scan_stop_keeps_unlocated_failure_retriable(self):
        source = "source-meta"
        failure_id = self._failure(source, "wanted", "Episode.mkv", action="generate")
        client = _TreeClient({
            source: [GuangYaFile("child", "Child", True, parent_id=source)],
            "child": [GuangYaFile("wanted", "Episode.mkv", False, 10, "e", "child")],
        })
        with tempfile.TemporaryDirectory() as root, self._config(root, source):
            result = strm_module.retry_strm_failures(
                [failure_id],
                "manual",
                client=client,
                should_stop=lambda: bool(client.list_calls),
            )

        self.assertTrue(result["stopped"])
        self.assertEqual(result["stop_stage"], "scan")
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["stale"], 0)
        self.assertEqual(result["resolved"], 0)
        self.assertEqual(client.list_calls, [source])
        row = db.list_strm_failures(status="open", ids=[failure_id], limit=1)[0]
        self.assertEqual(row["retry_count"], 1)
        self.assertEqual(row["failure_count"], 1)
        self.assertIn("扫描已停止", row["error"])
        self.assertEqual(
            db.list_strm_failures(status="retrying", ids=[failure_id], limit=1),
            [],
        )


class StrmRetryBatchAndSchedulerSafetyTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_failures")
            conn.execute("DELETE FROM strm_index")

    @staticmethod
    def _bulk_failures(count: int) -> None:
        timestamp = db.now()
        rows = [
            (
                "source-batch", "批量源", f"file-{index}", "source-batch",
                f"Movie-{index}.mkv", "generate", "",
                f"光鸭云盘/Movie-{index}.mkv.strm", "failure", "open",
                1, 0, timestamp, timestamp,
            )
            for index in range(count)
        ]
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO strm_failures("
                "source_id,source_name,file_id,parent_id,filename,action,rel_dir,"
                "target_rel_path,error,status,failure_count,retry_count,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

    @staticmethod
    def _bulk_runtime(root: str) -> dict:
        return {
            "base_url": "http://127.0.0.1:1258",
            "strm_root": root,
            "sources": [{
                "id": "source-batch", "name": "批量源", "rel_prefix": "",
                "source_key": "guangya:source-batch",
                "metadata_source_key": "guangya-meta:source-batch",
            }],
        }

    def test_retry_all_processes_more_than_one_thousand_in_bounded_batches(self):
        self._bulk_failures(1005)
        files = [
            GuangYaFile(
                f"file-{index}", f"Movie-{index}.mkv", False, 1,
                f"etag-{index}", "source-batch",
            )
            for index in range(1005)
        ]
        client = _TreeClient({"source-batch": files})
        progress_events = []
        with tempfile.TemporaryDirectory() as root, patch(
            "app.modules.strm._install_video_candidate", return_value=(0, "")
        ), patch("app.modules.strm._update_video_index_snapshot", return_value=None):
            result = strm_module.retry_all_strm_failures(
                "source-batch", "generate", "web",
                client=client, runtime_config=self._bulk_runtime(root),
                on_progress=lambda *event: progress_events.append(event),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(client.list_calls, ["source-batch"])
        self.assertEqual(result["attempted"], 1005)
        self.assertEqual(result["matched"], 1005)
        self.assertEqual(result["resolved"], 1005)
        self.assertEqual(result["batches"], 2)
        self.assertLessEqual(len(progress_events), 11)
        self.assertEqual(progress_events[-1][1:3], (1005, 1005))
        self.assertEqual(db.count_strm_failures(status="open"), 0)
        self.assertEqual(db.list_strm_failures(status="retrying", limit=1000), [])

    def test_retry_all_defers_every_unresolved_item_after_one_incomplete_scan(self):
        self._bulk_failures(1005)
        files = [
            GuangYaFile(
                f"file-{index}", f"Movie-{index}.mkv", False, 1,
                f"etag-{index}", "source-batch",
            )
            for index in range(1005)
        ]
        client = _TreeClient({"source-batch": files})
        with tempfile.TemporaryDirectory() as root, patch(
            "app.modules.strm._scan_limits", return_value=(10, 1, 10000, 30.0)
        ), patch(
            "app.modules.strm._install_video_candidate", return_value=(0, "")
        ), patch("app.modules.strm._update_video_index_snapshot", return_value=None):
            result = strm_module.retry_all_strm_failures(
                "source-batch", "generate", "web",
                client=client, runtime_config=self._bulk_runtime(root),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(client.list_calls, ["source-batch"])
        self.assertEqual(result["attempted"], 1005)
        self.assertEqual(result["batches"], 2)
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(result["deferred"], 1004)
        self.assertTrue(result["scan_incomplete"])
        self.assertEqual(result["scan_limit_reason"], "entries")
        self.assertEqual(db.count_strm_failures(status="open"), 1004)
        self.assertEqual(db.list_strm_failures(status="retrying", limit=1000), [])

    def test_external_progress_callback_exception_does_not_fail_or_stick_scheduler(self):
        from app.modules import scheduler as scheduler_module

        scheduler = scheduler_module.STRMScheduler()
        empty_stats = {
            "total": 0, "generated": 0, "skipped": 0, "failed": 0,
            "metadata_total": 0, "metadata_generated": 0, "metadata_skipped": 0,
            "metadata_failed": 0, "metadata_cleaned": 0, "cleaned": 0,
            "empty_dirs_cleaned": 0, "directories": 0, "scan_elapsed_seconds": 0.0,
            "metadata_elapsed_seconds": 0.0, "error_samples": [], "changes": [],
            "omitted_count": 0,
        }

        def fake_sync(*args, **kwargs):
            kwargs["on_progress"]("scan", 1, 1, "扫描")
            return dict(empty_stats)

        values = {"GY_STRM_BASE_URL": "http://mediaflux.invalid", "STRM_ROOT": "/tmp/strm"}
        with patch.object(scheduler, "validate_config", return_value=""), patch.object(
            scheduler, "_source_dirs", return_value=[{
                "id": "source", "name": "来源", "rel_prefix": "",
                "source_key": "guangya:source", "metadata_source_key": "guangya-meta:source",
            }]
        ), patch.object(scheduler, "_video_exts", return_value={"mkv"}), patch.object(
            scheduler, "_metadata_exts", return_value=set()
        ), patch.object(scheduler, "_refresh_media_servers", return_value={}), patch.object(
            scheduler, "_notify_success"
        ), patch.object(scheduler, "_notify_details"), patch(
            "app.modules.scheduler.sync_strm", side_effect=fake_sync
        ), patch(
            "app.modules.scheduler.get", side_effect=lambda key, default="": values.get(key, default)
        ), patch("app.modules.scheduler.get_int", return_value=0):
            result = scheduler.run_blocking(
                "manual", on_progress=lambda *args: (_ for _ in ()).throw(RuntimeError("ui gone"))
            )

        self.assertTrue(result["ok"])
        self.assertFalse(scheduler.status()["running"])

    def test_scheduler_result_exposes_per_source_runtime_states(self):
        from app.modules import scheduler as scheduler_module

        scheduler = scheduler_module.STRMScheduler()
        sources = [
            {"id": "a", "name": "A", "rel_prefix": "A", "source_key": "guangya:a", "metadata_source_key": "guangya-meta:a"},
            {"id": "b", "name": "B", "rel_prefix": "B", "source_key": "guangya:b", "metadata_source_key": "guangya-meta:b"},
        ]
        empty_stats = {
            "total": 1, "generated": 1, "skipped": 0, "failed": 0,
            "metadata_total": 0, "metadata_generated": 0, "metadata_skipped": 0,
            "metadata_failed": 0, "metadata_cleaned": 0, "cleaned": 0,
            "empty_dirs_cleaned": 0, "directories": 1, "scan_elapsed_seconds": 0.1,
            "metadata_elapsed_seconds": 0.0, "error_samples": [], "changes": [],
            "omitted_count": 0,
        }
        values = {"GY_STRM_BASE_URL": "http://mediaflux.invalid", "STRM_ROOT": "/tmp/strm"}
        with patch.object(scheduler, "validate_config", return_value=""), patch.object(
            scheduler, "_source_dirs", return_value=sources
        ), patch.object(scheduler, "_video_exts", return_value={"mkv"}), patch.object(
            scheduler, "_metadata_exts", return_value=set()
        ), patch.object(scheduler, "_refresh_media_servers", return_value={}), patch.object(
            scheduler, "_notify_success"
        ), patch.object(scheduler, "_notify_details"), patch(
            "app.modules.scheduler.sync_strm", side_effect=[dict(empty_stats), dict(empty_stats)]
        ), patch(
            "app.modules.scheduler.get", side_effect=lambda key, default="": values.get(key, default)
        ), patch("app.modules.scheduler.get_int", return_value=0):
            result = scheduler.run_blocking("manual")

        self.assertEqual(result["base_url"], "http://mediaflux.invalid")
        self.assertEqual(
            [(row["id"], row["status"], row["completed"], row["total"]) for row in result["source_runtime"]],
            [("a", "completed", 1, 1), ("b", "completed", 1, 1)],
        )


class StrmFailureUiJavascriptTests(unittest.TestCase):
    _DOM_STUB = r"""
class StubNode {
  constructor(id='') {
    this.id=id; this.value=''; this.textContent=''; this.disabled=false; this.hidden=false;
    this.checked=false; this.dataset={}; this.style={}; this.children=[]; this.listeners={};
    this.className=''; this.attributes={}; this._innerHTML=''; this.input=null;
    this.classList={toggle:()=>{},add:()=>{},remove:()=>{}};
  }
  set innerHTML(value) {
    this._innerHTML=String(value);
    this.textContent=String(value);
    if (this._innerHTML.includes('<input')) {
      this.input=new StubNode('checkbox');
      const match=this._innerHTML.match(/value="(\d+)"/);
      this.input.value=match?match[1]:'';
      this.input.checked=this._innerHTML.includes(' checked');
    }
  }
  get innerHTML(){return this._innerHTML;}
  addEventListener(type, fn){this.listeners[type]=fn;}
  fire(type){if(this.listeners[type])return this.listeners[type]({currentTarget:this,target:this});}
  setAttribute(name,value){this.attributes[name]=String(value);}
  getAttribute(name){return this.attributes[name];}
  replaceChildren(...nodes){this.children=[...nodes];}
  append(node){this.children.push(node);}
  querySelector(selector){
    if(selector==='input')return this.input;
    if(selector==='span')return new StubNode('span');
    return new StubNode(selector);
  }
  querySelectorAll(selector){
    if(selector.includes('input'))return this.children.map(node=>node.input).filter(Boolean);
    return [];
  }
}
const nodes=new Map();
const node=id=>{if(!nodes.has(id))nodes.set(id,new StubNode(id));return nodes.get(id);};
globalThis.document={
  getElementById:id=>node(id),
  createElement:tag=>new StubNode(tag),
  querySelectorAll:()=>[],
};
globalThis.window=globalThis;
globalThis.setInterval=()=>1;
globalThis.clearInterval=()=>{};
globalThis.loadAppConfig=()=>new Promise(()=>{});
globalThis.fillConfigFields=()=>{};
globalThis.saveAppConfig=async()=>{};
globalThis.openGuangYaDirectoryPicker=()=>{};
globalThis.appConfirm=async()=>true;
globalThis.__MEDIAFLUX_STRM_TEST_HOOK__=true;
"""

    @staticmethod
    def _template_script() -> str:
        return Path("app/static/js/guangya-strm.js").read_text(encoding="utf-8")

    def _run_node(self, assertions: str) -> None:
        program = self._DOM_STUB + "\n" + self._template_script() + "\n" + assertions
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
            handle.write(program)
            name = handle.name
        try:
            result = subprocess.run(
                ["node", name], text=True, capture_output=True, timeout=10, check=False
            )
        finally:
            Path(name).unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_retry_failure_reenables_real_rendered_checkbox(self):
        self._run_node(textwrap.dedent(r"""
            const openPayload={items:[{id:7,source_id:'source',source_name:'来源',filename:'Movie.mkv',action:'generate',target_rel_path:'光鸭云盘/Movie.mkv.strm',error:'failed',status:'open'}],summary:{open:1,resolved:0,sources:[{id:'source',name:'来源',open:1}]}};
            globalThis.fetch=async url=>{
              if(String(url).includes('/retry'))return {ok:true,json:async()=>({resolved:0,failed:1,remaining:1})};
              return {ok:true,json:async()=>openPayload};
            };
            (async()=>{
              const api=globalThis.__MEDIAFLUX_STRM_TEST_API__;
              if(!api)throw new Error('missing UI test API');
              api.renderFailures(openPayload);
              let checkbox=node('strmFailureList').children[0].querySelector('input');
              checkbox.checked=true; checkbox.fire('change');
              await api.retryFailures(false);
              checkbox=node('strmFailureList').children[0].querySelector('input');
              if(checkbox.disabled)throw new Error('checkbox remained disabled after retry');
            })().catch(error=>{console.error(error);process.exitCode=1;});
        """))

    def test_filter_refresh_ignores_stale_response_and_renders_latest_progress(self):
        self._run_node(textwrap.dedent(r"""
            const pending=[];
            globalThis.fetch=(url,options)=>new Promise(resolve=>pending.push({url:String(url),resolve}));
            const response=payload=>({ok:true,json:async()=>payload});
            const payload=id=>({items:[{id:id==='new'?2:1,source_id:id,source_name:id,filename:id+'.mkv',action:'generate',target_rel_path:id,error:'e',status:'open'}],summary:{open:1,resolved:0,sources:[{id,name:id,open:1}]}});
            (async()=>{
              const api=globalThis.__MEDIAFLUX_STRM_TEST_API__;
              if(!api)throw new Error('missing UI test API');
              const first=api.loadFailures();
              node('strmFailureSourceFilter').value='new';
              const second=api.loadFailures();
              if(pending.length!==2)throw new Error('filter refresh was dropped');
              pending[1].resolve(response(payload('new'))); await second;
              pending[0].resolve(response(payload('old'))); await first;
              if(api.snapshot()[0].source_id!=='new')throw new Error('stale response overwrote latest filter');
              api.renderStatus({running:true,config_error:'',enabled:true,next_run:'',last_run:{},progress:{stage:'generate',completed:5,total:10,percent:50,detail:'生成 STRM'},source_runtime:[{id:'a',name:'A',status:'completed',completed:3,total:3}]});
              if(node('strmProgressStage').textContent!=='生成 STRM')throw new Error('progress stage not rendered');
              if(!node('strmProgressCount').textContent.includes('5 / 10'))throw new Error('progress count not rendered');
              if(!node('strmSourceRuntime').textContent.includes('A'))throw new Error('source runtime not rendered');
            })().catch(error=>{console.error(error);process.exitCode=1;});
        """))
