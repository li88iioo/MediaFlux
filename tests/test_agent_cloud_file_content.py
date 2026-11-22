"""从 SDK 和真实工作区读取光鸭小文本，云响应使用本地夹具。"""
import asyncio
import hashlib
import json
from contextlib import contextmanager
from unittest.mock import Mock

import httpx
import pytest

from app import sdk
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.state import InMemorySessionStateStore
from app.clients.guangya import GuangYaFile
from app.modules import guangya_file_content
from tests.test_agent_code_sandbox import make_context


@pytest.fixture
def cloud_file(monkeypatch):
    client = Mock()
    client.logged_in = True
    client.get_download_url.return_value = "https://example.invalid/file?signature=private"
    monkeypatch.setattr("app.clients.guangya.GuangYaClient", lambda: client)

    def serve(name, data, **props):
        props.setdefault("size", len(data))
        client.file_info.return_value = GuangYaFile("item", name, False, **props)

        @contextmanager
        def stream(*args, **kwargs):
            yield httpx.Response(200, content=data, request=httpx.Request("GET", client.get_download_url.return_value))

        monkeypatch.setattr(guangya_file_content.httpx, "stream", stream)
        return client
    return serve


def test_read_text_preserves_line_endings_and_untrusted_content(cloud_file):
    data = "字幕第一行\r\n".encode("utf-16")
    cloud_file("中文.srt", data, md5=hashlib.md5(data, usedforsecurity=False).hexdigest())
    assert sdk.cloud.read_text("item") == {
        "file_id": "item", "name": "中文.srt", "size": len(data),
        "content": "字幕第一行\r\n", "content_trust": "untrusted",
    }


@pytest.mark.parametrize("size,digest", [(6, ""), (5, "0" * 32), (5, "invalid"), (4, "")])
def test_read_text_rejects_size_or_checksum_mismatch(cloud_file, size, digest):
    cloud_file("episode.nfo", b"12345", size=size, md5=digest)
    with pytest.raises(ValueError, match="校验|大于"):
        sdk.cloud.read_text("item")


def test_read_text_limits_and_empty_file(cloud_file):
    client = cloud_file("大文件.srt", b"", size=2**20 + 1)
    with pytest.raises(ValueError, match="1 MiB"):
        sdk.cloud.read_text("item")
    cloud_file("video.mp4", b"binary")
    with pytest.raises(ValueError, match="二进制"):
        sdk.cloud.read_text("item")
    client.get_download_url.assert_not_called()
    cloud_file("binary.txt", b"\xff")
    with pytest.raises(ValueError, match="UTF"):
        sdk.cloud.read_text("item")
    cloud_file("empty.nfo", b"")
    result = sdk.cloud.read_text("item")
    assert (result["size"], result["content"]) == (0, "")


def test_logout_during_read_does_not_return_previous_account_text(cloud_file, monkeypatch):
    client = cloud_file("episode.nfo", b"abc")

    @contextmanager
    def stream(*args, **kwargs):
        yield httpx.Response(200, content=b"abc", request=httpx.Request("GET", "https://example.invalid"))
        client.logged_in = False

    monkeypatch.setattr(guangya_file_content.httpx, "stream", stream)
    with pytest.raises(ValueError, match="凭据已变更"):
        sdk.cloud.read_text("item")


def test_read_text_via_real_workspace(cloud_file, tmp_path):
    if unavailable := sandbox.workspace_unavailable_reason():
        pytest.skip(unavailable)
    content = "<title>Episode</title>"
    cloud_file("ep1.nfo", content.encode())

    async def run():
        state = InMemorySessionStateStore()
        context = await make_context(state, owner="browser", session="first")
        terminal, _ = await sandbox._execute(tmp_path / "workspace", {"code":
            "import json, mf\n"
            "print(json.dumps(mf.cloud.read_text('item')))"
        }, context, ToolPipeline(catalog=ToolCatalog(), state_store=state))
        assert terminal["exit_code"] == 0, terminal
        result = json.loads(terminal["stdout"])
        assert result["content"] == content
        assert result["content_trust"] == "untrusted"

    asyncio.run(run())
