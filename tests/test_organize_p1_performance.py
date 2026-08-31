from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.media_probe import (
    ProbeBudget,
    _ProbeCancelled,
    _run_ffprobe,
    probe_local_media_profile,
    probe_media_profile,
    probe_media_profiles_batch,
)
from app.modules.organize import MatchResult, OrganizeRules, Organizer
from tests.support import IsolatedDatabaseTestCase, release_parse_result


class _ProbeScraper:
    @staticmethod
    def match(filename: str) -> MatchResult:
        title = "Alpha" if "Alpha" in filename else "Beta"
        return MatchResult(
            tmdb_id="101" if title == "Alpha" else "202",
            title=title,
            year="2026",
            media_type="movie",
            confidence=1.0,
        )

    @staticmethod
    def parse_media(filename: str, parent_path: str = "", match=None):
        return release_parse_result(
            {"season": None, "episode": None, "type": "movie"},
            filename=filename, parent_path=parent_path,
        )

    @staticmethod
    def get_detail(_tmdb_id: str, _media_type: str) -> dict:
        return {"genres": [], "origin_country": ["US"]}


class _ProbeTreeClient:
    def __init__(self, files: list[GuangYaFile]):
        self.files = files
        self.get_download_url = Mock(side_effect=AssertionError("cache hit must not probe"))

    @staticmethod
    def file_info(file_id: str):
        if file_id == "source":
            return GuangYaFile("source", "Source", True)
        return None

    def list_dir(self, file_id: str):
        if file_id == "source":
            return list(self.files)
        return []


