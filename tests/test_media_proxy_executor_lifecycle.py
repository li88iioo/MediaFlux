from __future__ import annotations

import threading
import unittest

from app.modules import media_proxy


class SignedMediaProbeExecutorLifecycleTests(unittest.TestCase):
    def tearDown(self) -> None:
        media_proxy.shutdown_signed_media_probe_runtime(1.0)
        media_proxy.start_signed_media_probe_runtime()

    def test_shutdown_stops_admission_and_allows_explicit_restart(self):
        media_proxy.start_signed_media_probe_runtime()
        started = threading.Event()
        release = threading.Event()

        def blocking_probe() -> str:
            started.set()
            release.wait(1.0)
            return "done"

        future = media_proxy._signed_media_probe_runtime.submit(blocking_probe)
        self.assertTrue(started.wait(0.5))
        self.assertFalse(media_proxy.shutdown_signed_media_probe_runtime(0.01))
        with self.assertRaises(media_proxy._SignedMediaProbeCapacityError):
            media_proxy._signed_media_probe_runtime.submit(lambda: None)

        release.set()
        self.assertEqual(future.result(timeout=1.0), "done")

        media_proxy.start_signed_media_probe_runtime()
        restarted = media_proxy._signed_media_probe_runtime.submit(lambda: "restarted")
        self.assertEqual(restarted.result(timeout=1.0), "restarted")
        self.assertTrue(media_proxy.shutdown_signed_media_probe_runtime(1.0))
