"""GCID 重试必须使用持久凭据与任务 CAS；未知在途写入不得重放。"""

from __future__ import annotations

import json
import multiprocessing
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import database as db
from app.modules import gcid_import
from tests.support import IsolatedDatabaseTestCase
from tests.test_gcid_import_api import TEST_ACCOUNT_KEY, FakeImporter, FakeOutcome, mock_importer


class _RetryProcessGates:
    def __init__(self, context):
        self.ready = context.Event()
        self.start = context.Event()
        self.entered = context.Event()
        self.executing = context.Event()
        self.finished = context.Event()
        self.before_claim = context.Event()


def _wait_for_phase(event, phase, timeout=10):
    if not event.wait(timeout):
        raise AssertionError(f"process handshake timed out: {phase}")


def _process_result(results, phase):
    from queue import Empty

    try:
        return results.get(timeout=3)
    except Empty as exc:
        raise AssertionError(f"process result timed out: {phase}") from exc


def _retry_in_process(
    database_path, task_id, token, marker, gates, release, results,
    claim_release, claims, collide,
):
    # Spawn imports the tests package first (isolated runtime per process).
    # Only the scratch database is deliberately shared by both contenders.
    class Importer:
        def import_file(self, **kwargs):
            with open(marker, "a", encoding="utf-8") as stream:
                stream.write(token + "\n")
            return FakeOutcome(True, remote_file_id="synthetic-remote")

    original_execute = gcid_import._execute_items
    original_claim = gcid_import._claim_task_for_run

    def execute_after_gate(*args, **kwargs):
        gates.executing.set()
        _wait_for_phase(release, token + " execute release", timeout=15)
        return original_execute(*args, **kwargs)

    @contextmanager
    def claim_after_gate(*args, **kwargs):
        if collide:
            # Both callers have passed retry_task's real failed-item precheck.
            gates.before_claim.set()
            _wait_for_phase(claim_release, token + " CAS release")
        with original_claim(*args, **kwargs) as claimed:
            claims.put((token, claimed))
            yield claimed

    try:
        db.configure_database(database_path, test_mode=True)
        with (
            patch.object(gcid_import, "_execute_items", side_effect=execute_after_gate),
            patch.object(gcid_import, "_claim_task_for_run", side_effect=claim_after_gate),
            mock_importer(Importer()),
            patch.object(gcid_import.notifier, "notify_gcid_import_started"),
            patch.object(gcid_import.notifier, "notify_gcid_import_finished"),
        ):
            gates.ready.set()
            # Startup is separate from the bounded, already-warm execute gate.
            _wait_for_phase(gates.start, token + " start permit", timeout=30)
            gates.entered.set()
            task, replayed = gcid_import.retry_task(task_id, operation_token=token)
            results.put((token, task["status"], replayed))
    except BaseException as exc:
        results.put((token, "error", type(exc).__name__))
        raise
    finally:
        gates.finished.set()


