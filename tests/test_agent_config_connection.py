"""公共连接服务与SDK的真实HTTP边界；不访问配置外地址或真实媒体。"""
from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
import requests

from app import sdk
from app.modules import media_server_profiles as profiles
from app.modules.media_server_profiles import MediaServerProfile


@pytest.fixture
def connection_backend(monkeypatch):
    selected = [MediaServerProfile("configured:jellyfin", "jellyfin", "Jellyfin", "http://jellyfin.internal:8096", "jellyfin-secret-token", True),
                MediaServerProfile("configured:emby", "emby", "Emby", "https://emby.internal/base", "emby-secret-token", False)]
    payloads = {"jellyfin": {"ServerName": " Living\nRoom ", "ProductName": "Jellyfin Server", "Version": "12.0.1"},
                "emby": {"ServerName": "Legacy Room", "ProductName": "Emby", "Version": "4.9.1"}}
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        slot = next(profile.server_type for profile in selected if url == profile.url.rstrip("/") + "/System/Info")
        payload = payloads[slot]
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, requests.Response):
            return payload
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response
    monkeypatch.setattr(profiles, "list_configured_profiles", lambda: list(selected))
    monkeypatch.setattr(profiles.requests, "get", get)
    return selected, payloads, calls


def test_connection_uses_configured_snapshot_and_returns_complete_safe_identity(connection_backend):
    selected, payloads, calls = connection_backend
    payloads["jellyfin"]["Secret"] = "must-not-leak"
    result = sdk.library.connection(" JellyFin ")
    node = result["data"]["nodes"][0]
    assert result["status"] == "healthy" and result["ok"]
    assert node["server_name"] == "Living Room" and node["product_name"] == "Jellyfin Server"
    assert node["product"] == "jellyfin" and node["product_detected"] is True
    assert node["version"] == "12.0.1" and node["major_version"] == 12
    assert node["compatibility"] == "compatible" and node["error"] == ""
    assert 1 <= node["latency_ms"] <= 11_500 and result["data"]["checked_at"]
    assert calls[0][0] == selected[0].url + "/System/Info"
    assert calls[0][1] == {"headers": {"Accept": "application/json", "Authorization": 'MediaBrowser Token="jellyfin-secret-token"'},
                            "params": None, "timeout": (3.5, 8), "allow_redirects": False}
    serialized = json.dumps(result)
    assert all(value not in serialized for value in ("jellyfin.internal", "jellyfin-secret-token", "must-not-leak"))


@pytest.mark.parametrize("url,credential", [
    ("", "x"), ("file:///tmp/media", "x"), ("https://user:pass@emby.internal", "x"),
    ("https://emby.internal/base?next=/admin", "x"), ("https://emby.internal/base#fragment", "x"),
    ("https://emby.internal:bad", "x"), ("http://[invalid", "x"), ("http://media.invalid:0", "x"),
    ("http://media.invalid:65536", "x"), ("http://media.invalid", ""),
])
def test_invalid_configured_endpoints_do_not_send_credentials(connection_backend, url, credential):
    selected, _, calls = connection_backend
    selected[0] = replace(selected[0], url=url, credential=credential)
    result = sdk.library.connection("jellyfin")
    assert not result["ok"] and result["status"] == "not_configured"
    assert result["data"]["network_accessed"] is False and not calls
    assert result["data"]["nodes"][0]["reason_code"] == "configuration_incomplete"


def test_disabled_and_all_unconfigured_keep_nodes_without_network(connection_backend):
    selected, _, calls = connection_backend
    selected[0] = replace(selected[0], enabled=False)
    single = sdk.library.connection("jellyfin")
    assert single["status"] == "disabled" and not single["ok"]
    result = sdk.library.connection()
    assert result["status"] == "not_configured" and result["ok"]
    assert [node["slot"] for node in result["data"]["nodes"]] == ["jellyfin", "emby"]
    assert result["data"]["counts"] == {"enabled": 0, "configured": 0, "online": 0, "compatible": 0, "attention": 0}
    assert not result["data"]["network_accessed"] and not calls


