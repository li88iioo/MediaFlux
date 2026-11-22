"""保存通过校验的本地种子，供当前 Agent 会话选集。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterable
from functools import partial
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO

from app.runtime_paths import get_runtime_paths


def workspace_path(runtime_dir: Path, owner: str, session_id: str) -> Path:
    if not owner or not session_id:
        raise ValueError("缺少当前会话身份")
    scope = hashlib.sha256(json.dumps([owner, session_id]).encode()).hexdigest()
    return runtime_dir / "agent-workspaces" / scope


def _describe(path: Path) -> dict:
    return {"id": path.parent.name, "name": path.name, "size": path.stat().st_size}


def store_chunks(owner: str, session_id: str, filename: str, chunks: Iterable[bytes]) -> dict:
    from app.modules.download_dispatcher import TORRENT_MAX_BYTES, parse_torrent_manifest

    if Path(filename).suffix.lower() != ".torrent":
        raise ValueError("请选择 .torrent 种子")
    if (not filename or filename in {".", ".."} or "/" in filename or "\\" in filename
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)
            or len(filename.encode("utf-8")) > 255):
        raise ValueError("附件名必须是有效的单个文件名")
    workspace = workspace_path(get_runtime_paths().runtime_dir, owner, session_id)
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = workspace / "media-inputs"
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / uuid.uuid4().hex
    with TemporaryDirectory(prefix=".partial-", dir=directory) as temporary:
        path = Path(temporary) / filename
        buffer = BytesIO()
        for chunk in chunks:
            if buffer.tell() + len(chunk) > TORRENT_MAX_BYTES:
                raise ValueError("种子文件超过 10 MiB 限制")
            buffer.write(chunk)
        payload = buffer.getvalue()
        parse_torrent_manifest(payload)
        path.write_bytes(payload)
        os.rename(temporary, target)
    return _describe(target / filename)


def store_file(owner: str, session_id: str, filename: str, stream: BinaryIO) -> dict:
    return store_chunks(owner, session_id, filename, iter(partial(stream.read, 256 * 1024), b""))


def open_file(owner: str, session_id: str, artifact_id: str) -> tuple[dict, BinaryIO]:
    if not re.fullmatch("[a-f0-9]{32}", artifact_id):
        raise ValueError("会话文件编号无效")
    directory = workspace_path(get_runtime_paths().runtime_dir, owner, session_id) / "media-inputs" / artifact_id
    path = next(directory.glob("*"), None)
    if path is None:
        raise FileNotFoundError("当前会话没有这个文件")
    return _describe(path), path.open("rb")
