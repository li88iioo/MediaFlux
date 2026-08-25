import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.responses import JSONResponse

from app.routes import tools_api


def _request():
    return SimpleNamespace(session={"logged_in": True, "csrf_token": "tmdb-test-token"})


def _payload(response):
    assert isinstance(response, JSONResponse)
    return json.loads(response.body)


def _config(values):
    return patch.object(
        tools_api.config,
        "get",
        side_effect=lambda key, default="": values.get(key, default),
    )


def test_tmdb_test_uses_current_draft_and_rejects_redirects():
    success = MagicMock(status_code=200)
    success.json.return_value = {"images": {"secure_base_url": "https://image.tmdb.org/t/p/"}}
    with patch.object(tools_api.agent_rate_limiter, "allow", return_value=True), _config({}), patch.object(
        tools_api.requests, "get", return_value=success
    ) as request_get:
        response = tools_api.tmdb_test(
            _request(),
            {"api_key": "draft-key", "api_url": "https://api.tmdb.org/3/"},
        )

    assert response.status_code == 200
    assert _payload(response)["ok"] is True
    request_get.assert_called_once_with(
        "https://api.tmdb.org/3/configuration",
        params={"api_key": "draft-key"},
        headers={"User-Agent": "MediaFlux/1.0"},
        proxies=None,
        timeout=(3.0, 8.0),
        allow_redirects=False,
    )

    redirect = MagicMock(status_code=302)
    with patch.object(tools_api.agent_rate_limiter, "allow", return_value=True), _config({}), patch.object(
        tools_api.requests, "get", return_value=redirect
    ):
        rejected = tools_api.tmdb_test(
            _request(),
            {"api_key": "draft-key", "api_url": "https://api.tmdb.org/3"},
        )
    assert rejected.status_code == 502
    assert "拒绝跟随" in _payload(rejected)["error"]



def test_tmdb_test_reuses_saved_key_only_for_saved_url():
    response_mock = MagicMock(status_code=200)
    response_mock.json.return_value = {"images": {}}
    values = {
        "TMDB_API_KEY": "saved-key",
        "TMDB_API_URL": "http://tmdb-gateway.local:8080/api/3",
    }
    with patch.object(tools_api.agent_rate_limiter, "allow", return_value=True), _config(values), patch.object(
        tools_api.requests, "get", return_value=response_mock
    ) as request_get:
        response = tools_api.tmdb_test(
            _request(),
            {"api_key": "********", "api_url": "http://tmdb-gateway.local:8080/api/3/"},
        )
    assert response.status_code == 200
    assert request_get.call_args.kwargs["params"] == {"api_key": "saved-key"}
    assert request_get.call_args.args[0] == "http://tmdb-gateway.local:8080/api/3/configuration"


def test_tmdb_test_requires_authenticated_session():
    anonymous = SimpleNamespace(session={})
    try:
        tools_api.tmdb_test(anonymous, {})
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 401
    else:
        raise AssertionError("未登录请求必须被拒绝")

def test_tmdb_test_never_combines_saved_key_with_unsaved_url():
    values = {
        "TMDB_API_KEY": "saved-secret",
        "TMDB_API_URL": "https://api.themoviedb.org/3",
    }
    with patch.object(tools_api.agent_rate_limiter, "allow", return_value=True), _config(values), patch.object(
        tools_api.requests, "get"
    ) as request_get:
        response = tools_api.tmdb_test(
            _request(),
            {"api_key": "********", "api_url": "http://127.0.0.1:8096/tmdb"},
        )
    assert response.status_code == 400
    assert "请先保存" in _payload(response)["error"]
    request_get.assert_not_called()


def test_tmdb_test_validates_url_proxy_and_unknown_fields_before_network():
    cases = [
        {"api_key": "key", "api_url": "file:///etc/passwd"},
        {"api_key": "key", "api_url": "https://user:pass@tmdb.example/3"},
        {"api_key": "key", "api_url": "https://tmdb.example/3?target=other"},
        {"api_key": "key", "api_url": "https://tmdb.example/3", "extra": True},
    ]
    for data in cases:
        with patch.object(tools_api.agent_rate_limiter, "allow", return_value=True), _config({}), patch.object(
            tools_api.requests, "get"
        ) as request_get:
            response = tools_api.tmdb_test(_request(), data)
        assert response.status_code == 400
        request_get.assert_not_called()

    with patch.object(tools_api.agent_rate_limiter, "allow", return_value=True), _config({}), patch.object(
        tools_api.requests, "get"
    ) as request_get:
        response = tools_api.tmdb_test(
            _request(), {"api_key": "explicit-key", "api_url": "http://127.0.0.1:8096/tmdb"}
        )
    assert response.status_code == 400
    assert "请先保存" in _payload(response)["error"]
    request_get.assert_not_called()

    with patch.object(tools_api.agent_rate_limiter, "allow", return_value=True), _config({"PROXY_URL": "ftp://proxy.invalid", "TMDB_API_URL": "https://tmdb.example/3"}), patch.object(
        tools_api.requests, "get"
    ) as request_get:
        response = tools_api.tmdb_test(
            _request(), {"api_key": "key", "api_url": "https://tmdb.example/3"}
        )
    assert response.status_code == 400
    assert _payload(response)["error"] == "代理地址无效"
    request_get.assert_not_called()


def test_tmdb_test_returns_stable_safe_errors_and_rate_limits():
    with patch.object(tools_api.agent_rate_limiter, "allow", return_value=False), patch.object(
        tools_api.requests, "get"
    ) as request_get:
        limited = tools_api.tmdb_test(_request(), {})
    assert limited.status_code == 429
    assert "过于频繁" in _payload(limited)["error"]
    request_get.assert_not_called()

    secret = "draft-secret"
    failure = tools_api.requests.exceptions.ConnectionError(
        f"failed https://internal.invalid/configuration?api_key={secret}"
    )
    with patch.object(tools_api.agent_rate_limiter, "allow", return_value=True), _config({"TMDB_API_URL": "https://tmdb.example/3"}), patch.object(
        tools_api.requests, "get", side_effect=failure
    ):
        response = tools_api.tmdb_test(
            _request(), {"api_key": secret, "api_url": "https://tmdb.example/3"}
        )
    assert response.status_code == 502
    body = json.dumps(_payload(response), ensure_ascii=False)
    assert secret not in body
    assert "internal.invalid" not in body
