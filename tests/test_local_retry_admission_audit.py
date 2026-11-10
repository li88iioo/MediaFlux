"""本地手动重试准入与写中断核验必须跨所有执行入口保持一致。"""

from __future__ import annotations

from starlette.responses import JSONResponse

from typing import cast
from app.modules.scraper import TMDBScraper

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from starlette.requests import Request

from app import database as db
from app.repositories import local_media as local_media_repo
from app.modules.local_media_scheduler import LocalMediaScheduler
from app.modules.local_media_service import LocalMediaService
from app.modules.local_move_transaction import LocalMoveTransaction
from app.modules.organize import OrganizeRules
from app.modules.scraper import MatchResult
from app.routes import local_media_api as api
from tests.support import isolated_test_database
from tests.test_local_media_service import FakeScraper


def request():
    return Request({"type": "http", "session": {"logged_in": True}})


class LocalRetryAdmissionAuditTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.incoming_root, self.library = self.root / "incoming", self.root / "library"
        self.incoming_root.mkdir()
        self.library.mkdir()
        self.incoming = self.incoming_root / "Movie.2026.mkv"
        self.incoming.write_bytes(b"new-media-for-regression")
        self.source_id = local_media_repo.create_local_media_source(
            name="Retry audit",
            qb_profile="",
            qb_path_prefix="",
            local_root=str(self.incoming_root),
            owner="admin",
            media_type="movie",
        )
        local_media_repo.upsert_local_library_target(
            self.source_id, "movie", str(self.library), owner="admin"
        )
        self.enterContext(
            patch("app.modules.local_media_scheduler.notify_local_media_task")
        )
        self.enterContext(
            patch(
                "app.modules.local_media_notifications.schedule_local_media_task_review"
            )
        )
        self.enterContext(patch.object(api, "notify_local_media_task"))

    def test_failed_batch_manual_scan_retries_through_the_real_scheduler(self):
        class Service:
            calls = 0

            def execute_task(self, owner, task_id, qb_client=None):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("temporary recognition failure")
                local_media_repo.update_local_media_task(task_id, owner=owner, status="completed")
                return {"status": "completed"}

        service = Service()
        scheduler = LocalMediaScheduler(service=service)
        task_id = scheduler.enqueue_manual_scan_candidates()["task_ids"][0]
        scheduler.run_once()
        previous = local_media_repo.get_local_media_task(task_id)
        assert previous is not None
        self.assertEqual(previous.status, "failed")
        with patch.object(api, "get_local_media_scheduler", return_value=scheduler):
            self.assertEqual(
                api.retry_task(task_id, request(), {}),
                {"queued": True, "task_id": task_id},
            )
        waiting = local_media_repo.get_local_media_task(task_id)
        self.assertTrue(scheduler._is_manual_scan_task(waiting))
        assert previous is not None
        assert waiting is not None
        self.assertNotEqual(waiting.operation_token, previous.operation_token)
        scheduler.run_once()
        self.assertEqual(service.calls, 2)
        get_local_media_task_result = local_media_repo.get_local_media_task(task_id)
        assert get_local_media_task_result is not None
        self.assertEqual(get_local_media_task_result.status, "completed")

    def test_all_retry_entries_keep_supported_prefix_and_do_not_enable_legacy_scan(
        self,
    ):
        for prefix in ("manual-scan:", "silent-manual-scan:", ""):
            for entry in ("retry", "manual_execute"):
                with self.subTest(prefix=prefix, entry=entry):
                    path = self.incoming_root / f"{prefix.replace(':', '')}-{entry}.mkv"
                    old_token = prefix + "old-attempt"
                    task_id = local_media_repo.create_local_media_task(
                        self.source_id,
                        "",
                        str(path),
                        owner="admin",
                        trigger="scan",
                        operation_token=old_token,
                    )
                    local_media_repo.update_local_media_task(task_id, status="failed")
                    if entry == "retry":
                        self.assertTrue(
                            local_media_repo.reset_local_media_task(task_id, owner="admin")
                        )
                    else:
                        self.assertEqual(
                            local_media_repo.prepare_manual_local_media_task(
                                self.source_id, str(path), snapshot_digest="preview:fixture", owner="admin"
                            ),
                            task_id,
                        )
                    after = local_media_repo.get_local_media_task(task_id)
                    assert after is not None
                    self.assertNotEqual(after.operation_token, old_token)
                    self.assertEqual(after.trigger, "scan")
                    self.assertEqual(
                        LocalMediaScheduler._is_manual_scan_task(after), bool(prefix)
                    )
                    self.assertEqual(
                        LocalMediaScheduler._is_silent_task(after),
                        prefix == "silent-manual-scan:",
                    )
                    local_media_repo.update_local_media_task(task_id, status="completed")

    def test_failed_and_requires_manual_interruption_markers_block_plain_prepare(self):
        for status in ("failed", "requires_manual"):
            with self.subTest(status=status):
                path = self.incoming_root / f"{status}.mkv"
                task_id = local_media_repo.create_local_media_task(
                    self.source_id,
                    "",
                    str(path),
                    owner="admin",
                    trigger="scan",
                    operation_token="manual-scan:interrupted",
                )
                local_media_repo.update_local_media_task(task_id, status="moving")
                db.init_db()
                local_media_repo.update_local_media_task(task_id, status=status)
                before = local_media_repo.get_local_media_task(task_id)
                assert before is not None
                self.assertTrue(db.is_interrupted_local_media_write_error(before.error))
                with self.assertRaisesRegex(ValueError, "核验"):
                    local_media_repo.prepare_manual_local_media_task(
                        self.source_id, str(path), snapshot_digest="preview:fixture", owner="admin"
                    )
                self.assertEqual(local_media_repo.get_local_media_task(task_id), before)
                self.assertTrue(
                    local_media_repo.reset_local_media_task(
                        task_id, owner="admin", confirm_interrupted_write=True
                    )
                )
                after = local_media_repo.get_local_media_task(task_id)
                self.assertTrue(LocalMediaScheduler._is_manual_scan_task(after))
                self.assertFalse(
                    local_media_repo.reset_local_media_task(
                        task_id, owner="admin", confirm_interrupted_write=True
                    )
                )
                local_media_repo.update_local_media_task(task_id, status="completed")

    def test_inspect_execute_cannot_erase_unconfirmed_retired_target_attempt(self):
        service = LocalMediaService(
            scraper=cast(TMDBScraper, FakeScraper(
                MatchResult(
                    tmdb_id="1",
                    title="Movie",
                    year="2026",
                    media_type="movie",
                    confidence=1.0,
                )
            ))
        )
        self.addCleanup(service.close)
        rules = OrganizeRules(
            region_split=False,
            year_split=False,
            naming_scope="both",
            conflict_strategy=2,
            emby_refresh=False,
            media_probe_enabled=False,
            clean_empty=False,
        )
        self.enterContext(
            patch(
                "app.modules.local_media_service.OrganizeRules.from_config",
                return_value=rules,
            )
        )
        self.enterContext(
            patch(
                "app.modules.local_media_service.probe_local_media_profile",
                return_value=None,
            )
        )
        inspection = service.inspect_source("admin", self.source_id, self.incoming)
        preview = service.preview(
            "admin", inspection["inspection_id"], tmdb_id="1", media_type="movie"
        )
        target = Path(preview["plans"][0]["target_path"])
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old-library-media")
        preview = service.preview(
            "admin", inspection["inspection_id"], tmdb_id="1", media_type="movie"
        )
        task_id = service.create_manual_task(
            "admin", inspection["inspection_id"],
            preview_digest=preview["preview_digest"],
        )
        self.assertTrue(local_media_repo.claim_local_media_task(task_id, owner="admin"))
        original_backup = LocalMoveTransaction._backup_replaced_target

        def crash_after_backup(transaction, *args, **kwargs):
            original_backup(transaction, *args, **kwargs)
            raise SystemExit("process loss after old-target retirement")

        with patch.object(
            LocalMoveTransaction, "_backup_replaced_target", crash_after_backup
        ):
            with self.assertRaises(SystemExit):
                service.execute_task("admin", task_id)
        backups = list(target.parent.glob(".*.mediaflux-replaced-*"))
        self.assertEqual(len(backups), 1)
        db.init_db()
        recovered = local_media_repo.get_local_media_task(task_id, owner="admin")
        before_steps = local_media_repo.list_local_media_operation_steps(task_id, owner="admin")
        retry_response = api.retry_task(task_id, request(), {})
        assert isinstance(retry_response, JSONResponse)
        self.assertEqual(retry_response.status_code, 409)
        with patch.object(api, "get_local_media_service", return_value=service):
            fresh = api.inspect_task(task_id, request())
            assert isinstance(fresh, dict)
            result = api.execute_media(
                request(),
                {
                    "inspection_id": fresh["inspection_id"],
                    "tmdb_id": "1",
                    "media_type": "movie",
                    "rules_snapshot": preview["rules_snapshot"],
                },
            )
        self.assertNotIsInstance(result, dict, result)
        self.assertIn(result.status_code, (400, 409))
        self.assertEqual(local_media_repo.get_local_media_task(task_id), recovered)
        self.assertEqual(local_media_repo.list_local_media_operation_steps(task_id), before_steps)
        self.assertTrue(self.incoming.exists())
        self.assertFalse(target.exists())
        self.assertEqual(backups[0].read_bytes(), b"old-library-media")
