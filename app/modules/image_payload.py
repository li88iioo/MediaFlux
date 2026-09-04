"""受限读取可信图片 MIME 的共享上游响应。"""
from __future__ import annotations

from typing import Protocol

MAX_IMAGE_BYTES = 5 * 1024 * 1024
SAFE_IMAGE_MIME_TYPES = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/avif",
    "image/gif",
})


class ImagePayloadError(ValueError):
    """上游图片的类型、大小或内容签名不可信。"""


class ImageStreamResponse(Protocol):
    headers: object

    def iter_content(self, chunk_size: int = 64 * 1024): ...


def matches_image_magic(content_type: str, content: bytes) -> bool:
    if content_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return (
            len(content) >= 12
            and content[:4] == b"RIFF"
            and content[8:12] == b"WEBP"
        )
    if content_type == "image/avif":
        if len(content) < 12 or content[4:8] != b"ftyp":
            return False
        brands = {content[8:12]}
        brands.update(
            content[offset:offset + 4]
            for offset in range(16, min(len(content), 64), 4)
        )
        return bool(brands & {b"avif", b"avis"})
    return False


def read_bounded_image(
    upstream: ImageStreamResponse,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> tuple[bytes, str]:
    maximum = max(1, int(max_bytes))
    headers = getattr(upstream, "headers", {})
    content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type not in SAFE_IMAGE_MIME_TYPES:
        raise ImagePayloadError("invalid upstream content type")
    try:
        content_length = int(headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        content_length = 0
    if content_length > maximum:
        raise ImagePayloadError("upstream image too large")

    chunks: list[bytes] = []
    total = 0
    for chunk in upstream.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > maximum:
            raise ImagePayloadError("upstream image too large")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not matches_image_magic(content_type, content):
        raise ImagePayloadError("upstream image content mismatch")
    return content, content_type
