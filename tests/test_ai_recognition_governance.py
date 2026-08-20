"""AI 媒体识别治理：限速、并发、日预算与熔断。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.modules import ai_recognition_governance as governance


class AIRecognitionGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        governance.reset_ai_recognition_governance_for_tests()

    def tearDown(self) -> None:
        governance.reset_ai_recognition_governance_for_tests()

    @staticmethod
    def _config(
        *,
        requests_per_minute: int = 10,
        daily_request_limit: int = 100,
        max_concurrency: int = 2,
        circuit_breaker_seconds: int = 60,
    ) -> governance.AIRecognitionGovernanceConfig:
        return governance.AIRecognitionGovernanceConfig(
            requests_per_minute=requests_per_minute,
            daily_request_limit=daily_request_limit,
            max_concurrency=max_concurrency,
            circuit_breaker_seconds=circuit_breaker_seconds,
        )

    def test_daily_rejection_releases_concurrency_and_does_not_consume_rpm(self):
        config = self._config(requests_per_minute=1, max_concurrency=1)
        with patch.object(governance, "governance_config", return_value=config):
            with self.assertRaises(governance.AIRecognitionGovernanceError):
                governance.acquire_ai_recognition_attempt(
                    "provider", reserve_daily=lambda _limit: False
                )
            lease = governance.acquire_ai_recognition_attempt(
                "provider", reserve_daily=lambda _limit: True
            )
            lease.release()

    def test_concurrency_slot_is_bounded_and_reusable(self):
        config = self._config(max_concurrency=1)
        with patch.object(governance, "governance_config", return_value=config):
            first = governance.acquire_ai_recognition_attempt(
                "provider", reserve_daily=lambda _limit: True
            )
            with self.assertRaises(governance.AIRecognitionGovernanceError) as caught:
                governance.acquire_ai_recognition_attempt(
                    "provider", reserve_daily=lambda _limit: True
                )
            self.assertIn("并发", str(caught.exception))
            first.release()
            second = governance.acquire_ai_recognition_attempt(
                "provider", reserve_daily=lambda _limit: True
            )
            second.release()

    def test_only_provider_failures_trip_circuit_breaker(self):
        config = self._config()
        with patch.object(governance, "governance_config", return_value=config):
            governance.record_ai_recognition_failure(
                "provider", provider_failure=False
            )
            lease = governance.acquire_ai_recognition_attempt(
                "provider", reserve_daily=lambda _limit: True
            )
            lease.release()

            for _ in range(3):
                governance.record_ai_recognition_failure("provider")
            with self.assertRaises(governance.AIRecognitionGovernanceError) as caught:
                governance.acquire_ai_recognition_attempt(
                    "provider", reserve_daily=lambda _limit: True
                )
            self.assertIn("熔断", str(caught.exception))

    def test_auth_and_rate_limit_responses_open_circuit(self):
        config = self._config(circuit_breaker_seconds=10)
        with patch.object(governance, "governance_config", return_value=config):
            for fingerprint, status_code, retry_after in (
                ("auth-provider", 401, ""),
                ("rate-provider", 429, "120"),
            ):
                with self.subTest(status_code=status_code):
                    governance.record_ai_recognition_failure(
                        fingerprint,
                        status_code=status_code,
                        retry_after=retry_after,
                    )
                    with self.assertRaises(
                        governance.AIRecognitionGovernanceError
                    ) as caught:
                        governance.acquire_ai_recognition_attempt(
                            fingerprint, reserve_daily=lambda _limit: True
                        )
                    self.assertIn("熔断", str(caught.exception))

    def test_provider_fingerprint_is_stable_and_never_contains_plaintext_key(self):
        first = governance.provider_fingerprint(
            base_url="https://ai.example/v1",
            model="model",
            api_key="top-secret-key",
            protocol="responses",
        )
        second = governance.provider_fingerprint(
            base_url="https://ai.example/v1",
            model="model",
            api_key="top-secret-key",
            protocol="responses",
        )
        changed = governance.provider_fingerprint(
            base_url="https://ai.example/v1",
            model="model",
            api_key="other-key",
            protocol="responses",
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)
        self.assertNotIn("top-secret-key", first)
        self.assertEqual(len(first), 64)


if __name__ == "__main__":
    unittest.main()
