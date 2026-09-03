"""Media Agent 媒体服务器版本与兼容槽位诊断。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from app.agent.errors import AgentToolError
from app.agent.media_server_actions import (
    diagnose_media_servers,
    media_server_diagnosis_arguments,
)
from app.agent.models import ToolResult


def _probe(
    slot: str,
    *,
    status: str = "success",
    product: str = "Jellyfin",
    version: str = "12.0.1",
    latency_ms: object = 7,
    product_detected: bool = True,
) -> ToolResult:
    ok = status == "success"
    data = {"server_type": slot, "connection_status": status}
    if ok:
        data.update(
            {
                "product": product,
                "product_detected": product_detected,
                "version": version,
                "latency_ms": latency_ms,
            }
        )
    return ToolResult(
        ok=ok, status=status, summary=status, data=data, error="" if ok else status
    )


class AgentMediaServerDiagnosisTests(unittest.TestCase):
    def test_arguments_are_strict(self):
        self.assertEqual(media_server_diagnosis_arguments({}), {})
        for arguments in (
            {"debug": True},
            {"url": "http://attacker.invalid"},
            {"token": "secret"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                media_server_diagnosis_arguments(arguments)

    def test_disabled_nodes_return_fixed_safe_shape_without_network_claim(self):
        with patch(
            "app.agent.media_server_actions._probe_all",
            return_value=[
                _probe("jellyfin", status="disabled"),
                _probe("emby", status="disabled"),
            ],
        ):
            result = diagnose_media_servers({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "not_configured")
        self.assertFalse(result.data["network_accessed"])
        self.assertEqual(
            result.data["counts"],
            {
                "enabled": 0,
                "configured": 0,
                "online": 0,
                "compatible": 0,
                "attention": 0,
            },
        )
        self.assertEqual(
            [node["slot"] for node in result.data["nodes"]], ["jellyfin", "emby"]
        )
        self.assertTrue(
            all(node["compatibility"] == "disabled" for node in result.data["nodes"])
        )

    def test_supported_jellyfin12_emby_and_legacy_jellyfin_are_classified(self):
        cases = (
            (
                [
                    _probe("jellyfin", product="Jellyfin", version="12.1.0"),
                    _probe("emby", status="disabled"),
                ],
                "jellyfin12_slot_compatible",
            ),
            (
                [
                    _probe("jellyfin", status="disabled"),
                    _probe("emby", product="Emby", version="4.9.1"),
                ],
                "emby_legacy_slot_compatible",
            ),
            (
                [
                    _probe("jellyfin", status="disabled"),
                    _probe("emby", product="Jellyfin", version="10.11.11"),
                ],
                "jellyfin10_legacy_slot_compatible",
            ),
        )
        for probes, reason in cases:
            with (
                self.subTest(reason=reason),
                patch("app.agent.media_server_actions._probe_all", return_value=probes),
            ):
                result = diagnose_media_servers({})
            self.assertTrue(result.ok)
            self.assertEqual(result.status, "healthy")
            node = next(item for item in result.data["nodes"] if item["online"])
            self.assertEqual(node["compatibility"], "compatible")
            self.assertEqual(node["reason_code"], reason)

    def test_wrong_slot_and_unknown_version_require_attention(self):
        cases = (
            (
                [
                    _probe("jellyfin", product="Jellyfin", version="10.10.7"),
                    _probe("emby", status="disabled"),
                ],
                "use_legacy_slot",
            ),
            (
                [
                    _probe("jellyfin", status="disabled"),
                    _probe("emby", product="Jellyfin", version="12.0.0"),
                ],
                "use_jellyfin12_slot",
            ),
            (
                [
                    _probe(
                        "jellyfin", product="Jellyfin", version="token=other-secret"
                    ),
                    _probe("emby", status="disabled"),
                ],
                "version_unrecognized",
            ),
            (
                [
                    _probe("jellyfin", status="disabled"),
                    _probe("emby", product="Jellyfin", version="9.1.0"),
                ],
                "jellyfin_major_not_classified",
            ),
        )
        for probes, reason in cases:
            with (
                self.subTest(reason=reason),
                patch("app.agent.media_server_actions._probe_all", return_value=probes),
            ):
                result = diagnose_media_servers({})
            self.assertTrue(result.ok)
            self.assertEqual(result.status, "attention")
            node = next(item for item in result.data["nodes"] if item["online"])
            self.assertEqual(node["reason_code"], reason)
            if reason == "version_unrecognized":
                self.assertIsNone(node["version"])
                self.assertNotIn(
                    "other-secret", json.dumps(result.to_dict(), ensure_ascii=False)
                )

    def test_unverified_product_identity_never_fails_open_from_slot_fallback(self):
        with patch(
            "app.agent.media_server_actions._probe_all",
            return_value=[
                _probe(
                    "jellyfin",
                    product="Jellyfin",
                    product_detected=False,
                    version="12.0.1",
                ),
                _probe("emby", status="disabled"),
            ],
        ):
            result = diagnose_media_servers({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        node = result.data["nodes"][0]
        self.assertEqual(node["product"], "unknown")
        self.assertEqual(node["compatibility"], "review")
        self.assertEqual(node["reason_code"], "product_unrecognized")

    def test_connection_failures_are_fixed_and_do_not_leak_probe_payload(self):
        secret_result = ToolResult(
            ok=False,
            status="connection",
            summary="http://192.168.1.9/?token=secret",
            data={
                "server_type": "jellyfin",
                "connection_status": "connection",
                "server_name": "Authorization: Bearer secret",
                "url": "http://192.168.1.9",
            },
            error="secret exception",
        )
        with patch(
            "app.agent.media_server_actions._probe_all",
            return_value=[secret_result, _probe("emby", status="disabled")],
        ):
            result = diagnose_media_servers({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for forbidden in ("192.168.1.9", "Bearer", "secret", "url", "server_name"):
            self.assertNotIn(forbidden, serialized)
        node = result.data["nodes"][0]
        self.assertEqual(node["reason_code"], "connection_failed")
        self.assertFalse(node["online"])

    def test_version_suffixes_are_not_reflected(self):
        for unsafe in ("12.0.0+tokensecret123", "12.0.0-secretvalue"):
            with (
                self.subTest(version=unsafe),
                patch(
                    "app.agent.media_server_actions._probe_all",
                    return_value=[
                        _probe("jellyfin", version=unsafe),
                        _probe("emby", status="disabled"),
                    ],
                ),
            ):
                result = diagnose_media_servers({})
            serialized = json.dumps(result.to_dict(), ensure_ascii=False)
            self.assertEqual(
                result.data["nodes"][0]["reason_code"], "version_unrecognized"
            )
            self.assertIsNone(result.data["nodes"][0]["version"])
            self.assertNotIn(unsafe, serialized)

    def test_busy_and_unexpected_probe_failures_use_safe_fixed_results(self):
        semaphore = Mock()
        semaphore.acquire.return_value = False
        with patch("app.agent.media_server_actions._DIAGNOSTIC_SEMAPHORE", semaphore):
            busy = diagnose_media_servers({})
        self.assertEqual(busy.status, "unavailable")
        self.assertFalse(busy.data["network_accessed"])
        semaphore.release.assert_not_called()
        with patch(
            "app.agent.media_server_actions._probe_all",
            side_effect=RuntimeError("secret http://192.168.1.9"),
        ):
            failed = diagnose_media_servers({})
        self.assertEqual(failed.status, "unavailable")
        self.assertTrue(failed.data["network_accessed"])
        serialized = json.dumps(failed.to_dict(), ensure_ascii=False)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("192.168.1.9", serialized)

    def test_version_and_latency_are_strictly_bounded(self):
        with patch(
            "app.agent.media_server_actions._probe_all",
            return_value=[
                _probe("jellyfin", version="12.0.1", latency_ms=999999),
                _probe("emby", product="Emby", version="4.9.0", latency_ms=True),
            ],
        ):
            result = diagnose_media_servers({})
        self.assertEqual(result.data["nodes"][0]["latency_ms"], 11500)
        self.assertIsNone(result.data["nodes"][1]["latency_ms"])
