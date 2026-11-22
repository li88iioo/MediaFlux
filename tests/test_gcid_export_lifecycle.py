"""验证下载回执、过期定时器和服务停止对临时清单的实际清理。"""
from __future__ import annotations

import threading
from tempfile import TemporaryFile
from unittest.mock import Mock, patch

from app.modules.gcid_exports import GCIDExports


def test_late_receipt_or_expiry_cannot_discard_newer_export():
    service = GCIDExports()
    ready = threading.Event()
    client = Mock()
    client.generate_gcid_json.return_value = {"files": []}
    timers = []

    def timer(seconds, fn, args):
        timer = Mock()
        timer.start.side_effect = ready.set
        timers.append(lambda: fn(*args))
        return timer

    try:
        with (
            TemporaryFile() as first, TemporaryFile() as second,
            patch("app.modules.gcid_exports.tempfile.TemporaryFile", side_effect=[first, second]),
            patch("app.modules.gcid_exports.threading.Timer", side_effect=timer),
        ):
            initial = service.start("owner", "0", "根目录", lambda: client)
            assert ready.wait(2)
            _, body = service.download("owner", initial["id"])
            assert body and not first.closed
            service.acknowledge("owner", initial["id"])
            assert first.closed

            ready.clear()
            newer = service.start("owner", "0", "根目录", lambda: client)
            assert ready.wait(2)
            timers[0]()
            service.acknowledge("owner", initial["id"])
            assert service.download("owner", newer["id"])[1] == body
            timers[1]()
            assert second.closed
            assert service.current("owner") is None
    finally:
        service.close()


def test_shutdown_releases_worker_client_and_prevents_publishing_partial_file():
    service = GCIDExports()
    reading, resume, released = threading.Event(), threading.Event(), threading.Event()
    client = Mock()
    client.close.side_effect = released.set

    def generate(source, name, on_progress):
        reading.set()
        assert resume.wait(2)
        on_progress(1, 0)
        return {"files": []}

    client.generate_gcid_json.side_effect = generate
    try:
        service.start("owner", "0", "根目录", lambda: client)
        assert reading.wait(2)
        service.close()
    finally:
        resume.set()
    assert released.wait(2)
    assert service.current("owner") is None
