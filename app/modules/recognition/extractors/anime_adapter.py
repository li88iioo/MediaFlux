"""可选 Anitomy-ng 适配器。

适配器只把第三方解析结果投影为 :class:`ReleaseParseEvidence`，不判断输入
是否一定是动漫，也不修改生产识别上下文。依赖未安装、平台不支持或解析失败
时返回空证据，因此主程序与发布包不会把 ``anitomy-ng`` 变成硬依赖。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Iterable

from app.modules.recognition.models import ReleaseParseEvidence

_SOURCE = "anitomy_ng"
_FIELD_KINDS = {
    "TITLE": ("title", 0.88),
    "YEAR": ("year", 0.95),
    "SEASON": ("season", 0.98),
    "EPISODE": ("episode", 0.98),
    "TYPE": ("special_type", 0.95),
    "RELEASE_GROUP": ("release_group", 0.9),
}
_SPECIAL_TYPES = frozenset({"OVA", "OAD", "OAV", "SPECIAL", "SP"})


def _kind_name(kind: object) -> str:
    name = getattr(kind, "name", None)
    if name:
        return str(name).upper()
    return str(kind).rsplit(".", 1)[-1].upper()


def _positive_int(value: object, *, allow_zero: bool) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if number < (0 if allow_zero else 1):
        return None
    return number


def _normalized_value(kind: str, value: object) -> object | None:
    text = str(value or "").strip()
    if not text:
        return None
    if kind == "year":
        return text if len(text) == 4 and text.isdigit() else None
    if kind == "season":
        return _positive_int(text, allow_zero=True)
    if kind == "episode":
        return _positive_int(text, allow_zero=False)
    if kind == "special_type":
        normalized = text.upper()
        return normalized if normalized in _SPECIAL_TYPES else None
    return text


@lru_cache(maxsize=4096)
def _parse_anitomy_ng(value: str) -> tuple[tuple[str, str], ...]:
    """延迟加载可选依赖并缓存不可变的原始字段。"""
    import anitomy_ng

    return tuple((_kind_name(item.kind), str(item.value)) for item in anitomy_ng.parse(value))


def parse_anime_evidence(value: str) -> tuple[ReleaseParseEvidence, ...]:
    """返回实验解析证据；任何不可用状态都安全退化为空元组。"""
    raw = str(value or "").strip()
    if not raw:
        return ()
    try:
        items: Iterable[tuple[str, str]] = _parse_anitomy_ng(raw)
    except Exception:
        return ()

    evidence: list[ReleaseParseEvidence] = []
    seen: set[tuple[str, object]] = set()
    for external_kind, raw_value in items:
        mapped = _FIELD_KINDS.get(external_kind)
        if mapped is None:
            continue
        kind, confidence = mapped
        normalized = _normalized_value(kind, raw_value)
        if normalized is None:
            continue
        key = (kind, normalized)
        if key in seen:
            continue
        seen.add(key)
        evidence.append(
            ReleaseParseEvidence(
                kind=kind,
                source=_SOURCE,
                value=normalized,
                confidence=confidence,
            )
        )
    return tuple(evidence)
