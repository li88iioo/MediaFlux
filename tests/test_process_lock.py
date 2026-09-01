from __future__ import annotations

import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.modules.process_lock import CrossProcessLock
from app.modules.qb_control import qb_control_write_lease


def _try_lock(directory: str, queue) -> None:
    lock = CrossProcessLock("shared", directory=directory)
    acquired = lock.acquire(blocking=False)
    queue.put(acquired)
    if acquired:
        lock.release()


class CrossProcessLockTests(unittest.TestCase):
    def test_nonblocking_lock_rejects_same_process_contender(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = CrossProcessLock("shared", directory=temp_dir)
            second = CrossProcessLock("shared", directory=temp_dir)
            self.assertTrue(first.acquire(blocking=False))
            try:
                self.assertFalse(second.acquire(blocking=False))
            finally:
                first.release()
            self.assertTrue(second.acquire(blocking=False))
            second.release()

    def test_qb_control_lease_blocks_same_cross_process_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shared = CrossProcessLock("qb-control-write", directory=temp_dir)
            contender = CrossProcessLock("qb-control-write", directory=temp_dir)
            with patch("app.modules.qb_control._QB_CONTROL_WRITE_LOCK", shared):
                with qb_control_write_lease():
                    self.assertFalse(contender.acquire(blocking=False))
                self.assertTrue(contender.acquire(blocking=False))
                contender.release()

    def test_lock_can_be_released_by_worker_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock = CrossProcessLock("shared", directory=temp_dir)
            self.assertTrue(lock.acquire(blocking=False))
            worker = threading.Thread(target=lock.release)
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()

    def test_interrupt_during_file_lock_does_not_poison_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock = CrossProcessLock("shared", directory=temp_dir)
            with patch.object(
                lock, "_acquire_file_lock", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    lock.acquire(blocking=False)

            self.assertTrue(lock.acquire(blocking=False))
            lock.release()

    def test_nonblocking_lock_rejects_other_process_contender(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock = CrossProcessLock("shared", directory=Path(temp_dir))
            self.assertTrue(lock.acquire(blocking=False))
            ctx = multiprocessing.get_context("spawn")
            queue = ctx.Queue()
            process = ctx.Process(target=_try_lock, args=(temp_dir, queue))
            process.start()
            process.join(timeout=10)
            try:
                self.assertEqual(process.exitcode, 0)
                self.assertFalse(queue.get(timeout=2))
            finally:
                lock.release()
