from __future__ import annotations

import tempfile
import threading
import time
import unittest
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

import app.clients.guangya as guangya_module
from app.clients.guangya import GuangYaClient, GuangYaFile, GuangYaReadMetrics
from app.modules.strm import sync_strm
from tests.support import isolated_test_database


class _ConcurrentTreeClient:
    def __init__(self, workers: int = 15):
        self.workers = workers
        self.barrier = threading.Barrier(workers)
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls: list[str] = []

    def iter_dir(self, dir_id: str, *, should_stop=None, max_items=None):
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
    def iter_dir(self, dir_id: str, *, should_stop=None, max_items=None):
        if dir_id == "dir-0":
            raise RuntimeError("temporary directory failure")
        return super().iter_dir(dir_id, should_stop=should_stop, max_items=max_items)


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
            def iter_dir(self, _dir_id: str, *, should_stop=None, max_items=None):
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
    def test_healthy_reads_keep_parallelism_without_fixed_qps(self):
        barrier = threading.Barrier(15)
        clients = [object.__new__(GuangYaClient) for _ in range(15)]

        def callback():
            barrier.wait(timeout=3)
            return {"code": 0}

        with (
            patch.dict(guangya_module._READ_CONGESTION, {}, clear=True),
            patch("app.clients.guangya.sleep") as sleep_mock,
            ThreadPoolExecutor(max_workers=15) as pool,
        ):
            futures = [pool.submit(c._call_read, "list_dir", callback) for c in clients]
            self.assertEqual([f.result(timeout=4) for f in futures], [{"code": 0}] * 15)
            sleep_mock.assert_not_called()

    def test_concurrent_clients_share_one_cooldown_for_the_same_rejection_wave(self):
        barrier = threading.Barrier(15)
        metrics = GuangYaReadMetrics()

        def run():
            client = object.__new__(GuangYaClient)
            client._read_metrics_lock = threading.Lock()
            client._read_metrics = metrics
            first = True

            def callback():
                nonlocal first
                if first:
                    first = False
                    barrier.wait(timeout=5)
                    return {"code": 127}
                return {"code": 0}

            return client._call_read("list_dir", callback)

        with (
            patch.dict(guangya_module._READ_CONGESTION, {}, clear=True),
            patch("app.clients.guangya.random.uniform", return_value=0),
            ThreadPoolExecutor(max_workers=15) as pool,
        ):
            futures = [pool.submit(run) for _ in range(15)]
            self.assertEqual([f.result(timeout=10) for f in futures], [{"code": 0}] * 15)
            gate = guangya_module._read_congestion("list_dir")
            self.assertEqual(gate.generation, 1)
            self.assertEqual(gate.interval, 0.125)
        self.assertEqual(metrics.requests, 30)
        self.assertEqual(metrics.rate_limit_retries, 15)

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


