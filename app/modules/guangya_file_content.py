"""从已登录光鸭账号读取和校验小文本正文。"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.clients.guangya import GuangYaClient, GuangYaFile, normalize_provider_md5

_TEXT_BYTES = 1024 * 1024


def _file(client: GuangYaClient, file_id: str) -> GuangYaFile:
    if not isinstance(file_id, str) or not file_id.strip():
        raise ValueError("请选择光鸭文件")
    item = client.file_info(file_id)
    if item is None or item.file_id != file_id:
        raise ValueError("光鸭文件不存在或已变更")
    if item.is_dir:
        raise ValueError("请选择文件，目录不能作为文件读取")
    return item


def _content(client: GuangYaClient, item: GuangYaFile, cancelled: Callable[[], bool] | None) -> Iterator[bytes]:
    def check_cancelled():
        if cancelled and cancelled():
            raise InterruptedError("读取光鸭文件已停止")

    check_cancelled()
    url = client.get_download_url(item.file_id)
    address = urlsplit(url or "")
    if not url or address.scheme not in {"https", "http"} or not address.hostname or address.username or address.password:
        raise ValueError("光鸭未提供可用的下载地址")
    expected_md5 = normalize_provider_md5(item.md5)
    if item.md5.strip() and expected_md5 is None:
        raise ValueError("光鸭返回的文件 MD5 格式无效，无法校验")
    digest = hashlib.md5(usedforsecurity=False) if expected_md5 else None
    received = 0
    try:
        with httpx.stream("GET", url, follow_redirects=True, headers={"Accept-Encoding": "identity"},
                          timeout=httpx.Timeout(30, connect=10)) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(256 * 1024):
                check_cancelled()
                received += len(chunk)
                if received > item.size:
                    raise ValueError("下载内容大于云端文件大小")
                if digest:
                    digest.update(chunk)
                yield chunk
    except httpx.HTTPError as exc:
        # HTTP 异常可能包含临时签名地址，仅向调用方返回可公开信息。
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        raise ValueError(f"光鸭文件下载失败（HTTP {status}）" if status else "光鸭文件下载中断") from None
    check_cancelled()
    if not client.logged_in:
        raise ValueError("读取期间光鸭凭据已变更，请重新选择文件")
    if received != item.size or (digest and digest.hexdigest() != expected_md5):
        raise ValueError("光鸭文件未通过大小或 MD5 校验，请重新获取")


def read_text(client: GuangYaClient, file_id: str, cancelled: Callable[[], bool] | None = None) -> dict:
    item = _file(client, file_id)
    if item.size > _TEXT_BYTES:
        raise ValueError("正文读取上限为 1 MiB")
    if item.mime_type.startswith(("video/", "audio/", "image/")) or Path(item.name).suffix.lower() in {
        ".pdf", ".zip", ".rar", ".7z", ".gz", ".mkv", ".mp4", ".png", ".jpg", ".docx", ".xlsx", ".pptx",
    }:
        raise ValueError("该文件为二进制格式，无法读取正文")
    payload = b"".join(_content(client, item, cancelled))
    try:
        encoding = "utf-16" if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        content = payload.decode(encoding)
    except UnicodeDecodeError:
        raise ValueError("无法按 UTF-8 或带 BOM 的 UTF-16 读取该文件") from None
    if "\x00" in content:
        raise ValueError("文件含二进制内容，无法读取正文")
    return {"file_id": item.file_id, "name": item.name, "size": len(payload),
            "content": content, "content_trust": "untrusted"}
