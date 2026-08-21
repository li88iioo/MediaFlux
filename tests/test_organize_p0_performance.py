from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.organize import OrganizeRules, Organizer, _OrganizeAuditWriteError
from app.modules.scraper import TMDBScraper
from app.modules.strm import _current_fingerprint_backfills, sync_strm
from tests.support import IsolatedDatabaseTestCase


class _SearchClient:
    api_key = "test-key"
    base_url = "https://api.example"
    config_error = ""
    session = None

    def __init__(self):
        self.search = Mock(return_value=[{
            "id": 100,
            "name": "Example Show",
            "first_air_date": "2026-01-01",
        }])


class _DetailClient:
    api_key = "test-key"
    base_url = "https://api.example"
    config_error = ""
    session = None

    def __init__(self):
        self.detail = Mock(return_value={
            "id": 100,
            "name": "Example Show",
            "first_air_date": "2026-01-01",
        })


class _EmptyOrganizerClient:
    @staticmethod
    def file_info(_file_id):
        return GuangYaFile("source", "Source", True)

    @staticmethod
    def list_dir(_file_id):
        return []


class _MetricScraper:
    def __init__(self):
        self.metrics = {
            "tmdb_search_requests": 3,
            "tmdb_search_cache_hits": 7,
            "tmdb_detail_requests": 2,
            "tmdb_detail_cache_hits": 4,
            "ai_requests": 1,
        }

    def performance_snapshot(self):
        return dict(self.metrics)


class _TreeClient:
    def __init__(self, source_id: str, files: list[GuangYaFile]):
        self.source_id = source_id
        self.files = files

    def list_dir(self, file_id: str):
        if file_id == self.source_id:
            return list(self.files)
        return []


class OrganizeP0PerformanceTests(IsolatedDatabaseTestCase):
    def test_tmdb_search_cache_reuses_equivalent_query_within_ttl(self):
        client = _SearchClient()
        scraper = TMDBScraper(client=client)

        first = scraper.search(" Example   Show ", "2026", "tv")
        second = scraper.search("example show", "2026", "tv")

        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertEqual(client.search.call_count, 1)
        metrics = scraper.performance_snapshot()
        self.assertEqual(metrics["tmdb_search_requests"], 1)
        self.assertEqual(metrics["tmdb_search_cache_hits"], 1)

    def test_tmdb_detail_cache_reuses_successful_result_without_shared_mutation(self):
        client = _DetailClient()
        scraper = TMDBScraper(client=client)

        first = scraper.get_detail("100", "tv")
        second = scraper.get_detail("100", "tv")

        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first["name"] = "mutated"
        self.assertEqual(scraper.get_detail("100", "tv")["name"], "Example Show")
        self.assertEqual(client.detail.call_count, 1)
        metrics = scraper.performance_snapshot()
        self.assertEqual(metrics["tmdb_detail_requests"], 1)
        self.assertEqual(metrics["tmdb_detail_cache_hits"], 2)

    def test_organize_exposes_stable_stage_timings_and_metric_deltas(self):
        scraper = _MetricScraper()
        organizer = Organizer(client=_EmptyOrganizerClient(), scraper=scraper)

        _plans, stats = organizer.organize(
            "source",
            rules=OrganizeRules(
                video_exts="", metadata_exts="", small_file_mb=0,
                clean_empty=False, link_strm=False,
            ),
        )

        for key in (
            "scan_elapsed_seconds", "recognition_elapsed_seconds",
            "conflict_check_elapsed_seconds", "total_elapsed_seconds",
        ):
            self.assertIn(key, stats)
            self.assertGreaterEqual(stats[key], 0)
        self.assertEqual(stats["tmdb_search_requests"], 0)
        self.assertEqual(stats["tmdb_search_cache_hits"], 0)
        self.assertEqual(stats["ai_requests"], 0)

    def test_strm_generation_loads_source_index_once_for_multiple_writes(self):
        source_id = "p0-source"
        files = [
            GuangYaFile("video-1", "Example.S01E01.mkv", False, 100, "etag-1", source_id),
            GuangYaFile("video-2", "Example.S01E02.mkv", False, 200, "etag-2", source_id),
        ]
        client = _TreeClient(source_id, files)
        original = db.list_strm_index
        source_key = f"guangya:{source_id}"
        calls: list[str] = []

        def counted(source: str = "guangya"):
            calls.append(source)
            return original(source)

        with tempfile.TemporaryDirectory() as root, patch.object(
            db, "list_strm_index", side_effect=counted
        ):
            stats = sync_strm(
                source_id, "http://example", root,
                client=client, clean_invalid=False,
            )

        self.assertEqual(stats["generated"], 2)
        self.assertEqual(calls.count(source_key), 1)

    def test_audit_bundle_uses_one_connection_and_marks_partial_detail_failure(self):
        args = ("guangya", "source/file.mkv", "target/file.mkv", "file-1", "success", "1")
        kwargs = {
            "original_parent_id": "source",
            "original_name": "file.mkv",
            "current_parent_id": "target",
            "current_name": "file.mkv",
            "legacy_incomplete": False,
        }
        items = [{"file_id": "file-1", "role": "video", "status": "success"}]
        original_get_conn = db.get_conn
        with patch.object(db, "get_conn", wraps=original_get_conn) as get_conn:
            log_id = Organizer._write_organize_audit(args, kwargs, items)
        self.assertEqual(get_conn.call_count, 1)
        self.assertEqual(len(db.list_organize_log_items(log_id)), 1)

        with patch(
            "app.modules.organize.add_organize_log_items",
            side_effect=RuntimeError("detail write failed"),
        ):
            with self.assertRaises(_OrganizeAuditWriteError) as raised:
                Organizer._write_organize_audit(
                    ("guangya", "source/other.mkv", "target/other.mkv", "file-2", "success", "2"),
                    {**kwargs, "original_name": "other.mkv", "current_name": "other.mkv"},
                    [{"file_id": "file-2", "role": "video", "status": "success"}],
                )
        row = db.get_organize_log(raised.exception.log_id)
        self.assertIsNotNone(row)
        self.assertEqual(int(row["legacy_incomplete"]), 1)
        self.assertIn("明细写入失败", str(row["error"]))


