from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.agent.async_bridge import AsyncBridgeUnavailable, run_awaitable_sync
from app.agent.feature_gate import (
    agent_runtime_generation_is_current,
    current_agent_runtime_generation,
    invalidate_agent_runtime_generation,
)
from app.agent.rate_limit import AgentRateLimiter
from app.main import AgentBodyLimitMiddleware


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


async def _invoke_body_guard(
    chunks: list[bytes], *, headers: list[tuple[bytes, bytes]] | None = None, limit: int = 8
) -> tuple[list[dict], bytes | None]:
    received_body: bytes | None = None

    async def downstream(scope, receive, send):
        nonlocal received_body
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        received_body = bytes(body)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    if not messages:
        messages.append({"type": "http.request", "body": b"", "more_body": False})

    async def receive():
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/agent/query",
        "headers": headers or [],
    }
    await AgentBodyLimitMiddleware(downstream, max_bytes=limit)(scope, receive, send)
    return sent, received_body


class AgentBodyLimitMiddlewareTests(unittest.TestCase):
    def test_rejects_chunked_body_without_content_length(self):
        sent, received = asyncio.run(_invoke_body_guard([b"12345", b"6789"]))
        self.assertEqual(sent[0]["status"], 413)
        self.assertIsNone(received)
        self.assertEqual(json.loads(sent[-1]["body"]), {"error": "request body too large"})

    def test_rejects_actual_body_larger_than_declared_length(self):
        sent, received = asyncio.run(
            _invoke_body_guard([b"1234", b"56789"], headers=[(b"content-length", b"1")])
        )
        self.assertEqual(sent[0]["status"], 413)
        self.assertIsNone(received)

    def test_replays_body_at_exact_limit(self):
        sent, received = asyncio.run(_invoke_body_guard([b"1234", b"5678"]))
        self.assertEqual(sent[0]["status"], 204)
        self.assertEqual(received, b"12345678")

    def test_rejects_conflicting_content_lengths(self):
        sent, received = asyncio.run(
            _invoke_body_guard(
                [b"1234"],
                headers=[(b"content-length", b"4"), (b"content-length", b"1")],
            )
        )
        self.assertEqual(sent[0]["status"], 400)
        self.assertIsNone(received)
        self.assertEqual(
            json.loads(sent[-1]["body"]),
            {"error": "invalid request body length"},
        )

    def test_rejects_body_shorter_than_declared_length(self):
        sent, received = asyncio.run(
            _invoke_body_guard([b"1234"], headers=[(b"content-length", b"5")])
        )
        self.assertEqual(sent[0]["status"], 400)
        self.assertIsNone(received)
        self.assertEqual(
            json.loads(sent[-1]["body"]),
            {"error": "invalid request body length"},
        )

    def test_rejects_body_longer_than_declared_but_within_limit(self):
        sent, received = asyncio.run(
            _invoke_body_guard([b"1234"], headers=[(b"content-length", b"3")])
        )
        self.assertEqual(sent[0]["status"], 400)
        self.assertIsNone(received)


class AgentRateLimiterTests(unittest.TestCase):
    def test_expired_keys_are_reclaimed_globally(self):
        clock = _Clock()
        limiter = AgentRateLimiter(max_keys=4, cleanup_interval=2, clock=clock)
        self.assertTrue(limiter.allow("a", limit=1, window_seconds=10))
        self.assertTrue(limiter.allow("b", limit=1, window_seconds=10))
        self.assertEqual(limiter.tracked_keys(), 2)
        clock.value = 10.0
        self.assertEqual(limiter.tracked_keys(), 0)

    def test_identity_capacity_is_bounded_and_fail_closed(self):
        limiter = AgentRateLimiter(max_keys=2, cleanup_interval=1)
        self.assertTrue(limiter.allow("a", limit=1, window_seconds=60))
        self.assertTrue(limiter.allow("b", limit=1, window_seconds=60))
        self.assertFalse(limiter.allow("c", limit=1, window_seconds=60))
        self.assertEqual(limiter.tracked_keys(), 2)

    def test_allow_is_atomic_under_concurrency(self):
        limiter = AgentRateLimiter()
        barrier = threading.Barrier(20)

        def attempt() -> bool:
            barrier.wait()
            return limiter.allow("shared", limit=5, window_seconds=60)

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(lambda _: attempt(), range(20)))
        self.assertEqual(sum(results), 5)


class AsyncBridgeTests(unittest.TestCase):
    def test_runs_awaitable_from_sync_context(self):
        async def value():
            return 42

        self.assertEqual(run_awaitable_sync(value()), 42)

    def test_fails_fast_inside_running_event_loop(self):
        async def scenario():
            started = time.perf_counter()
            with self.assertRaises(AsyncBridgeUnavailable):
                run_awaitable_sync(asyncio.sleep(0.2))
            self.assertLess(time.perf_counter() - started, 0.05)

        asyncio.run(scenario())


class AgentRuntimeGenerationTests(unittest.TestCase):
    def test_invalidation_revokes_only_preexisting_runtime_work(self):
        previous = current_agent_runtime_generation()
        current = invalidate_agent_runtime_generation()

        self.assertGreater(current, previous)
        self.assertFalse(agent_runtime_generation_is_current(previous))
        self.assertTrue(agent_runtime_generation_is_current(current))


if __name__ == "__main__":
    unittest.main()
