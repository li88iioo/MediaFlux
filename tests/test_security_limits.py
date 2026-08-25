"""登录与初始化限流状态容量边界测试。"""
from __future__ import annotations

import unittest
from collections import deque
from unittest.mock import patch

from app import security


class SecurityRateLimitCapacityTests(unittest.TestCase):
    def test_failure_identity_map_prunes_expired_entries_before_eviction(self) -> None:
        attempts = {
            "expired": deque([1.0]),
            "active": deque([99.0]),
        }
        with patch.object(security, "_RATE_LIMIT_MAX_IDENTITIES", 2):
            security._record_failure(
                attempts,
                "new",
                now=100.0,
                window_seconds=10.0,
            )

        self.assertNotIn("expired", attempts)
        self.assertEqual(set(attempts), {"active", "new"})

    def test_failure_identity_map_evicts_oldest_active_identity_at_capacity(self) -> None:
        attempts = {
            "oldest": deque([91.0]),
            "newer": deque([95.0]),
        }
        with patch.object(security, "_RATE_LIMIT_MAX_IDENTITIES", 2):
            security._record_failure(
                attempts,
                "latest",
                now=100.0,
                window_seconds=30.0,
            )

        self.assertNotIn("oldest", attempts)
        self.assertEqual(set(attempts), {"newer", "latest"})
        self.assertEqual(list(attempts["latest"]), [100.0])


if __name__ == "__main__":
    unittest.main()
