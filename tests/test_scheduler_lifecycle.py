"""所有轻量调度器必须原子发布已启动的线程，不能并发启动两份。"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from app.modules.agent_download_verification_scheduler import (
    DownloadLibraryVerificationScheduler,
)
from app.modules.agent_jobs_scheduler import AgentJobsScheduler
from app.modules.agent_library_patrol_scheduler import AgentLibraryPatrolScheduler
from app.modules.organize_scheduler import OrganizeScheduler


class SchedulerLifecycleTests(unittest.TestCase):
    def test_concurrent_start_publishes_only_one_running_worker(self):
        real_thread = threading.Thread
        factories = (
            AgentJobsScheduler,
            AgentLibraryPatrolScheduler,
            DownloadLibraryVerificationScheduler,
            lambda: OrganizeScheduler(manager=Mock()),
        )
        for factory in factories:
            scheduler = factory()
            with self.subTest(scheduler=type(scheduler).__name__):
                first_start = threading.Event()
                second_created = threading.Event()
                release_start = threading.Event()
                constructed = []
                errors = []

                class Worker:
                    def __init__(self, **kwargs):
                        self.alive = False
                        constructed.append(self)
                        if len(constructed) > 1:
                            second_created.set()

                    def start(self):
                        first_start.set()
                        if not release_start.wait(2):
                            raise TimeoutError("test failed to release worker start")
                        self.alive = True

                    def is_alive(self):
                        return self.alive

                def start():
                    try:
                        scheduler.start()
                    except BaseException as exc:
                        errors.append(exc)

                callers = [real_thread(target=start) for _ in range(2)]
                with patch("threading.Thread", Worker):
                    try:
                        callers[0].start()
                        self.assertTrue(first_start.wait(2))
                        callers[1].start()
                        # 正确实现会在生命周期锁外等待；旧实现可进入第二次构造。
                        second_created.wait(0.1)
                    finally:
                        release_start.set()
                        for caller in callers:
                            if caller.ident is not None:
                                caller.join(2)
                self.assertFalse(errors)
                self.assertFalse(any(caller.is_alive() for caller in callers))
                self.assertEqual(len(constructed), 1)
                self.assertIs(scheduler._thread, constructed[0])