class GuangYaAdaptiveReadTests(unittest.TestCase):
    """虚拟时间验证规模吞吐与拥塞恢复，不对生产服务做压力测试。"""

    def setUp(self):
        self.now = 100.0
        self.delays = []
        for patcher in (
            patch.dict(guangya_module._READ_CONGESTION, {}, clear=True),
            patch("app.clients.guangya.monotonic", side_effect=lambda: self.now),
            patch("app.clients.guangya.sleep", side_effect=self.advance),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = object.__new__(GuangYaClient)

    def advance(self, seconds):
        self.delays.append(seconds)
        self.now += seconds

    def test_large_healthy_library_adds_no_rate_limit_wait(self):
        for size in (3244, 32440):
            with self.subTest(requests=size):
                for _ in range(size):
                    self.client._call_read("list_dir", lambda: {"code": 0})
                self.assertEqual(self.delays, [])

    def test_sustained_provider_limit_recovers_without_unbounded_retry(self):
        accepted_at = deque()
        requests = 0

        def callback():
            nonlocal requests
            requests += 1
            while accepted_at and accepted_at[0] <= self.now - 1:
                accepted_at.popleft()
            if len(accepted_at) >= 8:
                return {"code": 127}
            accepted_at.append(self.now)
            return {"code": 0}

        with patch("app.clients.guangya.random.uniform", return_value=0):
            for _ in range(400):
                self.assertEqual(self.client._call_read("list_dir", callback), {"code": 0})
        self.assertGreater(requests, 400)
        self.assertLess(requests, 480)
        self.assertLess(self.now - 100, 120)

    def test_same_burst_failures_merge_and_old_success_cannot_clear_cooldown(self):
        gate = guangya_module._read_congestion("list_dir")
        generations = [gate.acquire() for _ in range(15)]
        for generation in generations:
            gate.rejected(generation, 1)
            gate.succeeded(generation)
        self.assertEqual(gate.generation, 1)
        self.assertEqual(gate.interval, 0.125)
        self.assertEqual(gate.next_at, 101.0)
        self.assertEqual(gate.successes, 0)
        self.assertEqual(gate.acquire(), 1)
        self.assertEqual(self.delays, [1.0])

    def test_successes_restore_full_speed_instead_of_permanent_low_qps(self):
        gate = guangya_module._read_congestion("list_dir")
        gate.rejected(gate.acquire(), 1)
        intervals = []
        for _ in range(48):
            generation = gate.acquire()
            gate.succeeded(generation)
            intervals.append(gate.interval)
        self.assertEqual([intervals[i] for i in (15, 31, 47)], [0.0625, 0.03125, 0])
        self.delays.clear()
        for _ in range(100):
            self.client._call_read("list_dir", lambda: {"code": 0})
        self.assertEqual(self.delays, [])

    def test_repeated_congestion_is_bounded_and_affects_only_same_endpoint(self):
        gate = guangya_module._read_congestion("list_dir")
        for _ in range(10):
            gate.rejected(gate.acquire(), 1)
        self.assertEqual(gate.interval, 2)
        self.delays.clear()
        for operation in ("get_download_url", "file_info", "task_status"):
            self.client._call_read(operation, lambda: {"code": 0})
        self.assertEqual(self.delays, [])
        self.client._call_read("connection_probe", lambda: {"code": 0})
        self.assertGreater(sum(self.delays), 0)

    def test_wait_rechecks_cooldown_when_another_request_is_rejected(self):
        gate = guangya_module._read_congestion("list_dir")
        gate.rejected(gate.acquire(), 1)
        calls = []

        def advance_and_reject(seconds):
            self.advance(seconds)
            if not calls:
                calls.append(1)
                gate.rejected(gate.generation, 2)

        with patch("app.clients.guangya.sleep", side_effect=advance_and_reject):
            gate.acquire()
        self.assertEqual(self.delays, [1.0, 2.0])
        self.assertEqual(gate.generation, 2)

    def test_deadline_rejects_wait_without_consuming_a_slot(self):
        gate = guangya_module._read_congestion("get_download_url")
        gate.rejected(gate.acquire(), 2)
        callback = Mock(return_value={"code": 0})
        with self.assertRaises(guangya_module.httpx.TimeoutException):
            self.client._call_read("get_download_url", callback, deadline=101)
        callback.assert_not_called()
        self.assertEqual(gate.next_at, 102)
        self.assertEqual(self.delays, [])

    def test_oversleep_past_deadline_cannot_send_network_request(self):
        gate = guangya_module._read_congestion("get_download_url")
        gate.rejected(gate.acquire(), 0.5)
        callback = Mock(return_value={"code": 0})
        with (
            patch("app.clients.guangya.sleep", side_effect=lambda t: self.advance(t + 1)),
            self.assertRaises(guangya_module.httpx.TimeoutException),
        ):
            self.client._call_read("get_download_url", callback, deadline=101)
        callback.assert_not_called()

    def test_cancel_interrupts_directory_cooldown_without_next_request(self):
        gate = guangya_module._read_congestion("list_dir")
        gate.rejected(gate.acquire(), 10)
        callback = Mock()
        with (
            patch.object(GuangYaClient, "raw", new_callable=PropertyMock) as raw,
        ):
            raw.return_value.fs_files = callback
            rows = list(self.client.iter_dir("root", should_stop=lambda: self.now >= 100.1))
        self.assertEqual(rows, [])
        callback.assert_not_called()
        self.assertAlmostEqual(sum(self.delays), 0.1)

    def test_cancel_during_network_backoff_prevents_retry(self):
        callback = Mock(side_effect=TimeoutError("temporary"))
        with self.assertRaises(guangya_module._ReadCancelled):
            self.client._call_read(
                "list_dir", callback, should_stop=lambda: self.now >= 100.1
            )
        callback.assert_called_once()

    def test_paginated_rate_limit_retries_same_page_and_keeps_all_entries(self):
        callback = Mock(side_effect=[
            {"code": 0, "data": {"total": 2, "list": [{"fileId": "1", "fileName": "a"}]}},
            {"code": 127, "msg": "操作过于频繁"},
            {"code": 0, "data": {"total": 2, "list": [{"fileId": "2", "fileName": "b"}]}},
        ])
        self.client._read_metrics_lock = threading.Lock()
        self.client._read_metrics = None
        metrics = self.client.begin_read_metrics()
        with patch.object(GuangYaClient, "raw", new_callable=PropertyMock) as raw:
            raw.return_value.fs_files = callback
            rows = self.client.list_dir("root")
        self.assertEqual([row.file_id for row in rows], ["1", "2"])
        self.assertEqual([c.kwargs["page"] for c in callback.call_args_list], [0, 1, 1])
        observed = self.client.end_read_metrics(metrics)
        self.assertEqual(observed["read_retries"], 1)
        self.assertEqual(observed["rate_limit_retries"], 1)
        self.assertEqual(observed["scan_pages"], 2)
        self.assertEqual(observed["directory_requests"], 3)


class GuangYaAdaptiveScanIntegrationTests(unittest.TestCase):
    def test_full_scan_recovery_and_repeat_preserve_every_file(self):
        calls = []
        limited = False
        lock = threading.Lock()

        def files(*, parent_id, page, page_size):
            nonlocal limited
            with lock:
                calls.append((parent_id, page))
                if parent_id == "dir-1" and not limited:
                    limited = True
                    return {"code": 127, "msg": "操作过于频繁"}
            if parent_id == "root":
                rows = [
                    {"fileId": f"dir-{i}", "fileName": f"Show-{i}", "resType": 2}
                    for i in range(300)
                ]
            else:
                rows = [
                    {"fileId": f"{parent_id}-{i}", "fileName": f"S01E{i+1:02d}.mkv", "resType": 1}
                    for i in range(4)
                ]
            return {"code": 0, "data": {"total": len(rows), "list": rows[page*page_size:(page+1)*page_size]}}

        client = object.__new__(GuangYaClient)
        client._read_metrics_lock = threading.Lock()
        client._read_metrics = None
        with (
            tempfile.TemporaryDirectory() as root,
            isolated_test_database(),
            patch.dict(guangya_module._READ_CONGESTION, {}, clear=True),
            patch.object(GuangYaClient, "raw", new_callable=PropertyMock) as raw,
        ):
            raw.return_value.fs_files.side_effect = files
            stats = sync_strm("root", "http://media.invalid", root, client=client,
                              clean_invalid=False, clean_empty_dirs=False, scan_workers=15)
            self.assertFalse(stats["scan_incomplete"])
            self.assertEqual(stats["directories"], 301)
            self.assertEqual(stats["directory_requests"], 303)  # 根目录两页 + 重试一页
            self.assertEqual(stats["generated"], 1200)
            self.assertEqual(stats["failed"], 0)
            self.assertEqual(stats["rate_limit_retries"], 1)
            before = {str(p): p.read_bytes() for p in Path(root).rglob("*.strm")}
            self.assertEqual(len(before), 1200)
            calls.clear()
            again = sync_strm("root", "http://media.invalid", root, client=client,
                              clean_invalid=False, clean_empty_dirs=False, scan_workers=15)
            self.assertEqual(again["generated"], 0)
            self.assertEqual(again["skipped"], 1200)
            self.assertEqual(again["read_retries"], 0)
            self.assertEqual(len(calls), 302)
            self.assertEqual(before, {str(p): p.read_bytes() for p in Path(root).rglob("*.strm")})

    def test_exhausted_rate_limit_never_cleans_existing_strm(self):
        client = object.__new__(GuangYaClient)
        client._read_metrics_lock = threading.Lock()
        client._read_metrics = None
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(guangya_module._READ_CONGESTION, {}, clear=True),
            patch.object(GuangYaClient, "raw", new_callable=PropertyMock) as raw,
            patch("app.modules.strm.clean_invalid_strm") as cleanup,
        ):
            path = Path(root) / "existing.strm"
            path.write_text("http://existing.invalid", encoding="utf-8")
            raw.return_value.fs_files.return_value = {"code": 127}
            stats = sync_strm("root", "http://media.invalid", root, client=client)
            self.assertTrue(stats["scan_incomplete"])
            self.assertTrue(stats["clean_skipped"])
            self.assertEqual(stats["generated"], 0)
            self.assertEqual(stats["directory_requests"], 3)
            self.assertEqual(stats["rate_limit_retries"], 2)
            self.assertEqual(path.read_text(), "http://existing.invalid")
            cleanup.assert_not_called()

    def test_full_scan_deadline_during_cooldown_never_cleans(self):
        client = object.__new__(GuangYaClient)
        client._read_metrics_lock = threading.Lock()
        client._read_metrics = None
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(guangya_module._READ_CONGESTION, {}, clear=True),
            patch.object(GuangYaClient, "raw", new_callable=PropertyMock) as raw,
            patch("app.modules.strm._scan_limits", return_value=(100, 1000, 1000, 0.05)),
            patch("app.modules.strm.clean_invalid_strm") as cleanup,
        ):
            gate = guangya_module._read_congestion("list_dir")
            gate.rejected(gate.acquire(), 10)
            stats = sync_strm("root", "http://media.invalid", root, client=client)
            raw.return_value.fs_files.assert_not_called()
        self.assertTrue(stats["scan_incomplete"])
        self.assertEqual(stats["scan_limit_reason"], "deadline")
        self.assertTrue(stats["clean_skipped"])
        cleanup.assert_not_called()

    def test_stop_at_last_empty_page_cannot_become_successful_empty_scan(self):
        stopped = threading.Event()

        class Client:
            def iter_dir(self, *args, **kwargs):
                stopped.set()
                return iter(())

        with (
            tempfile.TemporaryDirectory() as root,
            patch("app.modules.strm.db.list_strm_index", return_value=[]),
            patch("app.modules.strm.clean_invalid_strm") as cleanup,
        ):
            stats = sync_strm("root", "http://media.invalid", root, client=Client(),
                              should_stop=stopped.is_set)
        self.assertTrue(stats["stopped"])
        self.assertTrue(stats["clean_skipped"])
        cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
