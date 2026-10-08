from __future__ import annotations

import heapq
import random
import tempfile
import threading
import time
import unittest
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from email.utils import formatdate
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


class GuangYaCongestionReplayTests(unittest.TestCase):
    """15个并发槽位的确定性回放；模拟预算不是服务端真实配额声明。"""

    @staticmethod
    def replay(limit, *, count=3345, rtt=.1, lifts_at=None):
        clock = [0.0]
        accepted, ready = deque(), deque((i, 0) for i in range(min(15, count)))
        next_job = len(ready)
        pending = []
        completed = failed = attempts = sequence = 0
        randomizer = random.Random(27)
        gate = guangya_module._ReadCongestion()

        def rounding_wait(delay, **_kwargs):
            # 事件循环预先推进至准入时刻，仅允许浮点舍入误差。
            if delay > 1e-7:
                raise AssertionError("unexpected admission wait")
            clock[0] += delay

        with (
            patch("app.clients.guangya.monotonic", side_effect=lambda: clock[0]),
            patch("app.clients.guangya._wait_read_delay", side_effect=rounding_wait),
        ):
            while ready or pending:
                clock[0] = min(
                    pending[0][0] if pending else float("inf"),
                    max(clock[0], gate.next_at) if ready else float("inf"),
                )
                while pending and pending[0][0] <= clock[0] + 1e-10:
                    _, _, job, attempt, generation, success = heapq.heappop(pending)
                    if success:
                        gate.succeeded(generation)
                        completed += 1
                    else:
                        delay = (.6, 1.2)[min(attempt, 1)]
                        gate.rejected(generation, delay + randomizer.uniform(0, delay * .25))
                        if attempt < 2:
                            ready.appendleft((job, attempt + 1))
                            continue
                        failed += 1
                    if next_job < count:
                        ready.append((next_job, 0))
                        next_job += 1
                while ready and gate.next_at <= clock[0] + 1e-10:
                    job, attempt = ready.popleft()
                    generation = gate.acquire()
                    attempts += 1
                    sequence += 1
                    while accepted and accepted[0] <= clock[0] - 1 + 1e-9:
                        accepted.popleft()
                    success = limit is None or (lifts_at is not None and clock[0] >= lifts_at) or len(accepted) < limit
                    if success:
                        accepted.append(clock[0])
                    heapq.heappush(pending, (clock[0] + rtt, sequence, job, attempt, generation, success))
        return clock[0], attempts, completed, failed

    def test_sustained_limits_avoid_repeated_fast_restart_cycles(self):
        # 旧实现对应耗时529.845/431.688/362.559/289.664/246.889/162.908s。
        # 同RTT、同随机种子、同请求量；健康场景另行验证，不能只降低重试而牺牲吞吐。
        for quota, seconds, max_rejections in ((8, 510, 96), (12, 350, 70), (16, 290, 100),
                                             (24, 215, 150), (32, 190, 160), (64, 115, 160)):
            with self.subTest(quota=quota):
                elapsed, requests, completed, failed = self.replay(quota)
                self.assertEqual((completed, failed), (3345, 0))
                self.assertLess(elapsed, seconds)
                self.assertLess(requests - completed, max_rejections)

    def test_severe_congestion_keeps_strong_backoff_and_retry_budget(self):
        # 与旧策略相同的严苛基线；温和探速不能在低配额+高RTT时放大耗尽。
        for quota, ceiling, failures in ((1, 700, 2), (2, 400, 2), (4, 210, 0)):
            with self.subTest(quota=quota):
                elapsed, requests, completed, failed = self.replay(quota, count=512, rtt=.5)
                self.assertLess(elapsed, ceiling)
                self.assertEqual(failed, failures)
                self.assertEqual(completed + failed, 512)
                self.assertLess(requests, 600)

    def test_healthy_and_transient_limits_do_not_impose_permanent_pacing(self):
        elapsed, requests, completed, failed = self.replay(None)
        self.assertAlmostEqual(elapsed, 22.3)
        self.assertEqual((requests, completed, failed), (3345, 3345, 0))
        elapsed, requests, completed, failed = self.replay(12, lifts_at=5)
        self.assertLess(elapsed, 33)
        self.assertLess(requests, 3360)
        self.assertEqual((completed, failed), (3345, 0))


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

    @staticmethod
    def _http_error(status, retry_after):
        response = guangya_module.httpx.Response(
            status, headers={"Retry-After": retry_after.encode("utf-8")},
            request=guangya_module.httpx.Request("POST", "https://provider.invalid/list"),
        )
        return guangya_module.httpx.HTTPStatusError("limited", request=response.request, response=response)

    def test_retry_after_seconds_and_http_date_honor_provider_cooldown(self):
        for status in (429, 503):
            for value in ("15", formatdate(1_800_000_015, usegmt=True)):
                with self.subTest(status=status, value=value), patch.dict(
                    guangya_module._READ_CONGESTION, {}, clear=True,
                ), patch("app.clients.guangya.time", return_value=1_800_000_000):
                    self.now = 100.0
                    starts = []
                    def callback():
                        starts.append(self.now)
                        if self.now < 115:
                            raise self._http_error(status, value)
                        return {"code": 0}
                    self.assertEqual(self.client._call_read("list_dir", callback), {"code": 0})
                    self.assertEqual(starts, [100.0, 115.0])

    def test_retry_after_invalid_past_and_unrelated_responses_do_not_wait(self):
        for value in ("", "nonsense", "-1", "0", "1.5", "NaN", "inf", "９", "9" * 400,
                      "Thu, 01 Jan 1970 00:00:00 GMT", "Wed, 21 Oct 2015 07:28:00"):
            with self.subTest(value=value):
                self.assertEqual(self.client._retry_after_delay(self._http_error(429, value)), 0)
        self.assertEqual(self.client._retry_after_delay(self._http_error(403, "15")), 0)

    def test_wrapped_retry_after_and_exception_cycle(self):
        wrapper = RuntimeError("sdk wrapper")
        error = self._http_error(429, "15")
        wrapper.__cause__ = error
        error.__context__ = wrapper
        self.assertEqual(self.client._retry_after_delay(wrapper), 15)

    def test_success_probe_cannot_shorten_server_cooldown(self):
        gate = guangya_module._read_congestion("list_dir")
        gate.interval = .125
        gate.defer(15)
        for _ in range(16):
            gate.succeeded(gate.generation)
        self.assertEqual(gate.interval, .0625)
        gate.acquire()
        self.assertEqual(self.now, 115)

    def test_late_rejections_keep_longest_cooldown_without_multiple_rate_penalties(self):
        gate = guangya_module._read_congestion("list_dir")
        generation = gate.acquire()
        for seconds in (10, 30, 5):
            gate.defer(seconds)
            gate.rejected(generation, .6)
        self.assertEqual(gate.generation, 1)
        self.assertEqual(gate.interval, .125)
        gate.acquire()
        self.assertEqual(self.now, 130)

    def test_retry_after_deadline_and_cancel_never_send_early_retry(self):
        for options, exception in (({"deadline": 101}, guangya_module.httpx.TimeoutException),
                                   ({"should_stop": lambda: self.now >= 100.1}, guangya_module._ReadCancelled)):
            with self.subTest(options=options), patch.dict(guangya_module._READ_CONGESTION, {}, clear=True):
                self.now = 100.0
                callback = Mock(side_effect=self._http_error(429, "15"))
                with self.assertRaises(exception):
                    self.client._call_read("list_dir", callback, **options)
                callback.assert_called_once()
                gate = guangya_module._read_congestion("list_dir")
                self.assertEqual(gate.waiting, [0, 0])
                self.assertEqual(gate.cooldown_until, 115)

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
        for _ in range(32):
            previous = gate.interval
            gate.rejected(gate.acquire(), 1)
            self.assertGreaterEqual(gate.interval, previous)
            self.assertLessEqual(gate.interval, 2)
        self.assertEqual(gate.interval, 2)
        self.delays.clear()
        for operation in ("get_download_url", "file_info", "task_status"):
            self.client._call_read(operation, lambda: {"code": 0})
        self.assertEqual(self.delays, [])
        self.client._call_read("connection_probe", lambda: {"code": 0})
        self.assertGreater(sum(self.delays), 0)

    def test_recent_rejection_after_recovery_reuses_probe_rate(self):
        gate = guangya_module._read_congestion("list_dir")
        gate.rejected(gate.acquire(), 1)
        for _ in range(48):
            gate.succeeded(gate.acquire())
        self.assertEqual(gate.interval, 0)
        self.assertEqual(gate.probe_rate, 32)
        gate.rejected(gate.acquire(), 1)
        self.assertAlmostEqual(gate.interval, 1 / (32 * .85))
        self.assertLess(gate.interval, .125)

    def test_repeated_limits_use_stable_window_and_additive_probe(self):
        gate = guangya_module._read_congestion("list_dir")
        gate.rejected(gate.acquire(), 1)
        for _ in range(16):
            gate.succeeded(gate.acquire())
        gate.rejected(gate.acquire(), 1)
        gate.rejected(gate.acquire(), 1)
        self.assertTrue(gate.sustained)
        previous = gate.interval
        for _ in range(63):
            gate.succeeded(gate.acquire())
        self.assertEqual(gate.interval, previous)
        gate.succeeded(gate.acquire())
        self.assertAlmostEqual(1 / gate.interval, 1 / previous + 1)
        # 限制解除后不能永久保留低速；最终重新无节拍读取。
        for _ in range(640):
            gate.succeeded(gate.acquire())
        self.assertEqual(gate.interval, 0)

    def test_old_probe_does_not_dictate_speed_after_long_idle(self):
        gate = guangya_module._read_congestion("list_dir")
        gate.rejected(gate.acquire(), 1)
        for _ in range(48):
            gate.succeeded(gate.acquire())
        self.now += 60
        gate.rejected(gate.acquire(), 1)
        self.assertEqual(gate.interval, .125)
        self.assertEqual(gate.ceiling_rate, 0)
        self.assertFalse(gate.sustained)

    def test_wait_metrics_are_separate_from_network_latency(self):
        gate = guangya_module._read_congestion("list_dir")
        gate.rejected(gate.acquire(), 1)
        self.client._read_metrics_lock = threading.Lock()
        self.client._read_metrics = None
        collector = self.client.begin_read_metrics()
        self.client._call_read("list_dir", lambda: {"code": 0})
        metrics = self.client.end_read_metrics(collector)
        self.assertEqual(metrics["read_wait_count"], 1)
        self.assertEqual(metrics["read_wait_seconds"], 1)
        self.assertEqual(metrics["read_wait_max_seconds"], 1)
        self.assertEqual(metrics["request_p95_ms"], 0)
        self.assertEqual(metrics["rate_limit_retries"], 0)
        # 多个线程累计时间与单次最大值不是同一种统计。
        collector.record_wait(2)
        self.assertEqual(collector.snapshot()["read_wait_seconds"], 3)
        self.assertEqual(collector.snapshot()["read_wait_max_seconds"], 2)

    def test_cancelled_wait_is_measured_but_never_sends_request(self):
        gate = guangya_module._read_congestion("list_dir")
        gate.rejected(gate.acquire(), 5)
        self.client._read_metrics_lock = threading.Lock()
        self.client._read_metrics = None
        collector = self.client.begin_read_metrics()
        callback = Mock(return_value={"code": 0})
        with self.assertRaises(guangya_module._ReadCancelled):
            self.client._call_read("list_dir", callback, should_stop=lambda: self.now >= 100.1)
        metrics = self.client.end_read_metrics(collector)
        callback.assert_not_called()
        self.assertEqual(metrics["read_wait_count"], 1)
        self.assertEqual(metrics["read_wait_seconds"], .1)
        self.assertEqual(metrics["directory_requests"], 0)

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


