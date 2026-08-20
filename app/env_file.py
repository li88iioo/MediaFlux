"""无副作用地解析 MediaFlux user.env。"""
from __future__ import annotations

import re

_LITERAL_LINE_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)='((?:\\'|[^'])*)'\s+# mediaflux-literal\s*$"
)
_PLAIN_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def read_env_text(text: str) -> dict[str, str]:
    """解析正式 user.env；值保持字面量，不做 shell/dotenv 插值。"""
    values: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        literal = _LITERAL_LINE_RE.fullmatch(line)
        if literal is not None:
            key, encoded = literal.groups()
            values[key] = encoded.replace("\\'", "'")
            continue
        plain = _PLAIN_LINE_RE.fullmatch(line)
        if plain is not None:
            key, value = plain.groups()
            values[key] = value.strip()
            continue
        raise ValueError(f"user.env 第 {line_number} 行格式无效")
    return values


def read_env_bytes(payload: bytes) -> dict[str, str]:
    """以 UTF-8 解析 user.env 字节。"""
    return read_env_text(payload.decode("utf-8"))
