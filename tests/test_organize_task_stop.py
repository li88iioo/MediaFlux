"""光鸭整理任务协作式停止的原子前置条件测试。"""
from __future__ import annotations

import unittest

from app.modules.organize_tasks import OrganizeTaskManager


class OrganizeTaskStopTests(unittest.TestCase):
    @staticmethod
    def _manager(*, task_id: str = "task-a", status: str = "running", stoppable: bool = True):
        manager = OrganizeTaskManager()
        manager._task = {
            "id": task_id,
            "status": status,
            "stoppable": stoppable,
            "message": "running",
        }
        return manager

    def test_expected_task_id_and_running_state_are_checked_atomically(self):
        manager = self._manager()

        wrong = manager.stop(expected_task_id="task-b", require_running=True)
        self.assertFalse(wrong["ok"])
        self.assertEqual(manager._task["status"], "running")
        self.assertFalse(manager._cancel_event.is_set())

        stopped = manager.stop(expected_task_id="task-a", require_running=True)
        self.assertTrue(stopped["ok"])
        self.assertEqual(manager._task["status"], "stopping")
        self.assertTrue(manager._cancel_event.is_set())

    def test_confirmed_stop_rejects_already_stopping_or_atomic_stage(self):
        stopping = self._manager(status="stopping")
        self.assertFalse(
            stopping.stop(expected_task_id="task-a", require_running=True)["ok"]
        )

        atomic = self._manager(stoppable=False)
        result = atomic.stop(expected_task_id="task-a", require_running=True)
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "当前纠偏操作处于不可中断的原子写入阶段",
        )
        self.assertEqual(atomic._task["status"], "running")
        self.assertFalse(atomic._cancel_event.is_set())

    def test_existing_web_stop_semantics_remain_backward_compatible(self):
        manager = self._manager(status="stopping")
        result = manager.stop()
        self.assertTrue(result["ok"])
        self.assertTrue(manager._cancel_event.is_set())


if __name__ == "__main__":
    unittest.main()