class OrganizeP1PerformanceTests(IsolatedDatabaseTestCase):
    def test_cloud_probe_batch_processes_more_than_legacy_24_file_cap_with_bounded_concurrency(self):
        files = [
            GuangYaFile(
                f"batch-{index}", f"Episode.{index:02d}.mkv", False,
                1000 + index, f"etag-{index}", "source",
            )
            for index in range(32)
        ]
        client = SimpleNamespace(
            get_download_url=Mock(
                side_effect=lambda file_id: f"https://example/{file_id}"
            )
        )
        payload = json.dumps({"streams": [
            {
                "codec_type": "video", "codec_name": "hevc",
                "width": 3840, "height": 2160, "avg_frame_rate": "25/1",
                "color_transfer": "bt709", "pix_fmt": "yuv420p10le",
            },
            {"codec_type": "audio", "codec_name": "aac", "channels": 2},
        ]})
        completed = subprocess.CompletedProcess(
            ["ffprobe"], 0, stdout=payload, stderr="",
        )
        state_lock = threading.Lock()
        active = 0
        peak = 0

        def run_probe(*_args, **_kwargs):
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with state_lock:
                active -= 1
            return completed

        budget = ProbeBudget(attempts=len(files) * 2)
        with patch(
            "app.modules.media_probe._run_ffprobe", side_effect=run_probe,
        ), patch(
            "app.modules.media_probe._write_success_cache",
        ):
            profiles = probe_media_profiles_batch(
                files,
                client,
                enabled=True,
                timeout=5,
                prefetched_payloads={},
                cache_prefetched=True,
                budget=budget,
                max_workers=4,
            )

        self.assertEqual(len(profiles), 32)
        self.assertEqual(budget.attempted, 32)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        self.assertTrue(all("10-bit" in profile.render() for profile in profiles.values()))

    def test_cloud_probe_batch_stops_at_total_wall_clock_budget(self):
        files = [
            GuangYaFile(
                f"slow-{index}", f"Slow.{index:02d}.mkv", False,
                1000 + index, f"slow-etag-{index}", "source",
            )
            for index in range(20)
        ]
        client = SimpleNamespace(
            get_download_url=Mock(
                side_effect=lambda file_id: f"https://example/{file_id}"
            )
        )
        budget = ProbeBudget(attempts=len(files) * 2, max_seconds=0.06)

        def timeout_probe(_executable, url, timeout, **_kwargs):
            time.sleep(timeout)
            raise subprocess.TimeoutExpired(["ffprobe", url], timeout)

        started = time.monotonic()
        with patch(
            "app.modules.media_probe._run_ffprobe", side_effect=timeout_probe,
        ) as run_probe, patch(
            "app.modules.media_probe._write_failure_cache",
        ) as failure_cache:
            profiles = probe_media_profiles_batch(
                files,
                client,
                enabled=True,
                timeout=30,
                prefetched_payloads={},
                cache_prefetched=True,
                budget=budget,
                max_workers=4,
            )
        elapsed = time.monotonic() - started

        self.assertEqual(profiles, {})
        self.assertLessEqual(run_probe.call_count, 4)
        self.assertLess(elapsed, 0.5)
        # 负载高时 worker 可能还没领到探测名额预算就已到期，或已触发非阻塞快速返回：
        # 无论走超时路径、预算跳过路径还是时间截止终止，批次都必须被预算终止。
        self.assertTrue(
            budget.remaining_seconds() == 0.0
            or (budget.timeouts + budget.skipped_by_budget >= 1)
        )
        failure_cache.assert_not_called()

    def test_cloud_probe_batch_skips_queued_urls_after_wall_clock_expiry(self):
        files = [
            GuangYaFile(
                f"queued-{index}", f"Queued.{index:02d}.mkv", False,
                1000 + index, f"queued-etag-{index}", "source",
            )
            for index in range(4)
        ]
        first_url_started = threading.Event()
        release_url = threading.Event()

        def get_download_url(file_id):
            first_url_started.set()
            release_url.wait(timeout=1)
            return f"https://example/{file_id}"

        client = SimpleNamespace(get_download_url=Mock(side_effect=get_download_url))
        result_holder = {}
        budget = ProbeBudget(attempts=8, max_seconds=0.03)

        with patch("app.modules.media_probe._run_ffprobe") as run_probe:
            caller = threading.Thread(
                target=lambda: result_holder.setdefault(
                    "profiles",
                    probe_media_profiles_batch(
                        files,
                        client,
                        enabled=True,
                        timeout=30,
                        prefetched_payloads={},
                        cache_prefetched=True,
                        budget=budget,
                        max_workers=4,
                    ),
                )
            )
            caller.start()
            self.assertTrue(first_url_started.wait(timeout=1))
            time.sleep(0.06)
            release_url.set()
            caller.join(timeout=1)

        self.assertFalse(caller.is_alive())
        self.assertEqual(result_holder["profiles"], {})
        self.assertEqual(client.get_download_url.call_count, 1)
        run_probe.assert_not_called()

    def test_cloud_probe_batch_returns_when_active_url_request_exceeds_budget(self):
        files = [
            GuangYaFile(
                f"active-budget-{index}", f"Active.Budget.{index}.mkv", False,
                1000 + index, f"active-budget-etag-{index}", "source",
            )
            for index in range(2)
        ]
        request_started = threading.Event()
        request_finished = threading.Event()
        release_request = threading.Event()
        received_timeouts: list[float] = []

        def get_download_url(file_id, *, timeout=None):
            del file_id
            received_timeouts.append(float(timeout or 0))
            request_started.set()
            try:
                release_request.wait(timeout=1)
                return "https://example/active"
            finally:
                request_finished.set()

        client = SimpleNamespace(get_download_url=Mock(side_effect=get_download_url))
        result_holder = {}
        budget = ProbeBudget(attempts=2, max_seconds=0.05)
        caller = threading.Thread(
            target=lambda: result_holder.setdefault(
                "profiles",
                probe_media_profiles_batch(
                    files, client, enabled=True, timeout=30,
                    prefetched_payloads={}, cache_prefetched=True,
                    budget=budget, max_workers=2,
                ),
            )
        )

        with patch("app.modules.media_probe._run_ffprobe") as run_probe:
            started = time.monotonic()
            caller.start()
            self.assertTrue(request_started.wait(timeout=1))
            caller.join(timeout=0.3)
            elapsed = time.monotonic() - started
            self.assertFalse(caller.is_alive())
            self.assertLess(elapsed, 0.3)
            self.assertEqual(result_holder["profiles"], {})
            self.assertEqual(len(received_timeouts), 1)
            self.assertGreater(received_timeouts[0], 0)
            self.assertLessEqual(received_timeouts[0], 0.06)
            release_request.set()
            self.assertTrue(request_finished.wait(timeout=1))
            run_probe.assert_not_called()

    def test_cloud_probe_reclamps_url_timeout_after_waiting_for_lock(self):
        file = GuangYaFile(
            "lock-budget", "Lock.Budget.mkv", False, 1000,
            "lock-budget-etag", "source",
        )
        received_timeouts: list[float] = []

        def get_download_url(_file_id, *, timeout=None):
            received_timeouts.append(float(timeout or 0))
            return ""

        client = SimpleNamespace(get_download_url=Mock(side_effect=get_download_url))
        download_url_lock = threading.Lock()
        download_url_lock.acquire()
        budget_seconds = 0.3
        result_holder = {}
        caller = threading.Thread(
            target=lambda: result_holder.setdefault(
                "profile",
                probe_media_profile(
                    file,
                    client,
                    enabled=True,
                    timeout=30,
                    prefetched_payload="",
                    cache_prefetched=True,
                    budget=ProbeBudget(attempts=2, max_seconds=budget_seconds),
                    download_url_lock=download_url_lock,
                ),
            )
        )
        caller.start()
        time.sleep(0.12)
        download_url_lock.release()
        caller.join(timeout=1)

        self.assertFalse(caller.is_alive())
        self.assertIsNone(result_holder["profile"])
        self.assertEqual(len(received_timeouts), 1)
        self.assertGreater(received_timeouts[0], 0)
        self.assertLess(received_timeouts[0], budget_seconds - 0.07)

    def test_cloud_probe_classifies_download_url_transport_timeout(self):
        file = GuangYaFile(
            "url-timeout", "URL.Timeout.mkv", False, 1000,
            "url-timeout-etag", "source",
        )
        received: list[tuple[float, bool]] = []

        def get_download_url(_file_id, *, timeout=None, raise_timeout=False):
            received.append((float(timeout or 0), bool(raise_timeout)))
            raise TimeoutError("signed URL transport timeout")

        client = SimpleNamespace(get_download_url=Mock(side_effect=get_download_url))
        budget = ProbeBudget(attempts=1, max_seconds=5)
        with patch("app.modules.media_probe._run_ffprobe") as run_probe, patch(
            "app.modules.media_probe._write_failure_cache",
        ) as failure_cache:
            profile = probe_media_profile(
                file,
                client,
                enabled=True,
                timeout=5,
                prefetched_payload="",
                cache_prefetched=True,
                budget=budget,
            )

        self.assertIsNone(profile)
        self.assertEqual(len(received), 1)
        self.assertGreater(received[0][0], 0)
        self.assertTrue(received[0][1])
        self.assertEqual(budget.timeouts, 1)
        run_probe.assert_not_called()
        failure_cache.assert_called_once_with(file, "timeout", ttl_seconds=600)

    def test_cloud_probe_lock_wait_exits_before_holder_releases_on_cancel(self):
        file = GuangYaFile(
            "cancel-wait", "Cancel.Wait.mkv", False, 1000, "cancel-wait-etag", "source"
        )
        client = SimpleNamespace(get_download_url=Mock(return_value="https://example/file"))
        cancel_event = threading.Event()
        download_url_lock = threading.Lock()
        download_url_lock.acquire()
        result_holder = {}
        caller = threading.Thread(
            target=lambda: result_holder.setdefault(
                "profile",
                probe_media_profile(
                    file,
                    client,
                    enabled=True,
                    timeout=30,
                    prefetched_payload="",
                    cache_prefetched=True,
                    budget=ProbeBudget(attempts=2, max_seconds=10),
                    download_url_lock=download_url_lock,
                    cancel_event=cancel_event,
                ),
            )
        )
        try:
            caller.start()
            time.sleep(0.06)
            cancel_event.set()
            caller.join(timeout=0.25)
            self.assertFalse(caller.is_alive())
        finally:
            download_url_lock.release()
            caller.join(timeout=1)

        self.assertIsNone(result_holder["profile"])
        client.get_download_url.assert_not_called()

    def test_cloud_probe_batch_cancel_stops_starting_new_files(self):
        files = [
            GuangYaFile(
                f"cancel-{index}", f"Cancel.{index:02d}.mkv", False,
                1000 + index, f"cancel-etag-{index}", "source",
            )
            for index in range(20)
        ]
        cancel_event = threading.Event()

        def get_download_url(file_id):
            cancel_event.set()
            return f"https://example/{file_id}"

        client = SimpleNamespace(get_download_url=Mock(side_effect=get_download_url))
        with patch("app.modules.media_probe._run_ffprobe") as run_probe:
            profiles = probe_media_profiles_batch(
                files,
                client,
                enabled=True,
                timeout=30,
                prefetched_payloads={},
                cache_prefetched=True,
                budget=ProbeBudget(attempts=len(files) * 2, max_seconds=10),
                max_workers=4,
                cancel_event=cancel_event,
            )

        self.assertEqual(profiles, {})
        self.assertEqual(client.get_download_url.call_count, 1)
        run_probe.assert_not_called()

    def test_ffprobe_slots_are_shared_across_independent_callers(self):
        state_lock = threading.Lock()
        release = threading.Event()
        two_running = threading.Event()
        active = 0
        peak = 0
        results: list[subprocess.CompletedProcess] = []
        errors: list[BaseException] = []

        def run_process(command, **_kwargs):
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    two_running.set()
            release.wait(timeout=2)
            with state_lock:
                active -= 1
            return subprocess.CompletedProcess(command, 0, stdout='{"streams": []}', stderr="")

        def invoke(index: int) -> None:
            try:
                results.append(_run_ffprobe("ffprobe", f"file-{index}", 2))
            except BaseException as exc:  # pragma: no cover - 失败内容由主线程断言
                errors.append(exc)

        threads = [threading.Thread(target=invoke, args=(index,)) for index in range(5)]
        try:
            with patch.dict(
                "app.modules.media_probe.os.environ",
                {"MEDIAFLUX_MEDIA_PROBE_WORKERS": "2"},
            ), patch("app.modules.media_probe.subprocess.run", side_effect=run_process):
                for thread in threads:
                    thread.start()
                self.assertTrue(two_running.wait(timeout=1))
                time.sleep(0.05)
                self.assertEqual(peak, 2)
                release.set()
                for thread in threads:
                    thread.join(timeout=2)
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(peak, 2)

    def test_ffprobe_slot_wait_does_not_consume_process_timeout(self):
        first_running = threading.Event()
        release_first = threading.Event()
        results: dict[str, subprocess.CompletedProcess] = {}
        errors: list[BaseException] = []
        received_timeouts: dict[str, float] = {}

        def run_process(command, **kwargs):
            url = str(command[-1])
            received_timeouts[url] = float(kwargs.get("timeout") or 0)
            if url == "first":
                first_running.set()
                release_first.wait(timeout=2)
            return subprocess.CompletedProcess(
                command, 0, stdout='{"streams": []}', stderr="",
            )

        def invoke(name: str, timeout: float) -> None:
            try:
                results[name] = _run_ffprobe("ffprobe", name, timeout)
            except BaseException as exc:  # pragma: no cover - 主线程统一断言
                errors.append(exc)

        first = threading.Thread(target=invoke, args=("first", 1.0))
        second = threading.Thread(target=invoke, args=("second", 0.1))
        try:
            with patch.dict(
                "app.modules.media_probe.os.environ",
                {"MEDIAFLUX_MEDIA_PROBE_WORKERS": "1"},
            ), patch("app.modules.media_probe.subprocess.run", side_effect=run_process):
                first.start()
                self.assertTrue(first_running.wait(timeout=1))
                second.start()
                # 第二个探测排队时间故意超过它的执行超时。执行超时应从真正
                # 获得槽位后开始，而不是在全局并发队列中被提前耗尽。
                time.sleep(0.15)
                self.assertTrue(second.is_alive())
                release_first.set()
                first.join(timeout=2)
                second.join(timeout=2)
        finally:
            release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(set(results), {"first", "second"})
        self.assertAlmostEqual(received_timeouts["second"], 0.1, places=3)

    def test_waiting_ffprobe_slot_observes_cancellation(self):
        release = threading.Event()
        first_running = threading.Event()
        cancel_event = threading.Event()
        first_error: list[BaseException] = []

        def run_process(command, **_kwargs):
            first_running.set()
            release.wait(timeout=2)
            return subprocess.CompletedProcess(command, 0, stdout='{"streams": []}', stderr="")

        def occupy() -> None:
            try:
                _run_ffprobe("ffprobe", "first", 2)
            except BaseException as exc:  # pragma: no cover - 失败内容由主线程断言
                first_error.append(exc)

        thread = threading.Thread(target=occupy)
        try:
            with patch.dict(
                "app.modules.media_probe.os.environ",
                {"MEDIAFLUX_MEDIA_PROBE_WORKERS": "1"},
            ), patch("app.modules.media_probe.subprocess.run", side_effect=run_process):
                thread.start()
                self.assertTrue(first_running.wait(timeout=1))
                cancel_event.set()
                with self.assertRaises(_ProbeCancelled):
                    _run_ffprobe(
                        "ffprobe", "second", 1, cancel_event=cancel_event,
                    )
        finally:
            release.set()
            thread.join(timeout=2)

        self.assertEqual(first_error, [])
        self.assertFalse(thread.is_alive())

    def test_running_ffprobe_is_terminated_when_batch_is_cancelled(self):
        cancel_event = threading.Event()

        class RunningProcess:
            def __init__(self):
                self.returncode = None
                self.terminated = False
                self.killed = False

            def communicate(self, timeout=None):
                if self.terminated or self.killed:
                    return "", ""
                time.sleep(min(float(timeout or 0.01), 0.02))
                raise subprocess.TimeoutExpired(["ffprobe"], timeout or 0.01)

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def kill(self):
                self.killed = True
                self.returncode = -9

        process = RunningProcess()
        timer = threading.Timer(0.04, cancel_event.set)
        started = time.monotonic()
        timer.start()
        try:
            with patch("app.modules.media_probe.subprocess.Popen", return_value=process):
                with self.assertRaises(_ProbeCancelled):
                    _run_ffprobe(
                        "ffprobe", "https://example/slow", 5,
                        cancel_event=cancel_event,
                    )
        finally:
            timer.join(timeout=1)

        self.assertTrue(process.terminated)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_probe_cache_prune_removes_expired_and_caps_oldest_rows(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_probe_cache")
            conn.executemany(
                "INSERT INTO media_probe_cache("
                "file_id,etag,size,payload,updated_at"
                ") VALUES(?,?,?,?,?)",
                [
                    ("expired", "e1", 1, "{}", "2026-01-01 00:00:00"),
                    ("old-live", "e2", 2, "{}", "2026-06-01 00:00:00"),
                    ("new-live", "e3", 3, "{}", "2026-07-01 00:00:00"),
                ],
            )

        deleted = db.prune_media_probe_cache(
            expired_before="2026-05-01 00:00:00",
            max_rows=1,
            batch_size=10,
        )

        self.assertEqual(deleted, 2)
        with db.get_conn() as conn:
            remaining = [
                row["file_id"]
                for row in conn.execute(
                    "SELECT file_id FROM media_probe_cache ORDER BY file_id"
                )
            ]
        self.assertEqual(remaining, ["new-live"])

    def test_batch_probe_cache_uses_one_connection_and_strict_versions(self):
        db.upsert_media_probe_cache("one", "etag-1", 100, '{"resolution":"1080p"}')
        db.upsert_media_probe_cache("two", "etag-2", 200, '{"resolution":"2160p"}')
        original_get_conn = db.get_conn

        with patch.object(db, "get_conn", wraps=original_get_conn) as get_conn:
            cached = db.get_media_probe_cache_many([
                ("one", "etag-1", 100),
                ("two", "wrong-etag", 200),
                ("missing", "etag", 1),
            ])

        self.assertEqual(get_conn.call_count, 1)
        self.assertEqual(set(cached), {("one", "etag-1", 100)})
        self.assertNotIn(("two", "wrong-etag", 200), cached)

    def test_cloud_probe_cache_reuses_success_by_content_fingerprint(self):
        payload = json.dumps({"resolution": "2160p", "video_codec": "H.265"})
        db.upsert_media_probe_cache("old-id", "etag-shared", 4096, payload)

        self.assertEqual(
            db.get_media_probe_cache(
                "new-id", "etag-shared", 4096, allow_fingerprint_fallback=True,
            ),
            payload,
        )
        self.assertEqual(
            db.get_media_probe_cache_many(
                [("new-id", "etag-shared", 4096)],
                allow_fingerprint_fallback=True,
            ),
            {("new-id", "etag-shared", 4096): payload},
        )

    def test_cloud_probe_fingerprint_lookup_uses_compound_index(self):
        payloads = {
            ("etag-one", 4096): json.dumps({"resolution": "2160p"}),
            ("etag-two", 8192): json.dumps({"resolution": "1080p"}),
        }
        for index, ((etag, size), payload) in enumerate(payloads.items(), start=1):
            db.upsert_media_probe_cache(f"success-{index}", etag, size, payload)
            db.upsert_media_probe_cache(
                f"zz-failure-{index}", etag, size,
                json.dumps({"_media_probe_cache": "failure"}),
            )

        versions = [
            (f"new-{index}", etag, size)
            for index, (etag, size) in enumerate(payloads, start=1)
        ]
        cached = db.get_media_probe_cache_many(
            versions, allow_fingerprint_fallback=True,
        )
        self.assertEqual(
            set(cached.values()),
            set(payloads.values()),
        )

        with db.get_conn() as conn:
            for etag, size in payloads:
                plan = conn.execute(
                    "EXPLAIN QUERY PLAN "
                    "SELECT payload FROM media_probe_cache WHERE etag=? AND size=? "
                    "ORDER BY updated_at DESC, file_id DESC",
                    (etag, size),
                ).fetchall()
                details = " ".join(str(row["detail"] or "") for row in plan)
                self.assertIn("idx_media_probe_cache_fingerprint_updated", details)
                self.assertNotIn("USE TEMP B-TREE", details.upper())

    def test_cloud_success_fingerprint_overrides_exact_transient_failure(self):
        failure = json.dumps({
            "_media_probe_cache": "failure",
            "retry_after_epoch": 9999999999,
            "reason": "timeout",
        })
        success = json.dumps({"resolution": "2160p", "video_codec": "H.265"})
        db.upsert_media_probe_cache("failed-id", "etag-shared", 4096, failure)
        db.upsert_media_probe_cache("healthy-id", "etag-shared", 4096, success)

        self.assertEqual(
            db.get_media_probe_cache(
                "failed-id", "etag-shared", 4096, allow_fingerprint_fallback=True,
            ),
            success,
        )
        self.assertEqual(
            db.get_media_probe_cache("failed-id", "etag-shared", 4096),
            failure,
        )
        self.assertEqual(
            db.get_media_probe_cache_many(
                [("failed-id", "etag-shared", 4096)],
                allow_fingerprint_fallback=True,
            ),
            {("failed-id", "etag-shared", 4096): success},
        )

    def test_cloud_probe_failure_cache_never_spreads_to_another_file(self):
        failure = json.dumps({
            "_media_probe_cache": "failure",
            "retry_after_epoch": 9999999999,
            "reason": "timeout",
        })
        db.upsert_media_probe_cache("failed-id", "etag-failure-only", 8192, failure)

        self.assertEqual(
            db.get_media_probe_cache(
                "new-id", "etag-failure-only", 8192, allow_fingerprint_fallback=True,
            ),
            "",
        )
        self.assertEqual(
            db.get_media_probe_cache_many(
                [("new-id", "etag-failure-only", 8192)],
                allow_fingerprint_fallback=True,
            ),
            {},
        )

    def test_late_failure_cannot_replace_same_version_success_cache(self):
        success = json.dumps({"resolution": "2160p", "video_codec": "H.265"})
        failure = json.dumps({
            "_media_probe_cache": "failure",
            "retry_after_epoch": 9999999999,
            "reason": "timeout",
        })
        db.upsert_media_probe_cache("same-id", "same-etag", 4096, success)

        written = db.upsert_media_probe_failure_cache(
            "same-id", "same-etag", 4096, failure,
        )

        self.assertFalse(written)
        self.assertEqual(
            db.get_media_probe_cache("same-id", "same-etag", 4096),
            success,
        )

    def test_local_probe_budget_exhaustion_skips_ffprobe_without_negative_cache(self):
        budget = ProbeBudget(attempts=0, max_seconds=20)
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "budget.mkv"
            media.write_bytes(b"media")
            with (
                patch("app.modules.media_probe._run_ffprobe") as run,
                patch.object(db, "upsert_media_probe_failure_cache") as write_failure,
            ):
                profile = probe_local_media_profile(
                    media,
                    size=5,
                    mtime_ns=987654321,
                    device=99,
                    inode=999,
                    budget=budget,
                )

        self.assertIsNone(profile)
        self.assertEqual(budget.skipped_by_budget, 1)
        run.assert_not_called()
        write_failure.assert_not_called()

    def test_probe_budget_clamps_timeout_to_remaining_wall_clock(self):
        with patch("app.modules.media_probe.time.monotonic", side_effect=[100.0, 103.5]):
            budget = ProbeBudget(attempts=1, max_seconds=5)
            timeout = budget.clamp_timeout(30)

        self.assertAlmostEqual(timeout, 1.5)

    def test_local_probe_cache_is_exact_and_avoids_repeated_ffprobe(self):
        payload = json.dumps({
            "streams": [
                {
                    "codec_type": "video", "codec_name": "hevc",
                    "width": 1920, "height": 1080, "avg_frame_rate": "24/1",
                    "color_transfer": "bt709",
                },
                {"codec_type": "audio", "codec_name": "aac", "channels": 2},
            ],
        })
        completed = subprocess.CompletedProcess(
            ["ffprobe"], 0, stdout=payload, stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.mkv"
            second = Path(tmp) / "second.mkv"
            first.write_bytes(b"media")
            second.write_bytes(b"media")
            with patch(
                "app.modules.media_probe._run_ffprobe", return_value=completed,
            ) as run:
                first_profile = probe_local_media_profile(
                    first, size=5, mtime_ns=123, device=1, inode=11,
                )
                cached_profile = probe_local_media_profile(
                    first, size=5, mtime_ns=123, device=1, inode=11,
                )
                second_profile = probe_local_media_profile(
                    second, size=5, mtime_ns=123, device=1, inode=22,
                )

        self.assertEqual(first_profile.render(), "1080p.SDR.H.265.24fps.AAC.2.0")
        self.assertEqual(cached_profile, first_profile)
        self.assertEqual(second_profile, first_profile)
        self.assertEqual(run.call_count, 2)

    def test_organize_prefetches_source_probe_cache_without_per_file_queries(self):
        files = [
            GuangYaFile("alpha", "Alpha.2026.mkv", False, 100, "etag-a", "source"),
            GuangYaFile("beta", "Beta.2026.mkv", False, 200, "etag-b", "source"),
        ]
        payload = json.dumps({
            "resolution": "1080p",
            "dynamic_range": "SDR",
            "video_codec": "H.265",
            "audio_codec": "AAC",
        })
        db.upsert_media_probe_cache("alpha", "etag-a", 100, payload)
        db.upsert_media_probe_cache("beta", "etag-b", 200, payload)
        client = _ProbeTreeClient(files)
        organizer = Organizer(client=client, scraper=_ProbeScraper())
        rules = OrganizeRules(
            target_dir_id="target",
            region_split=False,
            year_split=False,
            small_file_mb=0,
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
            library_notify=False,
            media_info_enabled=True,
            media_probe_enabled=True,
        )

        with patch(
            "app.modules.media_probe.db.get_media_probe_cache",
            side_effect=AssertionError("unexpected per-file cache lookup"),
        ) as single_lookup:
            plans, stats = organizer.organize(
                "source", rules, dry_run=True, post_actions=False
            )

        self.assertEqual(len(plans), 2)
        self.assertEqual(stats["scan_list_dir_calls"], 1)
        self.assertEqual(stats["scan_file_info_calls"], 1)
        self.assertEqual(stats["media_probe_cache_batches"], 1)
        self.assertEqual(stats["media_probe_cache_hits"], 2)
        single_lookup.assert_not_called()
        client.get_download_url.assert_not_called()
        self.assertTrue(all("1080p" in plan.new_name for plan in plans))

    def test_target_variant_cache_is_batch_primed_and_reused(self):
        payload = json.dumps({
            "resolution": "2160p",
            "dynamic_range": "SDR",
            "video_codec": "H.265",
            "audio_codec": "AAC",
        })
        db.upsert_media_probe_cache("one", "etag-1", 100, payload)
        db.upsert_media_probe_cache("two", "etag-2", 200, payload)
        files = [
            GuangYaFile("one", "One.mkv", False, 100, "etag-1", "target"),
            GuangYaFile("two", "Two.mkv", False, 200, "etag-2", "target"),
        ]
        organizer = Organizer(client=Mock(), scraper=Mock())
        rules = OrganizeRules(media_probe_enabled=True)

        with patch(
            "app.modules.organize.get_media_probe_cache",
            side_effect=AssertionError("unexpected per-file target cache lookup"),
        ) as single_lookup:
            batches, hits = organizer._prime_existing_variant_cache(files, rules)
            variants = [organizer._existing_variant(file, rules) for file in files]

        self.assertEqual((batches, hits), (1, 2))
        single_lookup.assert_not_called()
        self.assertTrue(all(variant.dolby_vision is False for variant in variants))
        self.assertTrue(all(variant.atmos is False for variant in variants))
        self.assertTrue(all(variant.remux is False for variant in variants))


if __name__ == "__main__":
    unittest.main()


class OrganizeTaskRuntimeRecognitionTests(unittest.TestCase):
    def test_cacheable_recognition_is_single_flight_and_deep_copied(self):
        from app.modules.organize_runtime import OrganizeTaskRuntime

        runtime = OrganizeTaskRuntime()
        calls = 0
        lock = threading.Lock()
        results: list[tuple[dict, bool, float, bool]] = []

        def loader():
            nonlocal calls
            with lock:
                calls += 1
            time.sleep(0.05)
            return {"identity": ["tmdb", "223911"]}

        def run():
            results.append(runtime.resolve_recognition(
                ("series", "仙逆", "2023"),
                loader,
                cacheable=lambda _value: True,
                neutralize=lambda value: value,
            ))

        threads = [threading.Thread(target=run) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(calls, 1)
        self.assertEqual(sum(1 for _value, hit, _wait, _bound in results if hit), 2)
        self.assertEqual(sum(1 for _value, _hit, _wait, bound in results if bound), 1)
        self.assertTrue(any(wait > 0 for _value, _hit, wait, _bound in results))
        results[0][0]["identity"].append("mutated")
        self.assertEqual(results[1][0]["identity"], ["tmdb", "223911"])

    def test_uncacheable_followers_resume_in_parallel(self):
        from app.modules.organize_runtime import OrganizeTaskRuntime

        runtime = OrganizeTaskRuntime()
        owner_started = threading.Event()
        release_owner = threading.Event()
        followers_started = threading.Event()
        release_followers = threading.Event()
        calls = 0
        followers = 0
        lock = threading.Lock()
        failures: list[BaseException] = []

        def loader():
            nonlocal calls, followers
            with lock:
                calls += 1
                index = calls
            if index == 1:
                owner_started.set()
                release_owner.wait(timeout=2)
            else:
                with lock:
                    followers += 1
                    if followers >= 2:
                        followers_started.set()
                release_followers.wait(timeout=2)
            return {"cacheable": False}

        def run():
            try:
                runtime.resolve_recognition(
                    ("ambiguous", "same-input"),
                    loader,
                    cacheable=lambda _value: False,
                    neutralize=lambda value: value,
                )
            except BaseException as exc:  # pragma: no cover - 便于线程失败回传
                failures.append(exc)

        first = threading.Thread(target=run)
        first.start()
        self.assertTrue(owner_started.wait(timeout=1))
        waiting = [threading.Thread(target=run) for _ in range(2)]
        for thread in waiting:
            thread.start()
        try:
            release_owner.set()
            self.assertTrue(followers_started.wait(timeout=1))
        finally:
            release_followers.set()
            release_owner.set()
        first.join(timeout=2)
        for thread in waiting:
            thread.join(timeout=2)

        self.assertFalse(failures)
        self.assertEqual(calls, 3)
        self.assertTrue(all(not thread.is_alive() for thread in [first, *waiting]))

    def test_resolve_plan_match_reuses_task_runtime_only_after_strict_binding(self):
        from app.modules.organize_runtime import OrganizeTaskRuntime

        class _CountingScraper:
            def __init__(self):
                self.calls = 0

            def match(self, _filename):
                self.calls += 1
                return MatchResult(
                    tmdb_id="223911",
                    external_id="223911",
                    title="仙逆",
                    year="2023",
                    media_type="tv",
                    confidence=1.0,
                    status="matched",
                    matched_by="search",
                    provider="tmdb",
                )

        scraper = _CountingScraper()
        organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
        runtime = OrganizeTaskRuntime()
        stats = organizer._initial_stats()
        file = GuangYaFile(
            "episode-94", "Renegade.Immortal.S01E094.2023.mkv", False,
            1000, "etag", "source",
        )
        kwargs = {
            "match_name": file.name,
            "parent_path": "仙逆/第94集",
            "recognition_media_type_hint": "tv",
            "match_override": None,
            "recognition_work_cache": None,
            "recognition_work_cache_key": None,
            "task_runtime": runtime,
            "task_recognition_key": ("tmdb-tv", "renegadeimmortal", "2023"),
            "stats": stats,
        }
        with patch.object(
            Organizer, "_cacheable_task_recognition_match", return_value=True,
        ):
            first = organizer._resolve_plan_match(file, OrganizeRules(), **kwargs)
            second = organizer._resolve_plan_match(file, OrganizeRules(), **kwargs)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(scraper.calls, 1)
        self.assertEqual(stats["task_recognition_cache_bindings"], 1)
        self.assertEqual(stats["task_recognition_cache_hits"], 1)

    def test_task_identity_key_ignores_episode_but_keeps_title_and_year(self):
        from app.modules.scraper import TMDBScraper

        scraper = TMDBScraper(client=SimpleNamespace(api_key="", base_url=""))
        organizer = Organizer(client=SimpleNamespace(), scraper=scraper)
        rules = OrganizeRules()
        common = {
            "parent_path": "光鸭/仙逆",
            "media_type_hint": "",
            "rules": rules,
            "automatic": True,
            "trusted_match_override": None,
        }
        first = organizer._task_recognition_cache_key(
            recognition_name=(
                "Renegade.Immortal.S01E094.2023.2160p.WEB-DL.mkv"
            ),
            **common,
        )
        second = organizer._task_recognition_cache_key(
            recognition_name=(
                "Renegade.Immortal.S01E099.2023.2160p.WEB-DL.mkv"
            ),
            **common,
        )
        different_year = organizer._task_recognition_cache_key(
            recognition_name=(
                "Renegade.Immortal.S01E099.2024.2160p.WEB-DL.mkv"
            ),
            **common,
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, different_year)
