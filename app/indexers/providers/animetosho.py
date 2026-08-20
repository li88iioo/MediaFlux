from __future__ import annotations

import json
from datetime import datetime, timezone

from ..errors import IndexerInvalidResponse, IndexerRateLimited, IndexerSecurityError, IndexerUnavailable
from ..models import IndexerCapabilities, IndexerItem, IndexerPage, IndexerSearchRequest, ResolvedDownload
from .base import IndexerAdapter, fixed_host_join, magnet_infohash


class AnimeToshoAdapter(IndexerAdapter):
    site_id = "animetosho"
    site_name = "AnimeTosho"
    base_url = "https://animetosho.org/"
    api_url = "https://feed.animetosho.org/json"
    default_enabled = True
    capabilities = IndexerCapabilities(True, ("magnet", "torrent"))

    def __init__(self, *, http) -> None:
        self.http = http

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        response = await self.http.get(
            self.api_url,
            params={"q": request.query, "page": str(request.page)},
        )
        self._validate_status(response.status_code)
        self._require_json_response(response)
        entries = self._parse_entries(response.body)
        items: list[IndexerItem] = []
        for entry in entries:
            try:
                items.append(self._to_item(entry))
            except (IndexerInvalidResponse, IndexerSecurityError, ValueError):
                continue
        return IndexerPage(
            items=items,
            page=request.page,
            has_more=bool(entries) and request.page < 100,
            pagination_supported=True,
        )

    async def resolve(self, stored_result: IndexerItem) -> ResolvedDownload:
        if stored_result.site_id != self.site_id:
            raise IndexerSecurityError("result provider mismatch")
        if stored_result.magnet and magnet_infohash(stored_result.magnet):
            return ResolvedDownload(kind="magnet", value=stored_result.magnet)
        if stored_result.torrent_url:
            return ResolvedDownload(
                kind="torrent",
                value=fixed_host_join("https://storage.animetosho.org/", stored_result.torrent_url),
            )
        raise IndexerInvalidResponse("result has no downloadable candidate")

    def _to_item(self, entry: dict) -> IndexerItem:
        title = str(entry.get("title") or "").strip()
        if not title:
            raise IndexerInvalidResponse("AnimeTosho result omitted title")
        detail_url = None
        if entry.get("link"):
            try:
                detail_url = fixed_host_join(self.base_url, entry.get("link") or "")
            except IndexerSecurityError:
                detail_url = None
        torrent_url = entry.get("torrent_url")
        if torrent_url:
            try:
                torrent_url = fixed_host_join("https://storage.animetosho.org/", torrent_url)
            except IndexerSecurityError:
                torrent_url = None
        magnet = entry.get("magnet_uri")
        if magnet_infohash(magnet) is None:
            magnet = None
        kinds = tuple(kind for kind, value in (("magnet", magnet), ("torrent", torrent_url)) if value)
        return IndexerItem(
            site_id=self.site_id,
            site_name=self.site_name,
            title=title,
            detail_url=detail_url,
            size_bytes=_integer(entry.get("total_size")),
            seeders=_integer(entry.get("seeders")),
            leechers=_integer(entry.get("leechers")),
            downloads=_integer(entry.get("torrent_downloaded_count")),
            published_at=_timestamp(entry.get("timestamp")),
            download_state="ready" if kinds else "unavailable",
            download_kinds=kinds,
            magnet=magnet,
            torrent_url=torrent_url,
        )

    @staticmethod
    def _require_json_response(response) -> None:
        content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise IndexerInvalidResponse("AnimeTosho returned a non-JSON response")

    @staticmethod
    def _parse_entries(body: bytes) -> list[dict]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IndexerInvalidResponse("AnimeTosho returned malformed JSON") from exc
        if not isinstance(payload, list) or not all(isinstance(entry, dict) for entry in payload):
            raise IndexerInvalidResponse("AnimeTosho JSON response must be a list of objects")
        return payload

    @staticmethod
    def _validate_status(status_code: int) -> None:
        if status_code == 429:
            raise IndexerRateLimited("AnimeTosho returned HTTP 429")
        if status_code >= 500:
            raise IndexerUnavailable(f"AnimeTosho returned HTTP {status_code}")
        if status_code != 200:
            raise IndexerInvalidResponse(f"AnimeTosho returned HTTP {status_code}")


def _integer(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value) -> datetime | None:
    timestamp = _integer(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
