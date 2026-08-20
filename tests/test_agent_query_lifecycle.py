"""Agent 查询确认世代的跨入口兼容边界。"""
from __future__ import annotations

import unittest

from app.agent.query_lifecycle import (
    begin_query_confirmation_epoch,
    invalidate_query_confirmation_epoch,
)


class AgentQueryLifecycleTests(unittest.TestCase):
    def test_begin_accepts_only_non_negative_integer_generation(self):
        class Service:
            def __init__(self, value):
                self.value = value
                self.owners = []

            def begin_query_confirmation_epoch(self, *, owner: str):
                self.owners.append(owner)
                return self.value

        for value, expected in ((0, 0), (3, 3), (-1, None), (True, None), ("3", None)):
            with self.subTest(value=value):
                service = Service(value)
                self.assertEqual(
                    begin_query_confirmation_epoch(service, owner="owner-a"),
                    expected,
                )
                self.assertEqual(service.owners, ["owner-a"])

        self.assertIsNone(
            begin_query_confirmation_epoch(object(), owner="owner-a")
        )

    def test_invalidate_is_optional_and_owner_scoped(self):
        class Service:
            def __init__(self):
                self.owners = []

            def invalidate_query_confirmation_epoch(self, *, owner: str):
                self.owners.append(owner)

        service = Service()
        invalidate_query_confirmation_epoch(service, owner="owner-b")
        invalidate_query_confirmation_epoch(object(), owner="owner-c")
        self.assertEqual(service.owners, ["owner-b"])
