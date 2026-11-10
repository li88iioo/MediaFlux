"""准入发布必须按事务顺序收敛，启动恢复不得逐请求重复查询。"""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import media_subscriptions as repository
from tests.support import isolated_test_database


class MediaAdmissionTransactionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.path = self.enterContext(isolated_test_database("mediaflux.db"))
        self.subscription = repository.add_media_subscription(
            provider="tmdb",
            external_id="1",
            tmdb_id="1",
            media_type="tv",
            title="事务边界",
            monitor_mode="missing",
            action="confirm",
            download_target="qb",
            check_interval_minutes=60,
        )

    def seed(
        self,
        episode=1,
        *,
        status="manual_review",
        error="旧的核验原因",
        existing_request=None,
    ):
        key = f"tmdb:1:tv:S01E{episode:03d}"
        candidate = repository.replace_media_subscription_candidates(
            self.subscription,
            key,
            season=1,
            episode=episode,
            candidates=[{"result_id": f"candidate-{episode}", "title": key}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        admission = repository.claim_media_download_admission(
            media_key=key,
            tmdb_id="1",
            media_type="tv",
            subscription_id=self.subscription,
            candidate_id=candidate,
            season=1,
            episode=episode,
            subscription_revision=1,
        )
        assert admission is not None
        self.assertTrue(
            repository.begin_media_download_dispatch(
                admission, subscription_id=self.subscription, subscription_revision=1
            )
        )
        if existing_request is None:
            request, _ = download_requests_repo.create_download_request(
                f"transaction-{episode}", "magnet", admission_id=admission
            )
        else:
            request = existing_request
            assert admission is not None
            self.assertTrue(
                download_requests_repo.bind_media_download_admission_request(admission, request)
            )
        download_requests_repo.update_download_request(request, status=status, error=error)
        return request, admission

    @staticmethod
    def admission(admission):
        with db.get_conn() as conn:
            return dict(
                conn.execute(
                    "SELECT * FROM media_download_admissions WHERE id=?", (admission,)
                ).fetchone()
            )

    def test_stale_immediate_or_batch_projection_cannot_overwrite_newly_committed_reason(
        self,
    ):
        for mode in ("immediate", "batch"):
            with self.subTest(mode=mode):
                request, admission = self.seed(1 if mode == "immediate" else 2)
                repository.sync_media_download_admission_for_request(request)
                read_old, newer_started, newer_done = (
                    threading.Event(),
                    threading.Event(),
                    threading.Event(),
                )
                tls = threading.local()
                actual_conn = db.get_conn
                actual_projection = repository._download_request_admission_projection
                old_reason = "旧的核验原因"
                new_reason = "下载后端提交结果未知，请先核对下载器，勿直接重复提交"

                @contextmanager
                def tracked_conn():
                    with actual_conn() as conn:
                        tls.conn = conn
                        yield conn

                def projection(row, stamp):
                    if row[
                        "error"
                    ] == old_reason and threading.current_thread().name.startswith(
                        "old-projection"
                    ):
                        read_old.set()
                        if not newer_started.wait(5):
                            raise RuntimeError("newer writer not started")
                        # 未持有事务时，确定性地让新写入先提交；有事务则让真实
                        # SQLite writer 等待旧发布完成。两种情况下最终都必须是新事实。
                        if not tls.conn.in_transaction and not newer_done.wait(5):
                            raise RuntimeError(
                                "unprotected newer writer did not commit"
                            )
                    return actual_projection(row, stamp)

                def old_writer():
                    if mode == "immediate":
                        return repository.sync_media_download_admission_for_request(request)
                    return repository.reconcile_media_download_admissions(
                        self.subscription, set(), expected_revision=1
                    )

                def new_writer():
                    if not read_old.wait(5):
                        raise RuntimeError("old snapshot not reached")
                    newer_started.set()
                    try:
                        return download_requests_repo.update_download_request_and_sync_media_admission(
                            request, status="manual_review", error=new_reason
                        )
                    finally:
                        newer_done.set()

                with (
                    patch.object(db, "get_conn", tracked_conn),
                    patch.object(
                        repository, "_download_request_admission_projection", projection
                    ),
                ):
                    with (
                        ThreadPoolExecutor(
                            max_workers=1, thread_name_prefix="old-projection"
                        ) as old_pool,
                        ThreadPoolExecutor(
                            max_workers=1, thread_name_prefix="new-fact"
                        ) as new_pool,
                    ):
                        older = old_pool.submit(old_writer)
                        newer = new_pool.submit(new_writer)
                        self.assertGreaterEqual(older.result(timeout=10), 1)
                        self.assertEqual(newer.result(timeout=10), 1)
                get_download_request_result = download_requests_repo.get_download_request(request)
                assert get_download_request_result is not None
                self.assertEqual(get_download_request_result["error"], new_reason)
                result = self.admission(admission)
                self.assertEqual(result["status"], "processing")
                self.assertEqual(result["error"], new_reason)
                self.assertIsNone(result["completed_at"])

    def test_startup_uses_one_joined_read_for_many_bound_requests(self):
        entries = [
            self.seed(i, status="failed" if i % 3 == 0 else "manual_review")
            for i in range(1, 61)
        ]
        statements = []
        actual = db.get_conn

        @contextmanager
        def tracked():
            with actual() as conn:
                conn.set_trace_callback(statements.append)
                try:
                    yield conn
                finally:
                    conn.set_trace_callback(None)

        with patch.object(db, "get_conn", tracked):
            projected, released = repository.reconcile_startup_media_download_admissions()
        self.assertEqual((projected, released), (60, 0))
        reads = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        self.assertEqual(
            len(reads), 1, f"Startup issued {len(reads)} reads for 60 requests"
        )
        for index, (_, admission) in enumerate(entries, 1):
            result = self.admission(admission)
            self.assertEqual(
                result["status"], "failed" if index % 3 == 0 else "processing"
            )
            self.assertEqual(result["completed_at"] is not None, index % 3 == 0)

    def test_shared_request_projects_each_admission_once_and_preserves_terminal_rows(
        self,
    ):
        request, first = self.seed(1)
        _, second = self.seed(2, existing_request=request)
        _, terminal = self.seed(3, existing_request=request)
        assert terminal is not None
        repository.update_media_download_admission(
            terminal, status="cancelled", error="用户已取消"
        )
        before = self.admission(terminal)
        self.assertEqual(repository.sync_media_download_admission_for_request(request), 2)
        self.assertEqual(repository.reconcile_startup_media_download_admissions(), (2, 0))
        self.assertEqual(self.admission(first)["status"], "processing")
        self.assertEqual(self.admission(second)["status"], "processing")
        self.assertEqual(self.admission(terminal), before)

    def test_bulk_write_failure_rolls_back_every_admission_before_retry(self):
        import sqlite3

        entries = [self.seed(i, status="failed") for i in range(1, 5)]
        before = [self.admission(a) for _, a in entries]
        for mode in ("startup", "batch"):
            with self.subTest(mode=mode):
                with db.get_conn() as conn:
                    conn.execute(
                        f"CREATE TRIGGER abort_projection BEFORE UPDATE ON media_download_admissions WHEN NEW.id={entries[-1][1]} BEGIN SELECT RAISE(ABORT,'projection-write-failure'); END"
                    )
                operation = (
                    repository.reconcile_startup_media_download_admissions
                    if mode == "startup"
                    else lambda: repository.reconcile_media_download_admissions(
                        self.subscription, set(), expected_revision=1
                    )
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "projection-write-failure"
                ):
                    operation()
                self.assertEqual([self.admission(a) for _, a in entries], before)
                with db.get_conn() as conn:
                    conn.execute("DROP TRIGGER abort_projection")
        self.assertEqual(repository.reconcile_startup_media_download_admissions(), (4, 0))
        self.assertEqual(repository.reconcile_startup_media_download_admissions(), (0, 0))

    def test_primary_key_updates_avoid_repeated_queue_scans(self):
        entries = [self.seed(i) for i in range(1, 11)]
        actual = db.get_conn
        updates = []

        @contextmanager
        def tracked():
            with actual() as conn:
                conn.set_trace_callback(
                    lambda sql: (
                        updates.append(sql)
                        if sql.startswith(
                            "UPDATE media_download_admissions SET status="
                        )
                        else None
                    )
                )
                try:
                    yield conn
                finally:
                    conn.set_trace_callback(None)

        with patch.object(db, "get_conn", tracked):
            self.assertEqual(repository.reconcile_startup_media_download_admissions(), (10, 0))
        projected = [sql for sql in updates if "status='processing'" in sql]
        self.assertEqual(len(projected), len(entries))
        with db.get_conn() as conn:
            for sql in projected:
                plan = " ".join(
                    str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql)
                )
                self.assertIn("PRIMARY KEY", plan)
                self.assertNotIn("SCAN media_download_admissions", plan)

    def test_exit_mid_bulk_publish_rolls_back_and_zip_can_replay_once(self):
        import subprocess
        import sys

        from app.modules import backup
        from tests.test_ten_pass_process_restore_audit import runtime_paths

        entries = [
            self.seed(i, status="failed" if i % 2 else "manual_review")
            for i in range(1, 11)
        ]
        before = [self.admission(a) for _, a in entries]
        script = r"""
import tests
import os, sys, socket
from contextlib import contextmanager
from unittest.mock import patch
from app import database as db
from app.repositories import media_subscriptions as repository
socket.socket.connect = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network forbidden"))
db.configure_database(sys.argv[1], test_mode=True)
actual = db.get_conn
count = 0
def interrupt(sql):
 global count
 if sql.startswith("UPDATE media_download_admissions SET status="):
  count += 1
  if count == 2: os._exit(47)
@contextmanager
def tracked():
 with actual() as conn:
  conn.set_trace_callback(interrupt)
  yield conn
with patch.object(db, "get_conn", tracked):
 repository.reconcile_startup_media_download_admissions()
raise AssertionError("bulk publication window not reached")
"""
        child = subprocess.run(
            [sys.executable, "-c", script, str(self.path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(child.returncode, 47, child.stderr)
        self.assertEqual([self.admission(a) for _, a in entries], before)
        paths = runtime_paths(self.path)
        archive = backup.create_backup(paths, reason="uncommitted-projection")
        backup.verify_backup(archive)
        db.init_db()
        self.assertEqual(repository.reconcile_startup_media_download_admissions(), (10, 0))
        expected = [
            (
                self.admission(a)["status"],
                self.admission(a)["error"],
                self.admission(a)["completed_at"] is not None,
            )
            for _, a in entries
        ]
        self.assertEqual(
            len(repository.list_active_media_download_admissions(self.subscription)), 5
        )
        backup.restore_backup(paths, archive)
        db.init_db()
        self.assertEqual(repository.reconcile_startup_media_download_admissions(), (10, 0))
        restored = [
            (
                self.admission(a)["status"],
                self.admission(a)["error"],
                self.admission(a)["completed_at"] is not None,
            )
            for _, a in entries
        ]
        self.assertEqual(restored, expected)
        self.assertEqual(repository.reconcile_startup_media_download_admissions(), (5, 0))
