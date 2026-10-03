"""官网真实同名候选与详情快照，覆盖关键词/媒体卡片共用召回链路。"""
from pathlib import Path
import unittest
from urllib.parse import unquote

from app.indexers.models import IndexerMediaSearchRequest, IndexerSearchRequest
from app.indexers.providers.btbtla import BTBtlaAdapter
from app.indexers.registry import IndexerRegistry
from app.indexers.service import IndexerService
from app.indexers.result_store import IndexerResultStore
from tests.test_indexer_providers import FakeHttpClient

_FIXTURES = Path(__file__).parent / 'fixtures' / 'indexers'
_SEARCH = (_FIXTURES / 'btbtla-homonyms-search.html').read_bytes()
_DETAIL = (_FIXTURES / 'btbtla-zhengtu-2026-detail.html').read_bytes()
_EMPTY = (_FIXTURES / 'btbtla-empty-search.html').read_bytes()
_MOVIE = '<div id="download-list"><div class="module-row-info"><a class="module-row-text" href="/tdown/movie.html">征途.Double.World.2020.1080p [1GiB]</a></div></div>'.encode()


class BTBtlaRecallTests(unittest.IsolatedAsyncioTestCase):
    async def test_title_search_does_not_stop_after_first_homonymous_movie(self):
        http = FakeHttpClient(_EMPTY)
        http.responses = [_SEARCH, _MOVIE, _DETAIL]
        result = await BTBtlaAdapter(http=http).search(IndexerSearchRequest.create('征途'))
        self.assertEqual(len(result.items), 22)
        self.assertTrue(any('2020' in item.title for item in result.items))
        self.assertEqual(sum('2026' in item.title for item in result.items), 21)
        self.assertEqual(len(http.calls), 3)
        self.assertTrue(http.calls[2]['url'].endswith('/detail/48532442.html'))

    async def test_year_selects_later_homonym_without_requesting_old_movie(self):
        http = FakeHttpClient(_EMPTY)
        http.responses = [_SEARCH, _DETAIL]
        result = await BTBtlaAdapter(http=http).search(IndexerSearchRequest.create('征途', year=2026))
        self.assertEqual(len(result.items), 21)
        self.assertTrue(all('2026' in item.title for item in result.items))
        self.assertEqual(len(http.calls), 2)
        self.assertTrue(http.calls[1]['url'].endswith('/detail/48532442.html'))

    async def test_raw_trailing_year_retries_title_only_after_explicit_empty(self):
        for query in ('征途2026', '征途 2026', '征途（2026）', '征途 (2026)', '征途2026年'):
            with self.subTest(query=query):
                http = FakeHttpClient(_EMPTY)
                http.responses = [_EMPTY, _SEARCH, _DETAIL]
                result = await BTBtlaAdapter(http=http).search(IndexerSearchRequest.create(query))
                self.assertEqual(len(result.items), 21)
                self.assertTrue(all('2026' in item.title for item in result.items))
                self.assertEqual(unquote(http.calls[0]['url']).split('/search/')[1], IndexerSearchRequest.create(query).query)
                self.assertTrue(unquote(http.calls[1]['url']).endswith('/search/征途'))
                self.assertTrue(http.calls[2]['url'].endswith('/detail/48532442.html'))

    async def test_numeric_titles_and_exact_numbered_titles_are_not_rewritten(self):
        for query in ('1917', '2001', '银翼杀手2049', 'Blade Runner 2049'):
            with self.subTest(query=query):
                search = f'<div class="module-item"><a class="module-item-title" href="/detail/exact.html">{query}</a><div class="module-item-caption"><span>2017</span></div></div>'.encode()
                http = FakeHttpClient(_EMPTY)
                http.responses = [search, _MOVIE]
                await BTBtlaAdapter(http=http).search(IndexerSearchRequest.create(query))
                self.assertEqual(len(http.calls), 2)
                self.assertTrue(http.calls[1]['url'].endswith('/detail/exact.html'))
        http = FakeHttpClient(_EMPTY)
        await BTBtlaAdapter(http=http).search(IndexerSearchRequest.create('1917'))
        self.assertEqual(len(http.calls), 1)

    async def test_known_year_mismatch_stays_empty_without_loading_wrong_detail(self):
        http = FakeHttpClient(_SEARCH)
        result = await BTBtlaAdapter(http=http).search(IndexerSearchRequest.create('征途', year=2025))
        # 官网已有同名候选，不准退到其它名字/年份未知的弱匹配。
        self.assertFalse(any('/detail/10769324.html' in call['url'] or '/detail/48532442.html' in call['url'] for call in http.calls))
        self.assertEqual(result.items, [])
        self.assertEqual(len(http.calls), 1)

    async def test_unknown_year_is_retained_and_duplicate_details_are_not_retrieved_twice(self):
        node = '<div class="module-item"><a class="module-item-title" href="/detail/unknown.html">征途</a></div>'
        http = FakeHttpClient(_EMPTY)
        http.responses = [(node + node).encode(), _DETAIL]
        result = await BTBtlaAdapter(http=http).search(IndexerSearchRequest.create('征途', year=2026))
        self.assertEqual(len(result.items), 21)
        self.assertEqual(len(http.calls), 2)

    async def test_known_matching_year_does_not_expand_weaker_unknown_year_after_success(self):
        unknown = '<div class="module-item"><a class="module-item-title" href="/detail/unknown.html">征途</a></div>'.encode()
        http = FakeHttpClient(_EMPTY)
        http.responses = [unknown + _SEARCH, _DETAIL]
        result = await BTBtlaAdapter(http=http).search(IndexerSearchRequest.create('征途', year=2026))
        self.assertEqual(len(result.items), 21)
        self.assertEqual(len(http.calls), 2)
        self.assertTrue(http.calls[1]['url'].endswith('/detail/48532442.html'))

    async def test_year_fallback_preserves_native_pagination(self):
        search = _SEARCH + '<a href="/search/征途/3">下一页</a>'.encode()
        http = FakeHttpClient(_EMPTY)
        http.responses = [_EMPTY, search, _DETAIL]
        result = await BTBtlaAdapter(http=http).search(IndexerSearchRequest.create('征途 2026', page=2))
        self.assertEqual(result.page, 2)
        self.assertTrue(result.has_more)
        self.assertTrue(unquote(http.calls[1]['url']).endswith('/search/征途/2'))

    async def test_mirror_preserves_same_homonym_and_year_selection_as_primary(self):
        for query, responses, expected_count in (
            ('征途', [_SEARCH, _MOVIE, _DETAIL], 22),
            ('征途2026', [_EMPTY, _SEARCH, _DETAIL], 21),
        ):
            with self.subTest(query=query):
                http = FakeHttpClient(_EMPTY)
                http.responses = [b'<html>invalid primary endpoint</html>', *responses]
                result = await BTBtlaAdapter(http=http).search(IndexerSearchRequest.create(query))
                self.assertEqual(len(result.items), expected_count)
                self.assertTrue(any('2026' in item.title for item in result.items))
                self.assertEqual(len(http.calls), 4)
                self.assertTrue(all(call['url'].startswith('https://btbtlb.com/') for call in http.calls[1:]))
                self.assertTrue(all(item.detail_url.startswith('https://btbtlb.com/') for item in result.items))

    async def test_shared_service_keeps_year_context_and_caches_correct_resources(self):
        http = FakeHttpClient(_EMPTY)
        http.responses = [_SEARCH, _DETAIL]
        service = IndexerService(registry=IndexerRegistry({'btbtla': BTBtlaAdapter(http=http)}), result_store=IndexerResultStore())
        try:
            request = IndexerMediaSearchRequest.create(title='征途', year=2026, media_type='tv')
            result = await service.search_media(request, ('btbtla',))
            self.assertTrue(result.items)
            self.assertTrue(all('2026' in item.title for item in result.items))
            self.assertTrue(all(item.result_id for item in result.items))
            self.assertFalse(result.errors)
            cached = await service.search_media(request, ('btbtla',))
            self.assertTrue(cached.cached)
            self.assertEqual(len(http.calls), 2)
        finally:
            await service.aclose()