class GCIDRetryDurabilityAuditTests(IsolatedDatabaseTestCase):
    def setUp(self):
        gcid_import.reset_runtime_state()
        self.enterContext(patch.object(
            gcid_import, "_open_importer",
            side_effect=AssertionError("重放读取不得请求写入器"),
        ))
        self.enterContext(patch.object(gcid_import.notifier, "notify_gcid_import_started"))
        self.enterContext(patch.object(gcid_import.notifier, "notify_gcid_import_finished"))

    def _task(self, suffix=""):
        task_id = db.create_gcid_import_task(
            operation_token=self._testMethodName + suffix,
            manifest_digest="a" * 64,
            target_dir_id="target",
            target_account_key=TEST_ACCOUNT_KEY,
            file_count=1,
            total_size=1,
        )
        db.replace_gcid_import_items(
            task_id,
            [{"path": "Film.mkv", "size": 1, "gcid": "gcid-film", "status": "failed"}],
        )
        gcid_import._finish_task(task_id)
        return task_id

    def test_retry_token_survives_runtime_reset_even_when_retry_failed(self):
        task_id = self._task()
        fake = FakeImporter({"Film.mkv": [FakeOutcome(False), FakeOutcome(False)]})
        with mock_importer(fake):
            first, replayed = gcid_import.retry_task(
                task_id, operation_token="retry-once"
            )
            self.assertFalse(replayed)
            self.assertEqual(first["status"], "failed")
            gcid_import.reset_runtime_state()
            again, replayed = gcid_import.retry_task(
                task_id, operation_token="retry-once"
            )
        self.assertTrue(replayed)
        self.assertEqual(again["status"], "failed")
        self.assertEqual(len(fake.calls), 1)

    def test_orphaned_task_is_not_retried_or_advertised_as_retryable(self):
        task_id = self._task()
        db.update_gcid_import_task(task_id, status="running")
        fake = FakeImporter()
        with mock_importer(fake):
            current, replayed = gcid_import.retry_task(
                task_id, operation_token="while-running"
            )
        self.assertEqual(fake.calls, [])
        self.assertTrue(replayed)
        self.assertEqual(current["status"], "interrupted")
        self.assertFalse(current["can_retry"])
        get_gcid_import_task_result = db.get_gcid_import_task(task_id)
        assert get_gcid_import_task_result is not None
        self.assertEqual(get_gcid_import_task_result["status"], "running")

    def test_replayed_reply_rereads_after_another_process_claims(self):
        task_id = self._task()

        def claimed_between_reads(selected_id, token):
            self.assertTrue(
                self.enterContext(gcid_import._claim_task_for_run(selected_id, retry_token=token))
            )
            return True

        with patch.object(
            gcid_import, "_retry_was_recorded", side_effect=claimed_between_reads
        ):
            current, replayed = gcid_import.retry_task(
                task_id, operation_token="interleaved"
            )
        self.assertTrue(replayed)
        self.assertEqual(current["status"], "running")
        self.assertFalse(current["can_retry"])

    def test_new_token_can_retry_and_identical_tokens_on_other_tasks_are_independent(
        self,
    ):
        first = self._task("-a")
        second = self._task("-b")
        fake = FakeImporter(
            {"Film.mkv": [FakeOutcome(False), FakeOutcome(True), FakeOutcome(True)]}
        )
        before = db.list_gcid_import_items(first)[0]["id"]
        with mock_importer(fake):
            gcid_import.retry_task(first, operation_token="old")
            done, replayed = gcid_import.retry_task(first, operation_token="new")
            other, other_replayed = gcid_import.retry_task(
                second, operation_token="new"
            )
        self.assertEqual((done["status"], other["status"]), ("success", "success"))
        self.assertFalse(replayed or other_replayed)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(db.list_gcid_import_items(first)[0]["id"], before)

    def test_persistent_replay_lru_keeps_recent_hits_and_stays_bounded(self):
        task_id = self._task()
        fake = FakeImporter({"Film.mkv": [FakeOutcome(False), FakeOutcome(True)]})
        with mock_importer(fake):
            gcid_import.retry_task(task_id, operation_token="keep-recent")
            ledger = json.loads(db.kv_get(gcid_import._RETRY_REPLAY_KEY))
            self.assertIn([task_id, "keep-recent"], ledger)
            ledger = [
                [task_id, "keep-recent"],
                *[[10000 + i, f"historical-{i}"] for i in range(511)],
            ]
            db.kv_set(gcid_import._RETRY_REPLAY_KEY, json.dumps(ledger))
            _old, replayed = gcid_import.retry_task(
                task_id, operation_token="keep-recent"
            )
            self.assertTrue(replayed)
            gcid_import.reset_runtime_state()
            final, replayed = gcid_import.retry_task(
                task_id, operation_token="new-explicit"
            )
        self.assertFalse(replayed)
        self.assertEqual(final["status"], "success")
        stored = json.loads(db.kv_get(gcid_import._RETRY_REPLAY_KEY))
        self.assertEqual(len(stored), 512)
        self.assertIn([task_id, "keep-recent"], stored)
        self.assertIn([task_id, "new-explicit"], stored)
        self.assertNotIn([10000, "historical-0"], stored)
        self.assertEqual(len(fake.calls), 2)

    def test_two_real_processes_cannot_execute_the_same_failed_item(self):
        self._run_two_process_retries(collide=False)

    def test_two_real_processes_collide_at_the_durable_claim(self):
        self._run_two_process_retries(collide=True)

    def _run_two_process_retries(self, *, collide):
        expected_database = self.test_db_path.resolve()
        self.assertEqual(db.resolve_db_path(), expected_database)
        with db.get_conn() as conn:
            databases = conn.execute("PRAGMA database_list").fetchall()
        actual_database = next(row[2] for row in databases if row[1] == "main")
        self.assertEqual(Path(actual_database).resolve(), expected_database)
        task_id = self._task()
        context = multiprocessing.get_context("spawn")
        gates = [_RetryProcessGates(context) for _ in range(2)]
        release = context.Event()
        claim_release = context.Event()
        results = context.Queue()
        claims = context.Queue()
        tokens = ("process-a", "process-b")
        with TemporaryDirectory() as directory:
            marker = str(Path(directory) / "calls.txt")
            workers = [
                context.Process(
                    target=_retry_in_process,
                    args=(
                        str(self.test_db_path), task_id, token, marker, gates[index],
                        release, results, claim_release, claims, collide,
                    ),
                )
                for index, token in enumerate(tokens)
            ]
            try:
                # No interpreter startup occurs while an owner holds the gate.
                for worker in workers:
                    worker.start()
                for token, gate in zip(tokens, gates):
                    _wait_for_phase(gate.ready, token + " imports/database ready")
                if collide:
                    for gate in gates:
                        gate.start.set()
                    for token, gate in zip(tokens, gates):
                        _wait_for_phase(gate.before_claim, token + " before real CAS")
                    get_gcid_import_task_result_2 = db.get_gcid_import_task(task_id)
                    assert get_gcid_import_task_result_2 is not None
                    self.assertEqual(get_gcid_import_task_result_2["status"], "failed")
                    claim_release.set()
                    claim_results = [
                        _process_result(claims, "real CAS result") for _ in workers
                    ]
                    self.assertCountEqual([item[0] for item in claim_results], tokens)
                    self.assertCountEqual([item[1] for item in claim_results], [True, False])
                    winner = next(token for token, claimed in claim_results if claimed)
                else:
                    winner = "process-a"
                    gates[0].start.set()
                owner = tokens.index(winner)
                follower = 1 - owner
                _wait_for_phase(gates[owner].executing, winner + " owns execute gate")
                get_gcid_import_task_result_3 = db.get_gcid_import_task(task_id)
                assert get_gcid_import_task_result_3 is not None
                self.assertEqual(get_gcid_import_task_result_3["status"], "running")
                self.assertEqual(gcid_import.list_tasks()[0]["status"], "running")
                self.assertFalse(Path(marker).exists())
                if not collide:
                    self.assertEqual(_process_result(claims, "A real CAS"), (winner, True))
                    gates[follower].start.set()
                _wait_for_phase(gates[follower].entered, tokens[follower] + " retry entry")
                _wait_for_phase(gates[follower].finished, tokens[follower] + " retry return")
                early = _process_result(results, "running follower reply")
                # The return value, not a timeout, establishes overlap.
                self.assertEqual(early, (tokens[follower], "running", True))
                self.assertFalse(gates[follower].executing.is_set())
                self.assertFalse(gates[owner].finished.is_set())
                self.assertFalse(Path(marker).exists())
                release.set()
                _wait_for_phase(gates[owner].finished, winner + " completed execution")
                self.assertEqual(
                    _process_result(results, "owner success reply"),
                    (winner, "success", False),
                )
                for token, worker in zip(tokens, workers):
                    worker.join(10)
                    self.assertFalse(worker.is_alive(), token + " did not exit")
                self.assertEqual([worker.exitcode for worker in workers], [0, 0])
                calls = Path(marker).read_text().splitlines()
                self.assertEqual(calls, [winner])
                if not collide:
                    self.assertEqual(calls, ["process-a"])
                    self.assertEqual(early, ("process-b", "running", True))
                get_gcid_import_task_result_4 = db.get_gcid_import_task(task_id)
                assert get_gcid_import_task_result_4 is not None
                self.assertEqual(get_gcid_import_task_result_4["status"], "success")
                self.assertEqual(
                    [item["status"] for item in db.list_gcid_import_items(task_id)],
                    ["success"],
                )
                gcid_import.reset_runtime_state()
                replay, replayed = gcid_import.retry_task(task_id, operation_token=winner)
                self.assertEqual(replay["status"], "success")
                self.assertTrue(replayed)
                with self.assertRaisesRegex(ValueError, "任务没有可重试的失败项"):
                    gcid_import.retry_task(task_id, operation_token="fresh-after-success")
                self.assertEqual(Path(marker).read_text().splitlines(), calls)
            finally:
                # A failed phase stays a failure; release only to clean up.
                release.set()
                claim_release.set()
                for gate in gates:
                    gate.start.set()
                for worker in workers:
                    if worker.pid is not None:
                        worker.join(3)
                        if worker.is_alive():
                            worker.terminate()
                            worker.join(5)
                for channel in (results, claims):
                    channel.close()
                    channel.join_thread()
