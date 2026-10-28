"""主应用 STRM `/playgy` 长期 bearer URL 的稳定签名。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

from app.modules.web_secret import get_web_secret

_VERSION = "1"
_CONTEXT = b"mediaflux:playgy-url:v1"


def _signing_key() -> bytes:
    return hmac.new(
        get_web_secret().encode("utf-8"), _CONTEXT, hashlib.sha256
    ).digest()


def _canonical(file_id: str | int, etag: str, size: str | int, version: str | int = _VERSION) -> bytes:
    payload = [str(version), str(file_id), (etag or "0"), str(size or 0)]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode_playgy_path_token(value: str) -> str:
    payload = (value or "").encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_playgy_path_token(value: str) -> str:
    token = (value or "").strip()
    if not token:
        raise ValueError("播放路径令牌为空")
    padding = "=" * (-len(token) % 4)
    try:
        payload = base64.b64decode(
            f"{token}{padding}", altchars=b"-_", validate=True,
        )
        return payload.decode("utf-8")
    except ValueError as exc:
        raise ValueError("播放路径令牌无效") from exc


def sign_playgy(file_id: str | int, etag: str, size: str | int) -> str:
    return hmac.new(_signing_key(), _canonical(file_id, etag, size), hashlib.sha256).hexdigest()


def verify_playgy(file_id: str | int, etag: str, size: str | int, version: str | int, signature: str) -> bool:
    if version != _VERSION or len(signature or "") != 64 or not signature.isascii():
        return False
    expected = sign_playgy(file_id, etag, size)
    return hmac.compare_digest(expected, signature)
