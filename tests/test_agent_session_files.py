"""本地种子选集的 HTTP 权限、会话隔离及临时文件生命周期。"""
from __future__ import annotations

import asyncio
import errno
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import config
from app.agent.kernel.sandbox import clear_workspace
from app.agent.owner_routes import web_kernel_owner
from app.modules import agent_files, download_dispatcher
from app.routes import agent_api
from app.runtime_paths import get_runtime_paths
from app.web import web_session_credential
from tests.support import initialized_web_runtime
from tests.test_config_empty_values_api import login
from tests.test_torrent_manifest import _torrent

SESSION = "session_attachment_web_0001"
OTHER_SESSION = "session_attachment_web_0002"
TORRENT = f"/api/agent/sessions/{SESSION}/torrent"


def torrent_data(pieces: int = 1) -> bytes:
    return _torrent({b"name": b"Episode.mkv", b"length": 16384 * pieces, b"piece length": 16384,
                     b"pieces": b"a" * (pieces * 20)})


@pytest.fixture
def client():
    from app.main import create_app

    with initialized_web_runtime(), TestClient(create_app()) as client:
        yield client


def upload_headers(name: str) -> dict[str, str]:
    return {"Content-Type": "application/octet-stream", "X-File-Name": quote(name, safe="")}


def test_only_the_submitting_session_can_resolve_the_uploaded_torrent(client):
    headers = upload_headers("剧集 #1.torrent")
    assert client.post(TORRENT, headers=headers, content=b"anonymous").status_code == 401
    csrf = login(client)
    assert client.post(TORRENT, headers=headers, content=b"untrusted").status_code == 403

    payload = torrent_data()
    response = client.post(TORRENT, headers={**headers, **csrf}, content=payload)
    assert response.status_code == 201, response.text
    saved = response.json()["file"]
    assert saved == {"id": saved["id"], "name": "剧集 #1.torrent", "size": len(payload)}

    owner = web_kernel_owner(config.web_credentials()[0])
    source = download_dispatcher.torrent_artifact_input(saved["id"], owner=owner, session_id=SESSION)
    assert source.torrent_data == payload
    for other_owner, other_session in ((web_kernel_owner("other"), SESSION), (owner, OTHER_SESSION)):
        with pytest.raises(ValueError, match="没有这个种子"):
            download_dispatcher.torrent_artifact_input(saved["id"], owner=other_owner, session_id=other_session)
    old_url = f"/api/agent/sessions/{SESSION}/files"
    assert client.post(old_url, headers={**headers, **csrf}, content=payload).status_code == 404
    assert client.get(f"{old_url}/{saved['id']}").status_code == 404


def test_upload_reports_full_disk_and_discards_staged_file(client, monkeypatch):
    csrf = login(client)
    store_chunks = agent_files.store_chunks

    def disk_full(owner, session_id, name, chunks):
        def failing_chunks():
            yield next(iter(chunks))
            raise OSError(errno.ENOSPC, "internal path should stay private")

        return store_chunks(owner, session_id, name, failing_chunks())

    monkeypatch.setattr(agent_files, "store_chunks", disk_full)
    failed = client.post(TORRENT, headers={**upload_headers("movie.torrent"), **csrf}, content=b"incomplete")
    assert failed.status_code == 507
    assert failed.json() == {"error": "服务器磁盘空间不足，清理空间后可重新上传"}
    assert not list(_attachments_dir().iterdir())


def test_large_body_exemption_hits_only_the_exact_upload_route(client):
    csrf = login(client)
    body = torrent_data(pieces=3400)
    headers = {**upload_headers("episodes.torrent"), **csrf}
    for url in ("/api/agent/query", f"{TORRENT}/", f"{TORRENT}/{'a' * 32}"):
        assert client.post(url, headers=headers, content=body).status_code == 413
    assert client.post(TORRENT, headers=headers, content=body).status_code == 201


def _attachments_dir():
    owner = web_kernel_owner(config.web_credentials()[0])
    return agent_files.workspace_path(get_runtime_paths().runtime_dir, owner, SESSION) / "media-inputs"


def _streaming_request(name, receive):
    return Request({
        "type": "http", "method": "POST", "path": TORRENT,
        "session": {"logged_in": web_session_credential(config.web_credentials())},
        "headers": [(key.lower().encode(), value.encode()) for key, value in upload_headers(name).items()],
    }, receive)


@pytest.mark.parametrize("name", ["../bad.torrent", "video.mp4", "manifest.json"])
def test_invalid_filename_is_rejected_before_receiving_body(client, name):
    async def receive():
        raise AssertionError("invalid filename should be rejected before reading the body")

    response = asyncio.run(agent_api.upload_session_torrent(_streaming_request(name, receive), SESSION))
    assert response.status_code == 400
    assert not _attachments_dir().exists()


@pytest.mark.parametrize("clear", [False, True], ids=["disconnected", "session-cleared"])
def test_interrupted_upload_never_publishes_partial_or_stale_file(client, clear):
    owner = web_kernel_owner(config.web_credentials()[0])
    first = True
    payload = torrent_data()
    attachments = _attachments_dir()

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": payload[:10], "more_body": True}
        assert len(list(attachments.glob(".partial-*"))) == 1
        assert not list(attachments.glob("[0-9a-f]*"))
        if clear:
            clear_workspace(owner=owner, session_id=SESSION)
            return {"type": "http.request", "body": payload[10:], "more_body": False}
        return {"type": "http.disconnect"}

    response = asyncio.run(agent_api.upload_session_torrent(_streaming_request("video.torrent", receive), SESSION))
    assert response.status_code == (409 if clear else 400)
    assert not list(attachments.glob("*"))


def test_invalid_torrent_is_rejected_without_publishing(client):
    headers = {**upload_headers("fake.torrent"), **login(client)}
    assert client.post(TORRENT, headers=headers, content=b"not a torrent").status_code == 400
    assert not list(_attachments_dir().iterdir())


def test_oversized_torrent_stops_consuming_the_stream(client, monkeypatch):
    monkeypatch.setattr(download_dispatcher, "TORRENT_MAX_BYTES", 10)

    def chunks():
        yield b"12345678"
        yield b"1234"
        pytest.fail("超过上限后应停止接收")

    owner = web_kernel_owner(config.web_credentials()[0])
    with pytest.raises(ValueError, match="超过"):
        agent_files.store_chunks(owner, SESSION, "large.torrent", chunks())
    assert not list(_attachments_dir().iterdir())
