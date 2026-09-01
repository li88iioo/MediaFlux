from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from app.clients.guangya import GuangYaClient, GuangYaFile, GuangYaReadMetrics
from app.modules.strm import sync_strm


class _ConcurrentTreeClient:
    def __init__(self, workers: int = 15):
        self.workers = workers
        self.barrier = threading.Barrier(workers)
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls: list[str] = []

    def iter_dir(self, dir_id: str, *, should_stop=None):
        with self.lock:
            self.calls.append(str(dir_id))
        if dir_id == "root":
            return iter([
                GuangYaFile(f"dir-{index}", f"目录 {index}", True)
                for index in range(self.workers)
            ])

        def rows():
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                self.barrier.wait(timeout=3)
                time.sleep(0.01)
                if should_stop and should_stop():
                    return
                yield from ()
            finally:
                with self.lock:
                    self.active -= 1

        return rows()


class _FailingConcurrentTreeClient(_ConcurrentTreeClient):
    def iter_dir(self, dir_id: str, *, should_stop=None):
        if dir_id == "dir-0":
            raise RuntimeError("temporary directory failure")
        return super().iter_dir(dir_id, should_stop=should_stop)


class _BudgetTreeClient:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.yielded = 0

    def iter_dir(self, dir_id: str, *, should_stop=None, max_items=None):
        count = 4 if dir_id == "root" else 20

        def rows():
            for index in range(count):
                if should_stop and should_stop():
                    return
                with self.lock:
                    self.yielded += 1
                if dir_id == "root":
                    yield GuangYaFile(f"dir-{index}", f"目录 {index}", True)
                else:
                    yield GuangYaFile(
                        f"{dir_id}-file-{index}", f"Episode {index}.mkv", False,
                        100, f"etag-{index}", dir_id,
                    )

        return rows()


class StrmDirectoryConcurrencyTests(unittest.TestCase):
    def test_full_scan_reaches_fifteen_directory_workers(self):
        client = _ConcurrentTreeClient(15)
        with patch("app.modules.strm.db.list_strm_index", return_value=[]):
            stats = sync_strm(
                "root",
                "http://media.invalid",
                "/tmp/mediaflux-strm-concurrency",
                client=client,
                clean_invalid=False,
                scan_workers=15,
            )

        self.assertEqual(client.peak, 15)
        self.assertEqual(stats["scan_workers_configured"], 15)
        self.assertEqual(stats["scan_workers_peak"], 15)
        self.assertEqual(stats["directories"], 16)
        self.assertEqual(len(set(client.calls)), 16)
        self.assertFalse(stats["scan_incomplete"])

    def test_one_directory_failure_aborts_generation_and_cleanup(self):
        client = _FailingConcurrentTreeClient(15)
        with patch("app.modules.strm.db.list_strm_index", return_value=[]), patch(
            "app.modules.strm.clean_invalid_strm"
        ) as cleanup:
            stats = sync_strm(
                "root",
                "http://media.invalid",
                "/tmp/mediaflux-strm-concurrency-failure",
                client=client,
                clean_invalid=True,
                scan_workers=15,
            )

        self.assertTrue(stats["scan_incomplete"])
        self.assertTrue(stats["clean_skipped"])
        self.assertEqual(stats["scan_limit_reason"], "directory_error")
        cleanup.assert_not_called()

    def test_deadline_reached_inside_worker_marks_scan_incomplete_before_cleanup(self):
        class DeadlineClient:
            def iter_dir(self, _dir_id: str, *, should_stop=None):
                time.sleep(0.01)
                if should_stop and should_stop():
                    return iter(())
                return iter(())

        with patch(
            "app.modules.strm._scan_limits", return_value=(100, 100, 100, 0.001)
        ), patch("app.modules.strm.db.list_strm_index", return_value=[]), patch(
            "app.modules.strm.clean_invalid_strm"
        ) as cleanup:
            stats = sync_strm(
                "root",
                "http://media.invalid",
                "/tmp/mediaflux-strm-deadline",
                client=DeadlineClient(),
                clean_invalid=True,
                scan_workers=15,
            )

        self.assertTrue(stats["scan_incomplete"])
        self.assertEqual(stats["scan_limit_reason"], "deadline")
        self.assertTrue(stats["clean_skipped"])
        cleanup.assert_not_called()

    def test_global_entry_budget_is_not_multiplied_by_directory_workers(self):
        client = _BudgetTreeClient()
        with patch(
            "app.modules.strm._scan_limits", return_value=(100, 10, 100, 60)
        ), patch("app.modules.strm.db.list_strm_index", return_value=[]):
            stats = sync_strm(
                "root",
                "http://media.invalid",
                "/tmp/mediaflux-strm-budget",
                client=client,
                clean_invalid=False,
                scan_workers=4,
            )

        self.assertTrue(stats["scan_incomplete"])
        self.assertEqual(stats["scan_limit_reason"], "entries")
        self.assertEqual(stats["scan_entries"], 10)
        self.assertLessEqual(client.yielded, 11)