@pytest.mark.parametrize("slot,product,version,compatibility,reason", [
    ("jellyfin", "Jellyfin", "12.1.0", "compatible", "jellyfin12_slot_compatible"),
    ("jellyfin", "Jellyfin", "13.0.0", "compatible", "jellyfin12_slot_compatible"),
    ("emby", "Emby", "4.9.1", "compatible", "emby_legacy_slot_compatible"),
    ("emby", "Emby", None, "compatible", "emby_legacy_slot_compatible"),
    ("emby", "Jellyfin", "10.11.11", "compatible", "jellyfin10_legacy_slot_compatible"),
    ("jellyfin", "Emby", "4.9.0", "wrong_slot", "use_legacy_slot"),
    ("jellyfin", "Jellyfin", "10.10.7", "wrong_slot", "use_legacy_slot"),
    ("emby", "Jellyfin", "12.0.0", "wrong_slot", "use_jellyfin12_slot"),
    ("emby", "Jellyfin", "9.1.0", "review", "jellyfin_major_not_classified"),
    ("jellyfin", "Jellyfin", "11.0.0", "review", "jellyfin_major_not_classified"),
    ("jellyfin", None, "12.0.1", "review", "product_unrecognized"),
    ("jellyfin", "Other Server", "12.0.1", "review", "product_unrecognized"),
    ("jellyfin", "Jellyfin", None, "review", "version_unrecognized"),
])
def test_version_and_slot_boundaries_are_preserved(connection_backend, slot, product, version, compatibility, reason):
    selected, payloads, _ = connection_backend
    for index, profile in enumerate(selected):
        selected[index] = replace(profile, enabled=profile.server_type == slot)
    payloads[slot] = {"Name": "Media Room", "Product": product, "ServerVersion": version}
    result = sdk.library.connection()
    node = next(node for node in result["data"]["nodes"] if node["enabled"])
    assert node["online"] and node["compatibility"] == compatibility and node["reason_code"] == reason
    assert result["status"] == ("healthy" if compatibility == "compatible" else "attention")
    assert node["product_detected"] is (product in {"Jellyfin", "Emby"})


@pytest.mark.parametrize("version", ["12.0.0+tokensecret123", "12.0.0-secretvalue", "token=other-secret", "012.0", "12.12345", True])
def test_unrecognized_versions_are_unknown_and_never_reflect_suffixes(connection_backend, version):
    _, payloads, _ = connection_backend
    payloads["jellyfin"]["Version"] = version
    result = sdk.library.connection()
    node = result["data"]["nodes"][0]
    assert node["version"] is node["major_version"] is None
    assert node["reason_code"] == "version_unrecognized"
    if isinstance(version, str):
        assert version not in json.dumps(result)


@pytest.mark.parametrize("status,expected", [(301, "redirect_not_allowed"), (307, "redirect_not_allowed"),
                                         (401, "authentication"), (403, "authentication"), (404, "not_found"), (503, "http_error")])
def test_http_failures_are_distinct_and_do_not_redirect_or_disappear(connection_backend, status, expected):
    _, payloads, calls = connection_backend
    response = requests.Response()
    response.status_code = status
    response.url = "http://private.invalid/?token=secret"
    response.headers["Location"] = "http://169.254.169.254/latest/secret"
    response._content = b"private body"
    payloads["jellyfin"] = response
    result = sdk.library.connection("jellyfin")
    assert result["status"] == expected and not result["ok"]
    node = result["data"]["nodes"][0]
    assert node["connection_status"] == expected and node["online"] is False and node["error"]
    assert len(calls) == 1 and calls[0][1]["allow_redirects"] is False
    assert result["data"]["nodes"] and result["data"]["network_accessed"]
    assert all(value not in json.dumps(result) for value in ("169.254.169.254", "private.invalid", "private body", "token=secret"))


