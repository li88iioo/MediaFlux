from __future__ import annotations

import asyncio
import json
import unittest

from app.indexers.errors import IndexerInvalidResponse, IndexerUnavailable
from app.indexers.http import IndexerHttpResponse
from app.indexers.models import IndexerSearchRequest
from app.indexers.providers.aipan import AipanAdapter

AIPAN_SEARCH = "https://www.aipan.me/api/movies/search"
HASH_ONE = "0123456789abcdef0123456789abcdef01234567"
HASH_TWO = "abcdef0123456789abcdef0123456789abcdef01"


def response(url: str, body: bytes | str, *, status: int = 200, content_type: str = "application/json", headers=None):
    merged_headers = {"content-type": content_type, **(headers or {})}
    return IndexerHttpResponse(
        url=url,
        status_code=status,
        headers=merged_headers,
        body=body.encode() if isinstance(body, str) else body,
    )


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        route = self.routes[url]
        result = route(url, kwargs) if callable(route) else route
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result


class AipanAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_release_specs_and_appends_work_title_without_duplication(self):
        search = response(AIPAN_SEARCH, json.dumps({"movies": [
            {"id": 1, "title": "三体"},
        ]}))
        detail_url = "https://www.aipan.me/api/movies/detail/1"
        magnet_one = f"magnet:?xt=urn:btih:{HASH_ONE}&dn=The.Three.Body.Problem.S01E02.1080p"
        magnet_two = f"magnet:?xt=urn:btih:{HASH_TWO}&dn=三体.S01E03.2160p"
        detail = response(detail_url, json.dumps({"resources": [
            {"kind": "magnet", "name": "The Three Body Problem S01E02 1080p WEB-DL", "url": magnet_one},
            {"kind": "magnet", "name": "三体 S01E03 2160p", "url": magnet_two},
        ]}))
        adapter = AipanAdapter(http=FakeHttp({AIPAN_SEARCH: search, detail_url: detail}))

        page = await adapter.search(IndexerSearchRequest.create("三体"))

        self.assertEqual(len(page.items), 2)
        first, second = page.items
        self.assertEqual(first.title, "The Three Body Problem S01E02 1080p WEB-DL — 三体")
        self.assertIn("S01E02 1080p WEB-DL", first.title)
        self.assertEqual(second.title, "三体 S01E03 2160p")
        self.assertEqual(first.detail_url, "https://www.aipan.me/movie/1")
        self.assertEqual(first.site_id, "aipan")
        self.assertEqual(first.magnet, magnet_one)
        self.assertEqual(second.magnet, magnet_two)
        self.assertEqual(first.download_state, "ready")
        self.assertEqual(first.download_kinds, ("magnet",))

    async def test_ignores_invalid_magnets_but_keeps_multiple_valid_hashes_per_detail(self):
        search = response(AIPAN_SEARCH, '{"movies":[{"id":2,"title":"作品"}]}')
        detail_url = "https://www.aipan.me/api/movies/detail/2"
        detail = response(detail_url, json.dumps({"resources": [
            {"kind": "magnet", "name": "发行一 1080p", "url": f"magnet:?xt=urn:btih:{HASH_ONE}"},
            {"kind": "magnet", "name": "发行二 720p", "url": f"magnet:?xt=urn:btih:{HASH_TWO}"},
            {"kind": "magnet", "name": "坏磁力", "url": "magnet:?xt=urn:btih:bad"},
            {"kind": "pan", "name": "网盘", "url": "https://example.invalid/share"},
        ]}))
        adapter = AipanAdapter(http=FakeHttp({AIPAN_SEARCH: search, detail_url: detail}))

        page = await adapter.search(IndexerSearchRequest.create("作品"))

        self.assertEqual([item.title for item in page.items], ["发行一 1080p — 作品", "发行二 720p — 作品"])
        self.assertEqual(len({item.detail_url for item in page.items}), 1)

    async def test_empty_search_and_unsupported_page_are_distinct_without_extra_requests(self):
        empty = response(AIPAN_SEARCH, '{"movies":[]}')
        http = FakeHttp({AIPAN_SEARCH: empty})
        adapter = AipanAdapter(http=http)

        page = await adapter.search(IndexerSearchRequest.create("不存在"))
        self.assertEqual(page.items, [])
        self.assertEqual(page.errors, ())
        self.assertFalse(page.pagination_supported)

        later = await adapter.search(IndexerSearchRequest.create("不存在", page=2))
        self.assertEqual(later.items, [])
        self.assertEqual(later.page, 2)
        self.assertEqual(len(http.calls), 1)

    async def test_malformed_json_and_challenge_raise_instead_of_becoming_empty(self):
        adapter = AipanAdapter(http=FakeHttp({
            AIPAN_SEARCH: response(AIPAN_SEARCH, "not-json"),
        }))
        with self.assertRaises(IndexerInvalidResponse):
            await adapter.search(IndexerSearchRequest.create("三体"))

        adapter = AipanAdapter(http=FakeHttp({
            AIPAN_SEARCH: response(
                AIPAN_SEARCH,
                "<html>Just a moment</html>",
                content_type="text/html",
                headers={"cf-mitigated": "challenge"},
            ),
        }))
        with self.assertRaises(IndexerUnavailable):
            await adapter.search(IndexerSearchRequest.create("三体"))

    async def test_partial_detail_failure_is_safe_and_total_failure_raises(self):
        search = response(AIPAN_SEARCH, '{"movies":[{"id":3,"title":"三体"},{"id":4,"title":"三体2"}]}')
        failed_url = "https://www.aipan.me/api/movies/detail/3"
        good_url = "https://www.aipan.me/api/movies/detail/4"
        magnet = f"magnet:?xt=urn:btih:{HASH_ONE}"
        http = FakeHttp({
            AIPAN_SEARCH: search,
            failed_url: RuntimeError("https://secret.invalid/?token=private"),
            good_url: response(good_url, json.dumps({"resources": [
                {"kind": "magnet", "name": "三体2 S01E01", "url": magnet},
            ]})),
        })
        page = await AipanAdapter(http=http).search(IndexerSearchRequest.create("三体"))
        self.assertEqual(len(page.items), 1)
        self.assertEqual(len(page.errors), 1)
        self.assertEqual(page.errors[0].site_id, "btbtla")
        self.assertIn("Aipan", page.errors[0].message)
        self.assertNotIn("secret.invalid", page.errors[0].message)
        self.assertNotIn("private", page.errors[0].message)

        only_one = response(AIPAN_SEARCH, '{"movies":[{"id":3,"title":"三体"}]}')
        with self.assertRaises(IndexerUnavailable):
            await AipanAdapter(http=FakeHttp({AIPAN_SEARCH: only_one, failed_url: RuntimeError("broken")})).search(
                IndexerSearchRequest.create("三体")
            )

    async def test_detail_candidates_are_capped_and_canceled_tasks_are_reaped(self):
        movies = [{"id": index, "title": f"作品{index}"} for index in range(1, 5)]
        routes = {AIPAN_SEARCH: response(AIPAN_SEARCH, json.dumps({"movies": movies}))}
        started = asyncio.Event()
        release = asyncio.Event()
        tracker = {"active": 0, "max": 0}

        async def slow_detail(url, _kwargs):
            tracker["active"] += 1
            tracker["max"] = max(tracker["max"], tracker["active"])
            started.set()
            try:
                await release.wait()
                return response(url, '{"resources":[]}')
            finally:
                tracker["active"] -= 1

        for index in range(1, 5):
            routes[f"https://www.aipan.me/api/movies/detail/{index}"] = slow_detail
        adapter = AipanAdapter(http=FakeHttp(routes))
        task = asyncio.create_task(adapter.search(IndexerSearchRequest.create("作品")))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(tracker["active"], 0)
        self.assertLessEqual(tracker["max"], 3)
