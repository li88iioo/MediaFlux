"""LLM 请求的保守 Token 估算与上下文预算。"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from typing import Any

_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def estimate_tokens(value: Any) -> int:
    """零依赖保守估算；未知 tokenizer 下宁可高估，不低估上下文。"""
    if not isinstance(value, str):
        try:
            value = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, OverflowError):
            value = str(value)
    text = unicodedata.normalize("NFKC", value)
    if not text:
        return 0

    tokens = 0
    occupied = [False] * len(text)
    for match in _ASCII_WORD_RE.finditer(text):
        tokens += max(1, math.ceil(len(match.group(0)) / 3))
        for index in range(match.start(), match.end()):
            occupied[index] = True
    for index, char in enumerate(text):
        if occupied[index]:
            continue
        if _CJK_RE.fullmatch(char):
            tokens += 2
        elif char.isspace():
            tokens += 1 if index == 0 or not text[index - 1].isspace() else 0
        else:
            tokens += 1
    return max(1, tokens)


def infer_context_window(model: str) -> int:
    """模型未知时使用 8K fail-safe；已知家族也采用保守下界。"""
    normalized = str(model or "").strip().casefold()
    if any(name in normalized for name in ("gpt-4", "gpt-5", "o1", "o3", "o4")):
        return 32_768
    if any(name in normalized for name in ("claude", "gemini", "deepseek", "qwen")):
        return 32_768
    if any(name in normalized for name in ("llama", "mistral", "mixtral")):
        return 8_192
    return 8_192


def resolve_context_window(configured: object, *, model: str) -> int:
    raw = str(configured or "").strip()
    if raw:
        try:
            return max(4_096, min(int(raw), 2_000_000))
        except (TypeError, ValueError, OverflowError):
            pass
    return infer_context_window(model)


def input_token_budget(
    *, context_window: int, output_reserve: int, safety_margin: int = 256
) -> int:
    return max(0, int(context_window) - max(1, int(output_reserve)) - max(64, int(safety_margin)))


def request_fits_token_budget(
    body: dict[str, Any], *, context_window: int, output_reserve: int
) -> bool:
    return estimate_tokens(body) <= input_token_budget(
        context_window=context_window,
        output_reserve=output_reserve,
    )


def trim_text_to_token_budget(
    value: str,
    *,
    max_tokens: int,
    keep_tail: bool = True,
    marker: str = "[较早上下文已按预算省略]\n",
) -> str:
    text = str(value or "")
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    marker_tokens = estimate_tokens(marker)
    available = max_tokens - marker_tokens
    if available <= 0:
        return ""

    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[-middle:] if keep_tail else text[:middle]
        if estimate_tokens(candidate) <= available:
            low = middle
        else:
            high = middle - 1
    if low <= 0:
        return ""
    retained = text[-low:] if keep_tail else text[:low]
    return marker + retained if keep_tail else retained + "\n" + marker.rstrip()


def fit_structured_user_content(
    *,
    body_without_user: dict[str, Any],
    user_content: str,
    context_window: int,
    output_reserve: int,
) -> str | None:
    # 把字符串放回 JSON 请求体时还会增加字段名、引号和转义开销。这里再留一层
    # 小型序列化余量，避免“分别估算刚好可放、完整 body 却超预算”的边界误差。
    serialization_margin = 64
    available = input_token_budget(
        context_window=context_window,
        output_reserve=output_reserve,
    ) - estimate_tokens(body_without_user) - serialization_margin
    if available <= 0:
        return None
    fitted = trim_text_to_token_budget(
        user_content,
        max_tokens=available,
        keep_tail=True,
    )
    return fitted or None
