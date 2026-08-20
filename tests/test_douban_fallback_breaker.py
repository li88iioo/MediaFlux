"""豆瓣回退链熔断与登录态错误语义的回归测试。

覆盖两个真实观察到的问题：
- dbcl2 过期时豆瓣 302 到登录页，被笼统报成 unavailable，用户无法定位原因
- 回退凭据失败后每次请求仍重打 Frodo/dbcl2，刷两行 WARNING
"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from unittest.mock import Mock

from app.discovery.models import (
    MediaCard,
    ProviderAuthenticationError,
    ProviderUnavailable,
)
from app.discovery.providers import douban as douban_provider_module
from app.discovery.providers.douban import DoubanProvider


class _Response:
    def __init__(self, status_code: int, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}

    def close(self) -> None:
        return None


class Dbcl2RedirectSemanticsTests(unittest.TestCase):
    """登录重定向必须报认证失效，而不是服务不可用。"""

    def _status_error(self, status: int, location: str = ""):
        from app.clients.douban_authenticated import DoubanAuthenticatedClient

        return DoubanAuthenticatedClient._status_error(
            _Response(status, {"Location": location} if location else {})
        )

    def test_redirect_to_login_reports_authentication(self):
        for location in (
            "https://accounts.douban.com/passport/login?redir=x",
            "https://www.douban.com/login",
            "https://sec.douban.com/a?c=x",
        ):
            with self.subTest(location=location):
                error = self._status_error(302, location)

                self.assertIsInstance(error, ProviderAuthenticationError)
                self.assertIn("dbcl2", str(error))

    def test_other_redirects_stay_unavailable(self):
        error = self._status_error(302, "https://movie.douban.com/other")

        self.assertIsInstance(error, ProviderUnavailable)

    def test_direct_auth_status_is_unchanged(self):
        error = self._status_error(403)

        self.assertIsInstance(error, ProviderAuthenticationError)


class _FailingClient:
    def __init__(self, error: Exception):
        self.error = error
        self.calls = 0
        self.configured = True

    def get_detail(self, *_args, **_kwargs):
        self.calls += 1
        raise self.error

    def list_items(self, *_args, **_kwargs):
        self.calls += 1
        raise self.error


class _BlockingFailingClient(_FailingClient):
    def __init__(self, error: Exception):
        super().__init__(error)
        self.started = threading.Event()
        self.release = threading.Event()
        self._calls_lock = threading.Lock()

    def _fail(self):
        with self._calls_lock:
            self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=2.0):
            raise AssertionError("test did not release blocking fallback")
        raise self.error

    def get_detail(self, *_args, **_kwargs):
        return self._fail()

    def list_items(self, *_args, **_kwargs):
        return self._fail()


class DoubanFallbackBreakerTests(unittest.TestCase):
    """Frodo 鉴权失败停用到重启，其他失败仍按阈值熔断。"""

    def _provider(self, frodo, authenticated, *, clock):
        public = Mock()
        public.get_detail.side_effect = ProviderUnavailable("公共接口失败")
        public.list_items.side_effect = ProviderUnavailable("公共接口失败")
        return DoubanProvider(
            enabled=True,
            public_client=public,
            frodo_client=frodo,
            authenticated_client=authenticated,
            clock=clock,
        )

    def test_auth_failure_skips_fallback_after_first_attempt(self):
        now = [1000.0]
        frodo = _FailingClient(ProviderAuthenticationError("认证失败"))
        authenticated = _FailingClient(ProviderAuthenticationError("登录态失效"))
        provider = self._provider(frodo, authenticated, clock=lambda: now[0])

        for _attempt in range(3):
            with self.assertRaises(Exception):
                provider.get_detail("38581618", "movie")

        # 首次失败即熔断，后续请求不再触碰回退客户端。
        self.assertEqual(frodo.calls, 1)
        self.assertEqual(authenticated.calls, 1)

    def test_frodo_stays_disabled_while_dbcl2_cooldown_expires(self):
        now = [1000.0]
        frodo = _FailingClient(ProviderAuthenticationError("认证失败"))
        authenticated = _FailingClient(ProviderAuthenticationError("登录态失效"))
        provider = self._provider(frodo, authenticated, clock=lambda: now[0])

        with self.assertRaises(Exception):
            provider.get_detail("38581618", "movie")
        now[0] += 1801.0
        with self.assertRaises(Exception):
            provider.get_detail("38581618", "movie")

        # Frodo 凭据运行期不会刷新，因此只在进程启动后探测一次；
        # dbcl2 仍保留 30 分钟复探，并可由配置变更提前清除熔断。
        self.assertEqual(frodo.calls, 1)
        self.assertEqual(authenticated.calls, 2)

    def test_frodo_auth_failure_logs_once_with_safe_source(self):
        now = [1000.0]
        frodo = _FailingClient(ProviderAuthenticationError("认证失败"))
        frodo.credential_source = "compatibility_default"
        provider = self._provider(frodo, Mock(configured=False), clock=lambda: now[0])

        with self.assertLogs("app.discovery.providers.douban", level="WARNING") as captured:
            for _attempt in range(3):
                with self.assertRaises(Exception):
                    provider.get_detail("38581618", "movie")
                now[0] += 86400.0

        warnings = [line for line in captured.output if "frodo 回退认证失败" in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("credential_source=compatibility_default", warnings[0])
        self.assertIn("当前进程内已停用", warnings[0])
        self.assertEqual(frodo.calls, 1)

    def test_process_rebuild_reuses_disabled_credential_state(self):
        now = [1000.0]
        identity = "test-process-credential-fingerprint"
        first_frodo = _FailingClient(ProviderAuthenticationError("认证失败"))
        first_frodo._credential_identity = identity
        first_frodo.credential_source = "environment"
        second_frodo = _FailingClient(ProviderAuthenticationError("不应再次调用"))
        second_frodo._credential_identity = identity
        second_frodo.credential_source = "environment"

        with douban_provider_module._FRODO_AUTH_STATE_CONDITION:
            douban_provider_module._FRODO_AUTH_DISABLED_CREDENTIALS.discard(identity)
            douban_provider_module._FRODO_AUTH_IN_FLIGHT.discard(identity)
            douban_provider_module._FRODO_AUTH_STATE_CONDITION.notify_all()
        try:
            first_provider = self._provider(
                first_frodo, Mock(configured=False), clock=lambda: now[0]
            )
            second_provider = self._provider(
                second_frodo, Mock(configured=False), clock=lambda: now[0]
            )

            with self.assertLogs(
                "app.discovery.providers.douban", level="WARNING"
            ) as captured:
                with self.assertRaises(Exception):
                    first_provider.get_detail("38581618", "movie")
                with self.assertRaises(Exception):
                    second_provider.get_detail("38581618", "movie")

            warnings = [line for line in captured.output if "frodo 回退认证失败" in line]
            self.assertEqual(len(warnings), 1)
            self.assertEqual(first_frodo.calls, 1)
            self.assertEqual(second_frodo.calls, 0)
        finally:
            with douban_provider_module._FRODO_AUTH_STATE_CONDITION:
                douban_provider_module._FRODO_AUTH_DISABLED_CREDENTIALS.discard(identity)
                douban_provider_module._FRODO_AUTH_IN_FLIGHT.discard(identity)
                douban_provider_module._FRODO_AUTH_STATE_CONDITION.notify_all()

    def test_concurrent_provider_rebuilds_share_process_probe(self):
        identity = "test-concurrent-process-credential-fingerprint"
        first_frodo = _BlockingFailingClient(
            ProviderAuthenticationError("认证失败")
        )
        first_frodo._credential_identity = identity
        first_frodo.credential_source = "environment"
        second_frodo = _BlockingFailingClient(
            ProviderAuthenticationError("不应再次调用")
        )
        second_frodo._credential_identity = identity
        second_frodo.credential_source = "environment"
        first_provider = self._provider(
            first_frodo, Mock(configured=False), clock=lambda: 1000.0
        )
        second_provider = self._provider(
            second_frodo, Mock(configured=False), clock=lambda: 1000.0
        )

        with douban_provider_module._FRODO_AUTH_STATE_CONDITION:
            douban_provider_module._FRODO_AUTH_DISABLED_CREDENTIALS.discard(identity)
            douban_provider_module._FRODO_AUTH_IN_FLIGHT.discard(identity)
            douban_provider_module._FRODO_AUTH_STATE_CONDITION.notify_all()
        try:
            with self.assertLogs(
                "app.discovery.providers.douban", level="WARNING"
            ) as captured:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(
                        first_provider.get_detail, "38581618", "movie"
                    )
                    self.assertTrue(first_frodo.started.wait(timeout=1.0))
                    second = pool.submit(
                        second_provider.get_detail, "38581618", "movie"
                    )
                    try:
                        with self.assertRaises(FutureTimeoutError):
                            second.result(timeout=0.05)
                    finally:
                        first_frodo.release.set()
                    for future in (first, second):
                        with self.assertRaises(ProviderUnavailable) as raised:
                            future.result(timeout=1.0)
                        self.assertIn("可用数据源均不可用", str(raised.exception))

            warnings = [line for line in captured.output if "frodo 回退认证失败" in line]
            self.assertEqual(len(warnings), 1)
            self.assertEqual(first_frodo.calls, 1)
            self.assertEqual(second_frodo.calls, 0)
        finally:
            first_frodo.release.set()
            second_frodo.release.set()
            with douban_provider_module._FRODO_AUTH_STATE_CONDITION:
                douban_provider_module._FRODO_AUTH_DISABLED_CREDENTIALS.discard(identity)
                douban_provider_module._FRODO_AUTH_IN_FLIGHT.discard(identity)
                douban_provider_module._FRODO_AUTH_STATE_CONDITION.notify_all()

    def test_untrusted_credential_source_is_normalized_before_logging(self):
        frodo = _FailingClient(ProviderAuthenticationError("认证失败"))
        frodo.credential_source = "must-not-appear-in-log"
        provider = self._provider(frodo, Mock(configured=False), clock=lambda: 1000.0)

        with self.assertLogs("app.discovery.providers.douban", level="WARNING") as captured:
            with self.assertRaises(Exception):
                provider.get_detail("38581618", "movie")

        rendered = "\n".join(captured.output)
        self.assertIn("credential_source=unknown", rendered)
        self.assertNotIn(frodo.credential_source, rendered)

    def test_concurrent_requests_share_one_fallback_probe(self):
        now = [1000.0]
        frodo = _BlockingFailingClient(ProviderAuthenticationError("认证失败"))
        provider = self._provider(frodo, Mock(configured=False), clock=lambda: now[0])

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(provider.get_detail, "38581618", "movie")
            self.assertTrue(frodo.started.wait(timeout=1.0))
            second = pool.submit(provider.get_detail, "38581618", "movie")
            try:
                with self.assertRaises(FutureTimeoutError):
                    second.result(timeout=0.05)
            finally:
                frodo.release.set()
            for future in (first, second):
                with self.assertRaises(ProviderUnavailable) as raised:
                    future.result(timeout=1.0)
                self.assertIn("可用数据源均不可用", str(raised.exception))

        self.assertEqual(frodo.calls, 1)
        self.assertEqual(provider._fallback_in_flight, set())

    def test_concurrent_list_requests_share_one_fallback_probe(self):
        frodo = _BlockingFailingClient(ProviderAuthenticationError("认证失败"))
        provider = self._provider(frodo, Mock(configured=False), clock=lambda: 1000.0)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(provider.list_items, "recommend", "movie", 1, {})
            self.assertTrue(frodo.started.wait(timeout=1.0))
            second = pool.submit(provider.list_items, "recommend", "movie", 1, {})
            try:
                with self.assertRaises(FutureTimeoutError):
                    second.result(timeout=0.05)
            finally:
                frodo.release.set()
            for future in (first, second):
                with self.assertRaises(ProviderUnavailable) as raised:
                    future.result(timeout=1.0)
                self.assertIn("可用数据源均不可用", str(raised.exception))

        self.assertEqual(frodo.calls, 1)
        self.assertEqual(provider._fallback_in_flight, set())

    def test_transient_failures_need_threshold_before_cooldown(self):
        now = [1000.0]
        frodo = _FailingClient(ProviderUnavailable("超时"))
        authenticated = _FailingClient(ProviderUnavailable("超时"))
        provider = self._provider(frodo, authenticated, clock=lambda: now[0])

        for _attempt in range(4):
            with self.assertRaises(Exception):
                provider.get_detail("38581618", "movie")

        # 阈值 3 次后熔断，第 4 次不再触碰。
        self.assertEqual(frodo.calls, 3)

    def test_successful_fallback_resets_the_breaker(self):
        now = [1000.0]
        card = MediaCard(
            provider="douban", external_id="38581618", media_type="movie", title="片名",
        )
        frodo = Mock()
        frodo.configured = True
        frodo.get_detail.side_effect = [ProviderUnavailable("超时"), {"ok": True}]
        provider = self._provider(frodo, Mock(configured=False), clock=lambda: now[0])
        provider._required_card = lambda _raw, _media_type: card

        with self.assertRaises(Exception):
            provider.get_detail("38581618", "movie")
        result = provider.get_detail("38581618", "movie")

        self.assertIs(result, card)
        self.assertEqual(provider._fallback_breakers, {})


if __name__ == "__main__":
    unittest.main()
