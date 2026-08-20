"""MediaFlux 构建信息。"""
from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from app import __version__
from app.build_metadata import PACKAGE_TYPE as EMBEDDED_PACKAGE_TYPE


def _embedded_build_info() -> dict[str, str]:
    candidates: list[Path] = []
    explicit = os.getenv("MEDIAFLUX_BUILD_INFO_FILE")
    if explicit:
        candidates.append(Path(explicit))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "BUILD-INFO.json")
    candidates.append(Path(__file__).resolve().with_name("_build_info.json"))
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            return {str(key): str(value) for key, value in payload.items() if value is not None}
    return {}


@dataclass(frozen=True)
class BuildInfo:
    """可用于人类阅读和机器消费的构建元数据。"""

    name: str
    version: str
    commit: str
    build_time: str
    python: str
    platform: str
    package: str
    arch: str = ""

    @classmethod
    def current(cls) -> "BuildInfo":
        embedded = _embedded_build_info()
        return cls(
            name="MediaFlux",
            version=embedded.get("version", __version__),
            commit=embedded.get("commit", os.getenv("MEDIAFLUX_BUILD_COMMIT", "")),
            build_time=embedded.get("build_time", os.getenv("MEDIAFLUX_BUILD_TIME", "")),
            python=platform.python_version(),
            platform=platform.platform(),
            package=embedded.get("package", EMBEDDED_PACKAGE_TYPE),
            arch=embedded.get("arch", platform.machine()),
        )

    def as_dict(self) -> dict[str, str]:
        """返回稳定的 JSON 序列化字段。"""
        return asdict(self)
