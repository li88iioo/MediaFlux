"""只读的软件更新检查；不静默下载，也不在应用内替换二进制。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.semver import VersionOrder, version_order
from app.version import BuildInfo

_RELEASES_API = "https://api.github.com/repos/li88iioo/MediaFlux/releases?per_page=20"
_MAX_RESPONSE_BYTES = 1024 * 1024


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


def _select_asset(release: dict[str, Any]) -> tuple[str, str]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        return "", ""
    for suffix in (".tar.gz", ".zip"):
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
    except (HTTPError, URLError, OSError, UnicodeError, ValueError) as exc:
        raise UpdateCheckError(f"无法检查更新：{type(exc).__name__}") from exc
    if not isinstance(payload, list):
        raise UpdateCheckError("更新服务返回了无效数据")

    candidates: list[tuple[VersionOrder, dict[str, Any]]] = []
    for release in payload:
        if not isinstance(release, dict) or release.get("draft") is True:
            continue
        try:
            order = version_order(str(release.get("tag_name") or ""))
        except ValueError:
            continue
        if include_prerelease or (order.stable and release.get("prerelease") is not True):
            candidates.append((order, release))
    if not candidates:
        raise UpdateCheckError("没有找到可用的发布版本")
    order, latest = max(candidates, key=lambda candidate: candidate[0])
    latest_version = str(latest["tag_name"]).strip().removeprefix("v")
    try:
        current_order = version_order(current.version)
    except ValueError as exc:
        raise UpdateCheckError(f"无法识别版本号：{current.version}") from exc
    asset_name, asset_url = _select_asset(latest)
    release_url = str(latest.get("html_url") or "")
    if release_url and not release_url.startswith("https://github.com/"):
        release_url = ""
    return UpdateInfo(
        current_version=current.version.removeprefix("v"),
        latest_version=latest_version,
        update_available=order > current_order,
        prerelease=not order.stable or bool(latest.get("prerelease")),
        release_url=release_url,
        published_at=str(latest.get("published_at") or ""),
        recommended_asset_name=asset_name,
        recommended_asset_url=asset_url,
    )