if __name__ == "__main__":
    unittest.main()


class StrmBatchPerformanceTests(IsolatedDatabaseTestCase):
    def test_conflicting_index_replacement_uses_one_database_transaction(self):
        source_key = "guangya:transaction-source"
        db.upsert_strm_index(
            source_key, "old", "old-etag", 1, "Movie.mkv", "/tmp/old.strm"
        )
        original_get_conn = db.get_conn

        with patch.object(db, "get_conn", wraps=original_get_conn) as get_conn:
            db.upsert_strm_index(
                source_key, "new", "new-etag", 2, "Movie.mkv", "/tmp/new.strm",
                conflicting_file_ids=("old",),
            )

        self.assertEqual(get_conn.call_count, 1)
        rows = db.list_strm_index(source_key)
        self.assertEqual([row["file_id"] for row in rows], ["new"])

    def test_full_sync_resolves_failure_ledger_in_one_batch(self):
        source_id = "batch-resolve-source"
        files = [
            GuangYaFile("video-1", "Example.S01E01.mkv", False, 100, "etag-1", source_id),
            GuangYaFile("video-2", "Example.S01E02.mkv", False, 200, "etag-2", source_id),
        ]
        for file in files:
            db.record_strm_failure(
                source_id=source_id,
                source_name="批量测试",
                file_id=file.file_id,
                parent_id=source_id,
                filename=file.name,
                action="generate",
                rel_dir="",
                target_rel_path=f"{file.name}.strm",
                error="旧失败",
            )
        client = _TreeClient(source_id, files)
        original_batch = db.resolve_strm_failures_for_items

        with tempfile.TemporaryDirectory() as root, patch.object(
            db, "resolve_strm_failures_for_items", wraps=original_batch
        ) as batch_resolve, patch.object(
            db, "resolve_strm_failure_for_item",
            side_effect=AssertionError("不得逐文件关闭失败台账"),
        ):
            stats = sync_strm(
                source_id, "http://example", root,
                client=client, clean_invalid=False,
            )

        self.assertEqual(stats["generated"], 2)
        self.assertEqual(stats["failure_resolve_batches"], 1)
        self.assertEqual(stats["failure_ledger_failed"], 0)
        self.assertGreaterEqual(stats["failure_resolve_elapsed_seconds"], 0)
        batch_resolve.assert_called_once()
        self.assertEqual(
            set(batch_resolve.call_args.args[1]), {"video-1", "video-2"}
        )
        self.assertEqual(db.summarize_strm_failures()["open"], 0)

    def test_full_sync_prefilters_changed_index_before_local_read(self):
        source_id = "verify-prefilter-source"
        original = GuangYaFile(
            "video", "Movie.mkv", False, 100, "etag-1", source_id
        )
        changed = GuangYaFile(
            "video", "Movie.mkv", False, 200, "etag-2", source_id
        )
        with tempfile.TemporaryDirectory() as root:
            sync_strm(
                source_id, "http://example", root,
                client=_TreeClient(source_id, [original]), clean_invalid=False,
            )
            with patch(
                "app.modules.strm._read_strm_state",
                side_effect=AssertionError("索引已变化时不应预读旧 STRM"),
            ):
                stats = sync_strm(
                    source_id, "http://example", root,
                    client=_TreeClient(source_id, [changed]),
                    clean_invalid=False,
                )

        self.assertEqual(stats["generated"], 1)
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["verified_candidates"], 0)
        self.assertEqual(stats["verify_prefiltered"], 1)

    def test_full_sync_verifies_stable_strm_with_bounded_concurrency(self):
        source_id = "verify-concurrency-source"
        files = [
            GuangYaFile(
                f"video-{index}", f"Episode-{index}.mkv", False,
                100 + index, f"etag-{index}", source_id,
            )
            for index in range(8)
        ]
        active = 0
        peak = 0
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as root:
            first = sync_strm(
                source_id, "http://example", root,
                client=_TreeClient(source_id, files), clean_invalid=False,
            )
            from app.modules import strm as strm_module
            original_read = strm_module._read_strm_state

            def tracked_read(path, expected_url):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    time.sleep(0.02)
                    return original_read(path, expected_url)
                finally:
                    with lock:
                        active -= 1

            with patch(
                "app.modules.strm._read_strm_state", side_effect=tracked_read
            ) as read_state:
                second = sync_strm(
                    source_id, "http://example", root,
                    client=_TreeClient(source_id, files), clean_invalid=False,
                    verify_workers=4,
                )

        self.assertEqual(first["generated"], len(files))
        self.assertEqual(second["generated"], 0)
        self.assertEqual(second["skipped"], len(files))
        self.assertEqual(second["verify_workers_configured"], 4)
        self.assertEqual(second["verified_candidates"], len(files))
        self.assertEqual(second["verify_prefiltered"], 0)
        self.assertEqual(read_state.call_count, len(files))
        self.assertGreaterEqual(peak, 2)
        self.assertLessEqual(peak, 4)

    def test_stale_fingerprint_backfill_does_not_overwrite_later_index(self):
        source_id = "verify-stale-backfill-source"
        source_key = f"guangya:{source_id}"
        first = GuangYaFile(
            "video", "A.mkv", False, 100, "etag", source_id
        )
        second = GuangYaFile(
            "video", "B.mkv", False, 100, "etag", source_id
        )
        with tempfile.TemporaryDirectory() as root:
            first_path = Path(root) / "first.strm"
            first_path.write_text("managed", encoding="utf-8")
            db.upsert_strm_index(
                source_key, first.file_id, first.etag, first.size,
                first.name, str(first_path), "",
            )
            stale_backfill = {
                "file_id": first.file_id,
                "etag": first.etag,
                "size": first.size,
                "filename": first.name,
                "strm_path": str(first_path),
                "content_fingerprint": "sha256:stale",
            }
            current_snapshot = {
                "file_id": second.file_id,
                "etag": second.etag,
                "size": second.size,
                "filename": second.name,
                "strm_path": str(Path(root) / "second.strm"),
                "content_fingerprint": "sha256:new",
            }
            existing_by_id = {second.file_id: current_snapshot}
            current_backfills = _current_fingerprint_backfills(
                [stale_backfill], existing_by_id
            )

        self.assertEqual(current_backfills, [])

    def test_strm_stage_timings_are_exposed(self):
        source_id = "stage-timing-source"
        client = _TreeClient(source_id, [
            GuangYaFile("video", "Movie.mkv", False, 100, "etag", source_id)
        ])

        with tempfile.TemporaryDirectory() as root:
            stats = sync_strm(
                source_id, "http://example", root,
                client=client, clean_invalid=True,
            )

        for key in (
            "scan_elapsed_seconds",
            "generate_elapsed_seconds",
            "metadata_elapsed_seconds",
            "cleanup_elapsed_seconds",
            "failure_resolve_elapsed_seconds",
        ):
            self.assertIn(key, stats)
            self.assertGreaterEqual(stats[key], 0)
        self.assertEqual(stats["verify_workers_configured"], 8)
        self.assertEqual(stats["verified_candidates"], 0)
        self.assertEqual(stats["verify_prefiltered"], 1)
