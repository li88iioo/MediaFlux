from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import quote

from ..errors import IndexerInvalidResponse, IndexerRateLimited, IndexerUnavailable
from ..models import IndexerCapabilities, IndexerItem, IndexerPage, IndexerSearchRequest
from .base import DirectResultAdapter

_INFO_HASH = re.compile(r"^[0-9a-fA-F]{40}$")
_TRACKERS = (
    "udp://tracker.coppersurfer.tk:6969/announce",
    "udp://tracker.leechers-paradise.org:6969",
    "udp://open.demonii.com:1337/announce",
)
_CATEGORIES = {
    "1": "Audio",
    "2": "Video",
    "3": "Applications",
    "4": "Games",
    "5": "Porn",
}
_INVALID_NUMBER = object()


class PirateBayAdapter(DirectResultAdapter):
    site_id = "tpb"
    site_name = "The Pirate Bay"
    base_url = "https://thepiratebay.org/"
    api_url = "https://apibay.org/q.php"
    default_enabled = True
    capabilities = IndexerCapabilities(False, ("magnet",))

    def __init__(self, *, http) -> None:
        self.http = http

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        if request.page > 1:
            return IndexerPage(items=[], page=request.page, has_more=False, pagination_supported=False)
        response = await self.http.get(self.api_url, params={"q": request.query})
        self._validate_status(response.status_code)
        self._require_json_response(response)
        entries = self._parse_entries(response.body)
        if len(entries) == 1 and str(entries[0].get("id") or "") == "0":
            entries = []
        query_tokens = request.query.lower().split()
        items = [
            item
            for entry in entries
            if (item := self._to_item(entry, query_tokens)) is not None
        ]
        return IndexerPage(items=items, page=1, has_more=False, pagination_supported=False)

    def _to_item(self, entry: dict, query_tokens: list[str]) -> IndexerItem | None:
        title = entry.get("name")
        if not isinstance(title, str):
            return None
        title = title.strip()
        if not title or not all(token in title.lower() for token in query_tokens):
            return None
        info_hash = entry.get("info_hash")
        if not isinstance(info_hash, str):
            return None
        info_hash = info_hash.strip()
        if _INFO_HASH.fullmatch(info_hash) is None:
            return None
        size_bytes = _integer(entry.get("size"))
        seeders = _integer(entry.get("seeders"))
        leechers = _integer(entry.get("leechers"))
        added = _integer(entry.get("added"))
        if any(value is _INVALID_NUMBER for value in (size_bytes, seeders, leechers, added)):
            return None
        normalized_hash = info_hash.lower()
        magnet = (
            f"magnet:?xt=urn:btih:{normalized_hash}&dn={quote(title, safe='')}"
            + "".join(f"&tr={tracker}" for tracker in _TRACKERS)
        )
        return IndexerItem(
            site_id=self.site_id,
            site_name=self.site_name,
            title=title,
            category=_CATEGORIES.get(str(entry.get("category") or "")[:1], "Other"),
            size_bytes=size_bytes,
            seeders=seeders,
            leechers=leechers,
            published_at=_timestamp(added),
            download_state="ready",
            download_kinds=("magnet",),
            magnet=magnet,
        )

    @staticmethod
    def _require_json_response(response) -> None:
        content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise IndexerInvalidResponse("The Pirate Bay returned a non-JSON response")

    @staticmethod
    def _parse_entries(body: bytes) -> list[dict]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IndexerInvalidResponse("The Pirate Bay returned malformed JSON") from exc
        if not isinstance(payload, list) or not all(isinstance(entry, dict) for entry in payload):
            raise IndexerInvalidResponse("The Pirate Bay JSON response must be a list of objects")
        return payload

    @staticmethod
    def _validate_status(status_code: int) -> None:
        if status_code == 429:
            raise IndexerRateLimited("The Pirate Bay returned HTTP 429")
        if status_code >= 500:
            raise IndexerUnavailable(f"The Pirate Bay returned HTTP {status_code}")
        if status_code != 200:
            raise IndexerInvalidResponse(f"The Pirate Bay returned HTTP {status_code}")


def _integer(value) -> int | None | object:
    if value is None:
        return None
    if isinstance(value, bool):
        return _INVALID_NUMBER
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return _INVALID_NUMBER
    return result if result >= 0 else _INVALID_NUMBER


def _timestamp(value: int | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