class GuangYaReadPriorityTests(unittest.TestCase):
    def test_foreground_preference_is_bounded_and_preserves_endpoint_spacing(self):
        gate = guangya_module._ReadCongestion(interval=.005)
        barrier = threading.Barrier(17)
        local = threading.local()
        original_wait = guangya_module._wait_read_delay
        order = []
        def wait(*args, **kwargs):
            if not getattr(local, "registered", False):
                local.registered = True
                barrier.wait(timeout=5)
            original_wait(*args, **kwargs)
        def read(background):
            gate.acquire(background=background)
            order.append((background, time.monotonic()))
        with patch.object(guangya_module, "_wait_read_delay", side_effect=wait), ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(read, background) for background in [True] * 4 + [False] * 12]
            barrier.wait(timeout=5)
            for future in futures:
                future.result(timeout=5)
        self.assertEqual([lane for lane, _ in order], [False, False, False, True] * 4)
        self.assertGreaterEqual(min(b[1] - a[1] for a, b in zip(order, order[1:])), .004)
        self.assertEqual(gate.waiting, [0, 0])

    def test_cancelled_preferred_waiter_releases_background(self):
        gate = guangya_module._ReadCongestion(interval=.005)
        entered = threading.Event()
        cancelled = threading.Event()
        def should_stop():
            entered.set()
            return cancelled.is_set()
        # 先令前台也等待一个真实冷却，再取消；其名额不可永久挡住后台。
        gate.defer(.1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            foreground = pool.submit(gate.acquire, should_stop=should_stop)
            self.assertTrue(entered.wait(timeout=2))
            background = pool.submit(gate.acquire, background=True)
            cancelled.set()
            with self.assertRaises(guangya_module._ReadCancelled):
                foreground.result(timeout=2)
            self.assertEqual(background.result(timeout=2), 0)
        self.assertEqual(gate.waiting, [0, 0])

    def test_strm_owned_clients_use_background_lane_without_changing_supplied_client(self):
        from app.modules.strm import _guangya_client_scope
        with patch("app.modules.strm.GuangYaClient") as factory:
            with _guangya_client_scope(None) as client:
                self.assertIs(client, factory.return_value)
            factory.assert_called_once_with(background_reads=True)
            client.close.assert_called_once_with()
            supplied = object.__new__(GuangYaClient)
            with _guangya_client_scope(supplied) as current:
                self.assertIs(current, supplied)
                self.assertFalse(current._background_reads)
            factory.assert_called_once()


class GuangYaAdaptiveScanIntegrationTests(unittest.TestCase):
    def test_changed_directory_pagination_preserves_existing_strm_and_index(self):
        files = [
            {"fileId": name, "fileName": name + ".mkv", "resType": 1,
             "parentId": "root", "gcid": "etag-" + name, "fileSize": 123}
            for name in ("A", "B", "C")
        ]
        endings = (
            {"code": 0, "data": {"total": 2, "list": []}},
            {"code": 0, "data": {"hasMore": False, "list": []}},
        )
        for ending in endings:
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as root, isolated_test_database(), patch.dict(
                guangya_module._READ_CONGESTION, {}, clear=True,
            ), patch.object(GuangYaClient, "raw", new_callable=PropertyMock) as raw:
                client = object.__new__(GuangYaClient)
                client._read_metrics_lock = threading.Lock()
                client._read_metrics = None
                raw.return_value.fs_files.return_value = {
                    "code": 0, "data": {"total": 3, "list": files},
                }
                first = sync_strm("root", "http://media.invalid", root, client=client)
                self.assertEqual(first["generated"], 3)
                before = {str(p): p.read_bytes() for p in Path(root).rglob("*.strm")}
                from app import database as db
                with db.get_conn() as conn:
                    before_index = [tuple(row) for row in conn.execute(
                        "SELECT source,file_id,strm_path,content_fingerprint FROM strm_index ORDER BY source,file_id"
                    )]
                raw.return_value.fs_files.reset_mock()
                raw.return_value.fs_files.side_effect = [
                    {"code": 0, "data": {"total": 3, "list": files[:2]}}, ending,
                ]
                current = sync_strm("root", "http://media.invalid", root, client=client)
                self.assertTrue(current["scan_incomplete"], f"cleaned={current['cleaned']}, remaining={len(list(Path(root).rglob('*.strm')))}")
                self.assertTrue(current["clean_skipped"])
                self.assertEqual(current["generated"], 0)
                self.assertEqual(current["cleaned"], 0)
                self.assertEqual(raw.return_value.fs_files.call_count, 2)
                self.assertEqual(before, {str(p): p.read_bytes() for p in Path(root).rglob("*.strm")})
                with db.get_conn() as conn:
                    self.assertEqual(before_index, [tuple(row) for row in conn.execute(
                        "SELECT source,file_id,strm_path,content_fingerprint FROM strm_index ORDER BY source,file_id"
                    )])

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
            if parent_id == "*":
                # 账号远大于本次来源，首个成本探测后继续目录枚举。
                return {"data": {"total": 1_000_000, "list": [
                    {"fileId": f"outside-{i}", "fileName": f"Other-{i}",
                     "resType": 2, "parentId": "0"} for i in range(page_size)
                ]}}
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
            self.assertEqual(stats["directory_requests"], 303)  # 根目录、重试、一次成本探测
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


class _SnapshotTreeClient(GuangYaClient):
    """仅替换SDK传输：生产客户端分页、快照和STRM管道均真实执行。"""
    def __init__(self, directories=64):
        self._read_metrics_lock = threading.Lock()
        self._read_metrics = None
        self.calls = []
        self.hook = None
        self.rows = [self.row("root", "Root", "0", True)]
        for i in range(directories):
            self.rows.extend([
                self.row(f"d{i}", f"Show-{i}", "root", True),
                self.row(f"f{i}", "S01E01.mkv", f"d{i}"),
            ])
        self._raw = Mock()
        self._raw.fs_files.side_effect = self.files

    @staticmethod
    def row(fid, name, parent, directory=False):
        return {"fileId": fid, "fileName": name, "parentId": parent,
                "resType": 2 if directory else 1, "fileSize": 123, "gcid": "etag"}

    @property
    def raw(self):
        return self._raw

    def files(self, *, parent_id, page, page_size):
        self.calls.append((parent_id, page))
        if self.hook:
            self.hook(parent_id, page)
        rows = [r.copy() for r in self.rows if parent_id == "*" or r["parentId"] == (parent_id or "0")]
        return {"data": {"total": len(rows), "list": rows[page * page_size:(page + 1) * page_size]}}


class GuangYaSnapshotScanTests(unittest.TestCase):
    def run_sync(self, root, client, **kwargs):
        return sync_strm("root", "http://media.invalid", root, client=client, **kwargs)

    def test_bulk_scan_repeat_and_verified_removal_use_the_same_index_pipeline(self):
        from app import database as db
        client = _SnapshotTreeClient()
        # 账号中的无关孤儿、来源外视频都不能变成来源内候选。
        client.rows.extend([
            client.row("orphan", "Orphan.mkv", "missing"),
            client.row("outside", "Outside.mkv", "0"),
        ])
        with tempfile.TemporaryDirectory() as root, isolated_test_database():
            first = self.run_sync(root, client)
            self.assertTrue(first["scan_bulk_used"])
            self.assertFalse(first["scan_incomplete"])
            self.assertEqual(first["generated"], 64)
            self.assertEqual(first["directories"], 65)
            self.assertEqual(first["scan_entries"], 128)
            self.assertEqual(client.calls, [("root", 0), ("*", 0), ("*", 0), (None, 0)])
            before = {str(p): p.read_bytes() for p in Path(root).rglob("*.strm")}
            client.calls.clear()
            again = self.run_sync(root, client)
            self.assertEqual(again["skipped"], 64)
            self.assertEqual(again["generated"], 0)
            self.assertEqual(again["cleaned"], 0)
            self.assertEqual(before, {str(p): p.read_bytes() for p in Path(root).rglob("*.strm")})
            # 从新的完整快照真实缺失，仍经原所有权/索引事务清理。
            client.rows = [r for r in client.rows if r["fileId"] != "f0"]
            last = self.run_sync(root, client)
            self.assertEqual(last["cleaned"], 1)
            self.assertEqual(len(db.list_strm_index("guangya:root")), 63)
            self.assertEqual(len(list(Path(root).rglob("*.strm"))), 63)

    def test_multi_page_snapshot_preserves_metadata_and_conflict_winners(self):
        from app import database as db
        client = _SnapshotTreeClient(300)
        for i in range(300):
            for episode in range(2, 5):
                client.rows.append(client.row(f"f{i}-e{episode}", f"S01E{episode:02d}.mkv", f"d{i}"))
            client.rows.append(client.row(f"nfo{i}", "tvshow.nfo", f"d{i}"))
        # 两个独立ID目录映射到同名路径，继续采用原稳定赢家规则。
        next(r for r in client.rows if r["fileId"] == "d1")["fileName"] = "Show-0"
        next(r for r in client.rows if r["fileId"] == "f1")["fileSize"] = 999
        with tempfile.TemporaryDirectory() as root, isolated_test_database():
            result = self.run_sync(root, client, metadata_exts={"nfo"})
            self.assertTrue(result["scan_bulk_used"])
            self.assertFalse(result["scan_incomplete"])
            self.assertEqual(result["directories"], 301)
            self.assertEqual(result["generated"], 1196)
            self.assertEqual(result["duplicates_skipped"], 4)
            self.assertEqual(result["metadata_queued"], 299)
            self.assertEqual(len(client.calls), 6)
            output = Path(root) / "光鸭云盘" / "Show-0" / "S01E01.strm"
            self.assertIn("/f1/", output.read_text())
            with db.get_conn() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM strm_metadata_queue").fetchone()[0], 299)
            self.assertEqual(len(db.list_strm_index("guangya:root")), 1196)

    def test_snapshot_deadline_is_not_a_directory_error(self):
        client = _SnapshotTreeClient()
        def slow(parent, page):
            if parent == "*":
                time.sleep(0.15)
        client.hook = slow
        with tempfile.TemporaryDirectory() as root, isolated_test_database(), patch(
            "app.modules.strm._scan_limits", return_value=(1000, 1000, 1000, 0.1),
        ):
            result = self.run_sync(root, client)
            self.assertTrue(result["scan_incomplete"])
            self.assertEqual(result["scan_limit_reason"], "deadline")
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["generated"], 0)

    def test_explicit_source_remains_repeatable_when_its_outer_parent_is_missing(self):
        client = _SnapshotTreeClient()
        client.rows[0]["parentId"] = "missing-outside-source"
        with tempfile.TemporaryDirectory() as root, isolated_test_database():
            first = self.run_sync(root, client)
            self.assertFalse(first["scan_incomplete"])
            self.assertEqual(first["generated"], 64)
            again = self.run_sync(root, client)
            self.assertFalse(again["scan_incomplete"])
            self.assertEqual(again["skipped"], 64)
            self.assertEqual(again["cleaned"], 0)
            self.assertEqual(again["failed"], 0)

    def test_small_source_never_reads_account(self):
        with tempfile.TemporaryDirectory() as root, isolated_test_database():
            client = _SnapshotTreeClient(3)
            result = self.run_sync(root, client)
            self.assertFalse(result["scan_bulk_used"])
            self.assertEqual(result["generated"], 3)
            self.assertNotIn("*", [parent for parent, page in client.calls])

    def test_stable_retry_recovers_file_change_but_not_previously_observed_directory_change(self):
        for during in (1, 2):
            with self.subTest(during=during), tempfile.TemporaryDirectory() as root, isolated_test_database():
                client = _SnapshotTreeClient()
                self.run_sync(root, client)
                before = {str(p): p.read_bytes() for p in Path(root).rglob("*.strm")}
                rounds = 0
                def mutate(parent, page):
                    nonlocal rounds
                    if parent == "*" and page == 0:
                        rounds += 1
                        if rounds == during:
                            # 第一轮改已读目录 / 第二轮改普通文件，total均不变。
                            target = "d0" if during == 1 else "f0"
                            next(r for r in client.rows if r["fileId"] == target)["fileName"] = "Changed.mkv"
                client.hook = mutate
                result = self.run_sync(root, client)
                if during == 2:
                    self.assertFalse(result["scan_incomplete"])
                    self.assertFalse(result["clean_skipped"])
                    self.assertEqual(rounds, 4, "变化后必须重新取得两轮一致完整快照")
                    self.assertEqual(result["generated"], 1)
                    self.assertEqual(len(list(Path(root).rglob("*.strm"))), 64)
                    continue
                self.assertTrue(result["scan_incomplete"])
                self.assertTrue(result["clean_skipped"])
                self.assertEqual(result["generated"], 0)
                self.assertEqual(result["cleaned"], 0)
                self.assertEqual(before, {str(p): p.read_bytes() for p in Path(root).rglob("*.strm")})

    def test_indexed_orphan_prevents_retirement_but_known_outside_move_is_cleaned(self):
        for destination in ("missing", "0"):
            with self.subTest(destination=destination), tempfile.TemporaryDirectory() as root, isolated_test_database():
                client = _SnapshotTreeClient()
                self.run_sync(root, client)
                next(r for r in client.rows if r["fileId"] == "f0")["parentId"] = destination
                result = self.run_sync(root, client)
                self.assertEqual(result["scan_incomplete"], destination == "missing")
                self.assertEqual(result["cleaned"], 0 if destination == "missing" else 1)
                self.assertEqual(len(list(Path(root).rglob("*.strm"))), 64 if destination == "missing" else 63)

    def test_bulk_obeys_scoped_entry_directory_and_candidate_budgets(self):
        cases = ((64, 1000, 1000, "directories"), (1000, 100, 1000, "entries"), (1000, 1000, 5, "candidates"))
        for dirs, entries, candidates, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root, isolated_test_database(), patch(
                "app.modules.strm._scan_limits", return_value=(dirs, entries, candidates, 100),
            ):
                result = self.run_sync(root, _SnapshotTreeClient())
                self.assertTrue(result["scan_incomplete"])
                self.assertEqual(result["scan_limit_reason"], reason)
                self.assertEqual(result["generated"], 0)
                self.assertFalse(list(Path(root).rglob("*.strm")))

    def test_stop_while_bulk_is_read_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as root, isolated_test_database():
            client = _SnapshotTreeClient()
            self.run_sync(root, client)
            before = {str(p): p.read_bytes() for p in Path(root).rglob("*.strm")}
            stopped = False
            def stop(parent, page):
                nonlocal stopped
                if parent == "*":
                    stopped = True
            client.hook = stop
            result = self.run_sync(root, client, should_stop=lambda: stopped)
            self.assertTrue(result["stopped"])
            self.assertTrue(result["clean_skipped"])
            self.assertEqual(result["generated"], 0)
            self.assertEqual(before, {str(p): p.read_bytes() for p in Path(root).rglob("*.strm")})