class GuangYaReadMetricsTests(unittest.TestCase):
    def test_latency_samples_are_bounded_to_latest_requests(self):
        collector = GuangYaReadMetrics()
        with patch("app.clients.guangya._READ_METRICS_MAX_LATENCY_SAMPLES", 3):
            for milliseconds in (10, 20, 30, 40, 50):
                collector.record_request(milliseconds / 1000)

        metrics = collector.snapshot()
        self.assertEqual(metrics["directory_requests"], 5)
        self.assertEqual(metrics["latency_samples"], 3)
        self.assertEqual(metrics["latency_sampled"], 1)
        self.assertEqual(metrics["request_p50_ms"], 40.0)
        self.assertEqual(metrics["request_p95_ms"], 40.0)
        self.assertEqual(metrics["request_p99_ms"], 40.0)


class GuangYaRequestConcurrencyTests(unittest.TestCase):
    def test_request_policy_does_not_serialize_real_transport_requests(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        state = {"active": 0, "peak": 0}

        class Response:
            def raise_for_status(self):
                return None

        class Transport:
            def request(self, method, url, **kwargs):
                with lock:
                    state["active"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                try:
                    barrier.wait(timeout=2)
                    time.sleep(0.01)
                    return Response()
                finally:
                    with lock:
                        state["active"] -= 1

        class Raw:
            refresh_token_value = "refresh-secret"
            _client = Transport()

            def request(self, *_args, **_kwargs):
                raise AssertionError("SDK 自动重放入口不应被调用")

        client = object.__new__(GuangYaClient)
        client._request_policy_lock = threading.RLock()
        raw = Raw()
        client._install_request_retry_policy(raw)
        errors: list[BaseException] = []

        def run():
            try:
                raw.request("https://example.invalid/read", "POST")
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertEqual(errors, [])
        self.assertEqual(state["peak"], 2)
        self.assertEqual(raw.refresh_token_value, "refresh-secret")

    def test_read_metrics_count_attempts_retries_pages_and_latency(self):
        client = object.__new__(GuangYaClient)
        client._read_metrics_lock = threading.Lock()
        client._read_metrics = None
        collector = client.begin_read_metrics()
        calls = 0

        def callback():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("temporary")
            return {"ok": True}

        with patch("app.clients.guangya.sleep"):
            result = client._call_read("list_dir", callback)
        collector.record_page()
        metrics = client.end_read_metrics(collector)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(metrics["directory_requests"], 2)
        self.assertEqual(metrics["read_failures"], 1)
        self.assertEqual(metrics["read_retries"], 1)
        self.assertEqual(metrics["scan_pages"], 1)
        self.assertGreaterEqual(metrics["request_p95_ms"], 0)


if __name__ == "__main__":
    unittest.main()
