"""发布构建与用户更新检查共用的 SemVer 优先级。"""
from __future__ import annotations

import re
from typing import NamedTuple

_PRERELEASE_IDENTIFIER = r"(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER = re.compile(
    rf"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    rf"(?:-({_PRERELEASE_IDENTIFIER}(?:\.{_PRERELEASE_IDENTIFIER})*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class VersionOrder(NamedTuple):
    major: int
    minor: int
    patch: int
    stable: bool
    identifiers: tuple[tuple[int, int | str], ...]


def version_order(tag: str) -> VersionOrder:
    match = _SEMVER.fullmatch(tag.strip())
    if not match:
        raise ValueError(f"非法 SemVer tag：{tag}")
    prerelease = match.group(4)
    identifiers = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in prerelease.split(".")
    ) if prerelease else ()
    return VersionOrder(int(match[1]), int(match[2]), int(match[3]), prerelease is None, identifiers)
