"""只读的软件更新检查；不静默下载，也不在应用内替换二进制。"""
from __future__ import annotations

import json
import platform
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.version import BuildInfo

_RELEASES_API = "https://api.github.com/repos/li88iioo/MediaFlux/releases?per_page=20"
_MAX_RESPONSE_BYTES = 1024 * 1024
_SEMVER = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class UpdateCheckError(RuntimeError):
    """远端发布信息无法被安全解析。"""


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    update_available: bool
    prerelease: bool
    release_url: str
    published_at: str
    recommended_asset_name: str
    recommended_asset_url: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_version(value: str) -> tuple[int, int, int, tuple[tuple[int, int | str], ...] | None]:
    match = _SEMVER.fullmatch((value or "").strip())
    if not match:
        raise UpdateCheckError(f"无法识别版本号：{value}")
    prerelease = match.group(4)
    parts: tuple[tuple[int, int | str], ...] | None = None
    if prerelease is not None:
        parts = tuple(
            (0, int(item)) if item.isdigit() else (1, item.lower())
            for item in prerelease.split(".")
        )
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), parts


def _version_sort_key(
    value: str,
) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    major, minor, patch, prerelease = _parse_version(value)
    return major, minor, patch, 1 if prerelease is None else 0, prerelease or ()


def _is_newer(candidate: str, current: str) -> bool:
    return _version_sort_key(candidate) > _version_sort_key(current)


def _preferred_suffixes(_build: BuildInfo, _machine: str) -> tuple[str, ...]:
    # GitHub release assets currently share the same archive formats for every build target.
    return (".tar.gz", ".zip")


def _select_asset(release: dict[str, Any], build: BuildInfo, machine: str) -> tuple[str, str]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        return "", ""
    suffixes = _preferred_suffixes(build, machine)
    for suffix in suffixes:
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            url = str(asset.get("browser_download_url") or "")
            if name.endswith(suffix) and url.startswith("https://github.com/"):
                return name, url
    return "", ""


def _read_response(response: Any) -> bytes:
    declared = str(getattr(response, "headers", {}).get("Content-Length", "") or "")
    if declared.isdigit() and int(declared) > _MAX_RESPONSE_BYTES:
        raise UpdateCheckError("更新响应体过大")
    payload = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise UpdateCheckError("更新响应体过大")
    return payload


def check_for_updates(
    *,
    build: BuildInfo | None = None,
    include_prerelease: bool = False,
    timeout: float = 5.0,
    opener: Callable[..., Any] = urlopen,
    machine: str | None = None,
) -> UpdateInfo:
    """查询 GitHub Release，仅返回建议；下载与安装必须由用户主动完成。"""
    current = build or BuildInfo.current()
    request = Request(
        _RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"MediaFlux/{current.version}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(_read_response(response).decode("utf-8"))
    except (HTTPError, URLError, OSError, TimeoutError, UnicodeError, ValueError) as exc:
        raise UpdateCheckError(f"无法检查更新：{type(exc).__name__}") from exc
    if not isinstance(payload, list):
        raise UpdateCheckError("更新服务返回了无效数据")

    candidates: list[dict[str, Any]] = []
    for release in payload:
        if not isinstance(release, dict) or release.get("draft") is True:
            continue
        if release.get("prerelease") is True and not include_prerelease:
            continue
        tag = str(release.get("tag_name") or "")
        try:
            _parse_version(tag)
        except UpdateCheckError:
            continue
        candidates.append(release)
    if not candidates:
        raise UpdateCheckError("没有找到可用的发布版本")
    latest = max(
        candidates,
        key=lambda item: _version_sort_key(str(item.get("tag_name") or "0.0.0")),
    )
    latest_version = str(latest.get("tag_name") or "").removeprefix("v")
    asset_name, asset_url = _select_asset(latest, current, machine or platform.machine())
    release_url = str(latest.get("html_url") or "")
    if release_url and not release_url.startswith("https://github.com/"):
        release_url = ""
    return UpdateInfo(
        current_version=current.version.removeprefix("v"),
        latest_version=latest_version,
        update_available=_is_newer(latest_version, current.version),
        prerelease=bool(latest.get("prerelease")),
        release_url=release_url,
        published_at=str(latest.get("published_at") or ""),
        recommended_asset_name=asset_name,
        recommended_asset_url=asset_url,
    )
