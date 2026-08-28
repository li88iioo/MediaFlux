from __future__ import annotations

import asyncio
import threading
import time
import unittest

from app.indexers.concurrency import CrossLoopAsyncLock
from app.indexers.providers.base import SearchRequestPacer


class CrossLoopAsyncLockTests(unittest.TestCase):
    def test_serializes_callers_from_different_event_loops(self):
        lock = CrossLoopAsyncLock(poll_interval=0.001)
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        errors: list[BaseException] = []

        async def first_worker() -> None:
            async with lock:
                first_entered.set()
                while not release_first.is_set():
                    await asyncio.sleep(0.001)

        async def second_worker() -> None:
            async with lock:
                second_entered.set()

        def run(coroutine) -> None:
            try:
                asyncio.run(coroutine(), debug=True)
            except BaseException as exc:  # pragma: no cover - 仅用于线程错误回传
                errors.append(exc)

        first = threading.Thread(target=run, args=(first_worker,))
        second = threading.Thread(target=run, args=(second_worker,))
        first.start()
        self.assertTrue(first_entered.wait(timeout=1))
        second.start()
        time.sleep(0.03)
        self.assertFalse(second_entered.is_set())

        release_first.set()
        first.join(timeout=1)
        second.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())
        self.assertEqual(errors, [])

    def test_shared_search_pacer_is_safe_across_event_loops(self):
        pacer = SearchRequestPacer(interval_seconds=0.04)
        pacer._last_started = time.monotonic()
        start = threading.Barrier(2)
        errors: list[BaseException] = []

        async def wait_for_slot() -> None:
            await pacer.wait()

        def run() -> None:
            try:
                start.wait(timeout=1)
                asyncio.run(wait_for_slot(), debug=True)
            except BaseException as exc:  # pragma: no cover - 仅用于线程错误回传
                errors.append(exc)

        workers = [threading.Thread(target=run) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=1)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
