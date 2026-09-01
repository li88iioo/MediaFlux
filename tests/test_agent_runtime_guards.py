from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.agent.async_bridge import AsyncBridgeUnavailable, run_awaitable_sync
from app.agent.feature_gate import (
    AgentRuntimeDisabled,
    agent_runtime_admission,
    agent_runtime_effect_admission,
    agent_runtime_generation_is_current,
    agent_runtime_transition,
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

    def test_runtime_generation_comparison_rejects_coercible_values(self):
        current = current_agent_runtime_generation()
        self.assertFalse(agent_runtime_generation_is_current(str(current)))
        self.assertFalse(agent_runtime_generation_is_current(True))
        self.assertFalse(agent_runtime_generation_is_current(False))

    def test_runtime_transition_waits_for_admitted_confirmation(self):
        admitted = threading.Event()
        release = threading.Event()
        transitioned = threading.Event()

        def confirm() -> None:
            with agent_runtime_admission():
                admitted.set()
                self.assertTrue(release.wait(2))

        def disable() -> None:
            with agent_runtime_transition():
                transitioned.set()

        with patch("app.agent.feature_gate.config.get_bool", return_value=True):
            confirm_thread = threading.Thread(target=confirm)
            transition_thread = threading.Thread(target=disable)
            confirm_thread.start()
            self.assertTrue(admitted.wait(1))
            transition_thread.start()
            self.assertFalse(transitioned.wait(0.1))
            release.set()
            confirm_thread.join(2)
            transition_thread.join(2)

        self.assertFalse(confirm_thread.is_alive())
        self.assertFalse(transition_thread.is_alive())
        self.assertTrue(transitioned.is_set())

    def test_runtime_admission_rejects_after_disable_transition(self):
        with patch("app.agent.feature_gate.config.get_bool", return_value=False):
            with self.assertRaises(AgentRuntimeDisabled):
                with agent_runtime_admission():
                    self.fail("disabled Agent must not admit a confirmation")

    def test_runtime_admission_rejects_stale_expected_generation(self):
        previous = current_agent_runtime_generation()
        current = invalidate_agent_runtime_generation()

        with patch("app.agent.feature_gate.config.get_bool", return_value=True):
            with self.assertRaises(AgentRuntimeDisabled):
                with agent_runtime_admission(expected_generation=previous):
                    self.fail("stale runtime work must not publish")
            with agent_runtime_admission(expected_generation=current) as admitted:
                self.assertEqual(admitted, current)

    def test_runtime_transition_waits_for_started_external_effect(self):
        generation = current_agent_runtime_generation()
        effect_started = threading.Event()
        release = threading.Event()
        transition_finished = threading.Event()

        def external_effect() -> None:
            with agent_runtime_effect_admission(generation):
                effect_started.set()
                self.assertTrue(release.wait(2))

        def disable() -> None:
            with agent_runtime_transition():
                invalidate_agent_runtime_generation()
            transition_finished.set()

        effect_thread = threading.Thread(target=external_effect)
        transition_thread = threading.Thread(target=disable)
        effect_thread.start()
        self.assertTrue(effect_started.wait(1))
        transition_thread.start()
        self.assertFalse(transition_finished.wait(0.1))
        release.set()
        effect_thread.join(2)
        transition_thread.join(2)

        self.assertTrue(transition_finished.is_set())
        self.assertFalse(agent_runtime_generation_is_current(generation))


if __name__ == "__main__":
    unittest.main()
