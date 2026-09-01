"""Agent 查询确认世代的跨入口契约。"""
from __future__ import annotations

import unittest

from app.agent.query_lifecycle import (
    begin_query_confirmation_epoch,
    invalidate_query_confirmation_epoch,
)
from app.agent.registry import AgentToolError


class AgentQueryLifecycleTests(unittest.TestCase):
    def test_begin_accepts_positive_integer_generation(self):
        class Service:
            def __init__(self, value):
                self.value = value
                self.owners = []

            def begin_query_confirmation_epoch(self, *, owner: str):
                self.owners.append(owner)
                return self.value

        for value in (1, 3):
            with self.subTest(value=value):
                service = Service(value)
                self.assertEqual(
                    begin_query_confirmation_epoch(service, owner="owner-a"),
                    value,
                )
                self.assertEqual(service.owners, ["owner-a"])

    def test_begin_rejects_missing_or_invalid_lifecycle(self):
        class Service:
            def __init__(self, value):
                self.value = value

            def begin_query_confirmation_epoch(self, *, owner: str):
                return self.value

        for service in (
            Service(-1), Service(0), Service(True), Service("3"), object()
        ):
            with self.subTest(service=service):
                with self.assertRaises(AgentToolError) as raised:
                    begin_query_confirmation_epoch(service, owner="owner-a")
                self.assertEqual(raised.exception.code, "confirmation_unavailable")

    def test_invalidate_is_required_owner_scoped_and_returns_revoked_count(self):
        class Service:
            def __init__(self):
                self.owners = []

            def invalidate_query_confirmation_epoch(self, *, owner: str):
                self.owners.append(owner)
                return 0

        service = Service()
        self.assertEqual(
            invalidate_query_confirmation_epoch(service, owner="owner-b"),
            0,
        )
        self.assertEqual(service.owners, ["owner-b"])

        with self.assertRaises(AgentToolError) as raised:
            invalidate_query_confirmation_epoch(object(), owner="owner-c")
        self.assertEqual(raised.exception.code, "confirmation_unavailable")
