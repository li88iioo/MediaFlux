"""元数据积压冻结/取消边界，不连接云盘，不删除真实文件。"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.modules import strm_metadata_management as service
from app.repositories import strm as repository
from tests.support import IsolatedDatabaseTestCase
from tests.test_strm_metadata_queue import _job


class MetadataManagementTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_metadata_queue")
        self.secret = patch(
            "app.modules.web_secret.get_web_secret",
            return_value="isolated-management-secret",
        )
        self.secret.start()
        self.addCleanup(self.secret.stop)
        self.disabled = patch("app.config.get_bool", return_value=False)
        self.disabled.start()
        self.addCleanup(self.disabled.stop)

    def _enqueue(self, name="a"):
        repository.enqueue_strm_metadata_jobs([_job(file_id=name)])

    def _status(self):
        with db.get_conn() as conn:
            return {
                row["file_id"]: row["status"]
                for row in conn.execute(
                    "SELECT file_id,status FROM strm_metadata_queue"
                )
            }

    def test_disabled_queue_cancel_does_not_start_worker_or_delete_files(self):
        self._enqueue()
        with tempfile.TemporaryDirectory() as root:
            existing = Path(root) / "movie.nfo"
            existing.write_text("existing Jellyfin metadata")
            view, token = service.prepare_backlog_cancel("web:owner")
            self.assertFalse(view["enabled"])
            self.assertEqual(self._status()["a"], "queued")
            with (
                patch(
                    "app.modules.strm_metadata_worker.get_strm_metadata_worker"
                ) as worker,
                patch("app.config.update_runtime_env_file") as update,
            ):
                result = service.cancel_backlog_confirmed(token, "web:owner")
            self.assertEqual(result["cancelled"], 1)
            self.assertEqual(result["files_deleted"], 0)
            self.assertFalse(result["enabled"])
            self.assertEqual(existing.read_text(), "existing Jellyfin metadata")
            worker.assert_not_called()
            update.assert_not_called()
        self.assertEqual(self._status()["a"], "cancelled")
        with self.assertRaises(ValueError):
            service.cancel_backlog_confirmed(token, "web:owner")

    def test_new_jobs_after_preview_remain_queued(self):
        self._enqueue("old")
        _, token = service.prepare_backlog_cancel("owner")
        self._enqueue("new")
        result = service.cancel_backlog_confirmed(token, "owner")
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(self._status(), {"old": "cancelled", "new": "queued"})

    def test_running_completed_failed_not_in_scope(self):
        for name in ("pending", "retry", "working", "done", "failure"):
            self._enqueue(name)
        with db.get_conn() as conn:
            for name, state in (
                ("retry", "retry_wait"),
                ("working", "running"),
                ("done", "completed"),
                ("failure", "failed"),
            ):
                conn.execute(
                    "UPDATE strm_metadata_queue SET status=? WHERE file_id=?",
                    (state, name),
                )
        view, token = service.prepare_backlog_cancel("owner")
        self.assertEqual(view["count"], 2)
        service.cancel_backlog_confirmed(token, "owner")
        self.assertEqual(
            self._status(),
            {
                "pending": "cancelled",
                "retry": "cancelled",
                "working": "running",
                "done": "completed",
                "failure": "failed",
            },
        )

    def test_changed_revision_is_stale_and_no_partial_cancel(self):
        self._enqueue("a")
        self._enqueue("b")
        _, token = service.prepare_backlog_cancel("owner")
        repository.enqueue_strm_metadata_jobs([_job(file_id="b", etag="new-etag")])
        with self.assertRaisesRegex(ValueError, "队列已变化"):
            service.cancel_backlog_confirmed(token, "owner")
        self.assertEqual(set(self._status().values()), {"queued"})

    def test_claim_after_preview_rejects_without_touching_running(self):
        self._enqueue()
        _, token = service.prepare_backlog_cancel("owner")
        repository.claim_due_strm_metadata_jobs(owner="worker")
        with self.assertRaises(ValueError):
            service.cancel_backlog_confirmed(token, "owner")
        self.assertEqual(self._status()["a"], "running")

    def test_queued_running_queued_aba_changes_lease_generation(self):
        self._enqueue()
        _, token = service.prepare_backlog_cancel("owner")
        repository.claim_due_strm_metadata_jobs(owner="worker")
        with db.get_conn() as conn:
            conn.execute("UPDATE strm_metadata_queue SET status='queued'")
        with self.assertRaises(ValueError):
            service.cancel_backlog_confirmed(token, "owner")

    def test_old_completed_requeued_after_preview_not_silently_cancelled(self):
        self._enqueue("old-complete")
        with db.get_conn() as conn:
            conn.execute("UPDATE strm_metadata_queue SET status='completed'")
        self._enqueue("pending")
        _, token = service.prepare_backlog_cancel("owner")
        repository.enqueue_strm_metadata_jobs([_job(file_id="old-complete", etag="new")])
        with self.assertRaises(ValueError):
            service.cancel_backlog_confirmed(token, "owner")
        self.assertEqual(set(self._status().values()), {"queued"})

    def test_signature_owner_expiry_and_body_tampering_fail(self):
        self._enqueue()
        _, token = service.prepare_backlog_cancel("owner")
        for bad_token, owner in (
            (token, "another"),
            (token[:-1] + ("1" if token[-1] != "1" else "2"), "owner"),
            ("invalid", "owner"),
        ):
            with (
                self.subTest(owner=owner, token=bad_token[:10]),
                self.assertRaises(ValueError),
            ):
                service.cancel_backlog_confirmed(bad_token, owner)
        with (
            patch(
                "app.modules.strm_metadata_management.time.time", return_value=10**12
            ),
            self.assertRaises(ValueError),
        ):
            service.cancel_backlog_confirmed(token, "owner")
        self.assertEqual(self._status()["a"], "queued")

    def test_large_backlog_has_bounded_confirmation_payload(self):
        repository.enqueue_strm_metadata_jobs([_job(file_id=str(i)) for i in range(13581)])
        view, token = service.prepare_backlog_cancel("owner")
        self.assertEqual(view["count"], 13581)
        self.assertLess(len(token), 1024)
        self.assertLess(len(json.dumps(view)), 3000)
        self.assertEqual(
            service.cancel_backlog_confirmed(token, "owner")["cancelled"], 13581
        )

    def test_concurrent_confirmation_only_one_cancels(self):
        self._enqueue()
        snapshot = repository.capture_strm_metadata_backlog()
        barrier = threading.Barrier(2)
        results: list[int | str] = []

        def run():
            barrier.wait()
            try:
                results.append(repository.cancel_strm_metadata_backlog(snapshot))
            except ValueError:
                results.append("stale")

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, [1, "stale"])


    def test_empty_queue_does_not_generate_confirmation(self):
        with self.assertRaisesRegex(ValueError, "没有可取消"):
            service.prepare_backlog_cancel("owner")

    def test_zero_running_is_not_missing_worker_or_paused_when_enabled(self):
        raw = {
            "queued": 3,
            "running": 0,
            "enabled": True,
            "worker_running": True,
            "consumer_active": True,
        }
        with patch(
            "app.modules.strm_metadata_worker.get_strm_metadata_worker"
        ) as worker:
            worker.return_value.status.return_value = raw
            status = service.metadata_status()
        self.assertEqual(status["state"], "waiting")
        self.assertTrue(status["worker_running"])
        self.assertTrue(status["consumer_active"])
        self.assertIn("查询瞬间", status["semantics"])
        self.assertIn("旧配置继续入队", status["semantics"])
        self.assertEqual(status["worker_scope"], "current_process")
        self.assertIn("T", status["sampled_at"])

    def test_status_distinguishes_disabled_draining_backoff_and_missing_consumer(self):
        cases = [
            ({"enabled": False, "running": 0}, "paused"),
            ({"enabled": False, "running": 1}, "draining"),
            ({"enabled": True, "breaker_seconds": 12}, "backoff"),
            (
                {"enabled": True, "worker_running": True, "consumer_active": False},
                "waiting_worker",
            ),
        ]
        for raw, expected in cases:
            with (
                self.subTest(expected=expected),
                patch(
                    "app.modules.strm_metadata_worker.get_strm_metadata_worker"
                ) as worker,
            ):
                worker.return_value.status.return_value = raw
                self.assertEqual(service.metadata_status()["state"], expected)

    def test_policy_preflight_no_write_and_confirm_cas_preserves_queue(self):
        self._enqueue()
        with tempfile.TemporaryDirectory() as root:
            env = Path(root) / "user.env"
            env.write_text("STRM_METADATA_ENABLED=false\nOTHER=keep\n")
            with (
                patch("app.config.ENV_FILE", env),
                patch("app.config.has_external_override", return_value=False),
                patch("app.config.update_runtime_env_file") as update,
                patch(
                    "app.modules.strm_metadata_worker.get_strm_metadata_worker"
                ) as worker,
            ):
                view, token = service.prepare_policy(True, "owner")
                update.assert_not_called()
                self.assertTrue(view["enabled"])
                service.set_policy_confirmed(token, "owner")
                update.assert_called_once_with(
                    env, {"STRM_METADATA_ENABLED": "true"}, expected=env.read_bytes()
                )
                worker.return_value.wake.assert_called_once()
        self.assertEqual(self._status()["a"], "queued")

    def test_policy_rejects_concurrent_config_changes(self):
        with tempfile.TemporaryDirectory() as root:
            env = Path(root) / "user.env"
            env.write_text("STRM_METADATA_ENABLED=false\n")
            with (
                patch("app.config.ENV_FILE", env),
                patch("app.config.has_external_override", return_value=False),
                patch("app.config.update_runtime_env_file") as update,
            ):
                _, token = service.prepare_policy(True, "owner")
                env.write_text("STRM_METADATA_ENABLED=false\nOTHER=changed\n")
                with self.assertRaises(ValueError):
                    service.set_policy_confirmed(token, "owner")
                update.assert_not_called()