@pytest.mark.parametrize("failure,expected", [(requests.Timeout("secret timeout"), "timeout"),
                                          (requests.ConnectionError("secret host"), "connection"),
                                          (ValueError("secret json"), "invalid_response"),
                                          (RuntimeError("secret internal"), "unavailable")])
def test_upstream_exceptions_are_safe_failures_not_empty_results(connection_backend, failure, expected):
    _, payloads, _ = connection_backend
    payloads["jellyfin"] = failure
    result = sdk.library.connection("jellyfin")
    assert result["status"] == expected and not result["ok"]
    assert result["data"]["nodes"][0]["error"] and "secret" not in json.dumps(result)


@pytest.mark.parametrize("payload", [["not", "a", "dict"], None, "private"])
def test_invalid_system_info_shape_is_a_failed_probe(connection_backend, payload):
    _, payloads, _ = connection_backend
    payloads["jellyfin"] = payload
    result = sdk.library.connection("jellyfin")
    assert result["status"] == "invalid_response" and result["data"]["nodes"][0]["online"] is False


@pytest.mark.parametrize("value", ["jellyfin-secret-token", "http://jellyfin.internal:8096", "Authorization: Bearer unseen-private-token", "/private/config"])
def test_identity_fields_cannot_reflect_credentials_or_addresses(connection_backend, value):
    _, payloads, _ = connection_backend
    payloads["jellyfin"] = {"ServerName": value, "ProductName": value, "Version": value}
    result = sdk.library.connection()
    assert result["data"]["nodes"][0]["server_name"] is None
    assert value not in json.dumps(result)


@pytest.mark.parametrize("server", [None, 1, True, "auto", "plex", "http://attacker.invalid"])
def test_invalid_sdk_server_arguments_do_not_probe(connection_backend, server):
    _, _, calls = connection_backend
    with pytest.raises(TypeError if not isinstance(server, str) else ValueError):
        sdk.library.connection(server)
    assert not calls
    with pytest.raises(TypeError):
        sdk.METHODS["library.connection"](url="http://attacker.invalid")
    assert not calls


def test_busy_or_configuration_failure_keeps_unobserved_counts_unknown(connection_backend, monkeypatch):
    _, _, calls = connection_backend
    semaphore = Mock()
    semaphore.acquire.return_value = False
    monkeypatch.setattr(profiles, "_CONNECTION_LIMIT", semaphore)
    busy = sdk.library.connection()
    assert not busy["ok"] and busy["status"] == "unavailable" and not busy["data"]["probe_complete"]
    assert busy["data"]["counts"]["online"] is None and busy["data"]["nodes"][0]["online"] is None
    assert busy["data"]["network_accessed"] is False and not calls
    semaphore.release.assert_not_called()
    monkeypatch.setattr(profiles, "list_configured_profiles", lambda: (_ for _ in ()).throw(RuntimeError("secret config path")))
    failed = sdk.library.connection()
    assert len(failed["data"]["nodes"]) == 2 and all(value is None for value in failed["data"]["counts"].values())
    assert failed["status"] == "unavailable" and "secret" not in json.dumps(failed)


def test_probe_latency_is_bounded_without_affecting_identity(connection_backend, monkeypatch):
    clock = iter([0, 1000])
    monkeypatch.setattr(profiles.time, "perf_counter", lambda: next(clock))
    result = sdk.library.connection("jellyfin")
    assert result["data"]["nodes"][0]["latency_ms"] == 11_500


def test_interrupted_probe_batch_does_not_fabricate_zero_counts_or_leak_errors(connection_backend, monkeypatch):
    worker = Mock()
    worker.map.side_effect = RuntimeError("secret http://private.internal")
    pool = Mock()
    pool.__enter__ = Mock(return_value=worker)
    pool.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(profiles, "ThreadPoolExecutor", lambda **_: pool)
    result = sdk.library.connection()
    assert result["status"] == "unavailable" and result["data"]["nodes"]
    assert result["data"]["network_accessed"] is None and result["data"]["counts"]["online"] is None
    assert "secret" not in json.dumps(result) and "private.internal" not in json.dumps(result)
