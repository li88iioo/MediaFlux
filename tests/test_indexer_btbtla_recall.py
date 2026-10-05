"""官网真实同名候选与详情快照，覆盖关键词/媒体卡片共用召回链路。"""
from pathlib import Path
import unittest
import asyncio
from bs4 import BeautifulSoup
from app.indexers.errors import IndexerResultExpired, IndexerUnavailable
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

    @staticmethod
    def catalog_html(count, *, prefix="Demo", next_native=False):
        search = f'<div class="module-item"><a class="module-item-title" href="/detail/demo.html">{prefix}</a></div>'
        if next_native:
            search += f'<a href="/search/{prefix}/2">下一页</a>'
        rows = ''.join(f'<div class="module-row-info"><a class="module-row-text" href="/tdown/{i}.html">{prefix} S01E{i:02d} [1GiB]</a></div>' for i in range(count, 0, -1))
        return search.encode(), ('<div id="download-list">' + rows + '</div>').encode()

    async def test_resource_pages_keep_all_old_episodes_without_refetching(self):
        search, detail = self.catalog_html(95)
        http = FakeHttpClient(search)
        http.responses = [search, detail]
        adapter = BTBtlaAdapter(http=http, page_size=40)
        pages = [await adapter.search(IndexerSearchRequest.create('Demo', page=i, sort_mode='episode_desc')) for i in range(1, 4)]
        self.assertEqual([len(page.items) for page in pages], [40, 40, 15])
        self.assertEqual([page.has_more for page in pages], [True, True, False])
        self.assertEqual(len({item.detail_url for page in pages for item in page.items}), 95)
        self.assertIn('S01E01', pages[-1].items[-1].title)
        self.assertEqual(len(http.calls), 2)
        pages[0].items[0].title = 'caller mutation'
        repeated = await adapter.search(IndexerSearchRequest.create('Demo', sort_mode='episode_desc'))
        self.assertNotEqual(repeated.items[0].title, 'caller mutation')

    async def test_requested_early_episode_is_ranked_before_windowing(self):
        search, detail = self.catalog_html(95)
        http = FakeHttpClient(search)
        http.responses = [search, detail]
        adapter = BTBtlaAdapter(http=http, page_size=10)
        page = await adapter.search(IndexerSearchRequest.create('Demo', season=1, episode=1))
        self.assertIn('S01E01', page.items[0].title)
        self.assertIn('episode_exact', page.items[0].match_reasons)
        self.assertEqual(len(page.items), 10)
        self.assertTrue(page.has_more)

    async def test_native_search_page_waits_until_catalog_windows_exhausted(self):
        search, detail = self.catalog_html(3, next_native=True)
        second_search, second_detail = self.catalog_html(1, prefix='Demo Second')
        second_detail = second_detail.replace(b'/tdown/1.html', b'/tdown/second.html')
        http = FakeHttpClient(search)
        http.responses = [search, detail, second_search, second_detail]
        adapter = BTBtlaAdapter(http=http, page_size=2)
        first = await adapter.search(IndexerSearchRequest.create('Demo'))
        second = await adapter.search(IndexerSearchRequest.create('Demo', page=2))
        self.assertEqual(len(http.calls), 2)
        self.assertEqual([len(first.items), len(second.items)], [2, 1])
        self.assertTrue(second.has_more)
        third = await adapter.search(IndexerSearchRequest.create('Demo', page=3))
        self.assertEqual(third.items[0].detail_url, 'https://www.btbtlb.com/tdown/second.html')
        self.assertFalse(third.has_more)
        self.assertTrue(http.calls[2]['url'].endswith('/search/Demo/2'))

    async def test_site_category_filter_is_not_a_title_keyword_filter(self):
        tabs = ''.join(f'<div class="downtab-item"><span data-dropdown-value="{label}">{label}</span></div>' for label in ('1080p','720p','480p','Other','2160p'))
        panels = ''.join(f'<div class="module-downlist"><div class="module-row-one"><div class="module-row-info"><a class="module-row-text" title="《演示》English Release" href="/tdown/{i}.html">The Other Side S01E01 [1.5GiB] [纯净版]</a></div></div></div>' for i in range(5))
        adapter = BTBtlaAdapter(http=FakeHttpClient(b''))
        items = adapter._parse_resource_items(BeautifulSoup('<div id="download-list">'+tabs+panels+'</div>','lxml'), category='Drama', base_url=adapter.base_url)
        self.assertEqual([item.detail_url for item in items], ['https://www.btbtlb.com/tdown/0.html','https://www.btbtlb.com/tdown/4.html'])
        self.assertTrue(all(item.size_bytes == int(1.5 * 1024**3) for item in items))
        self.assertTrue(all(item.title == '演示 / The Other Side S01E01  [纯净版]' for item in items))

    async def test_service_can_continue_catalog_with_fresh_execution_references(self):
        search, detail = self.catalog_html(5)
        http = FakeHttpClient(search)
        http.responses = [search, detail]
        service = IndexerService(registry=IndexerRegistry({'btbtla': BTBtlaAdapter(http=http, page_size=2)}), result_store=IndexerResultStore(), max_results_per_site=2)
        try:
            pages = [await service.search('Demo', page=i, site_ids=('btbtla',), sort_mode='episode_desc') for i in (1,2,3)]
            self.assertEqual([len(page.items) for page in pages], [2,2,1])
            self.assertEqual([page.has_more for page in pages], [True,True,False])
            self.assertEqual(len({item.result_id for page in pages for item in page.items}),5)
            self.assertEqual(len(http.calls),2)
            self.assertTrue(all(page.site_truncated_counts['btbtla']==0 for page in pages))
            cached=await service.search('Demo', page=2, site_ids=('btbtla',), sort_mode='episode_desc')
            self.assertTrue(cached.cached)
            self.assertNotEqual(cached.items[0].result_id,pages[1].items[0].result_id)
        finally:
            await service.aclose()

    async def test_failed_following_native_page_does_not_erase_valid_windows(self):
        search, detail = self.catalog_html(1, next_native=True)
        http = FakeHttpClient(search)
        http.responses = [search, detail, b'invalid', b'invalid']
        adapter = BTBtlaAdapter(http=http)
        first = await adapter.search(IndexerSearchRequest.create('Demo'))
        with self.assertRaises(IndexerUnavailable):
            await adapter.search(IndexerSearchRequest.create('Demo', page=2))
        repeated = await adapter.search(IndexerSearchRequest.create('Demo'))
        self.assertEqual(first.items[0].detail_url,repeated.items[0].detail_url)
        self.assertTrue(repeated.has_more)
        self.assertEqual(len(http.calls),4)

    async def test_deep_cold_page_does_not_crawl_unbounded_native_pages(self):
        search, detail = self.catalog_html(1, next_native=True)
        second = search.replace(b'/search/Demo/2',b'/search/Demo/3')
        http = FakeHttpClient(search)
        http.responses = [search,detail,second,detail]
        adapter = BTBtlaAdapter(http=http)
        with self.assertRaises(IndexerResultExpired):
            await adapter.search(IndexerSearchRequest.create('Demo', page=99))
        self.assertEqual(len(http.calls),4)

    async def test_catalog_cache_expires_and_concurrent_requests_share_fetch(self):
        search, detail = self.catalog_html(3)
        clock=[0.0]
        http=FakeHttpClient(search)
        http.responses=[search,detail,search,detail]
        adapter=BTBtlaAdapter(http=http, page_size=2, cache_ttl_seconds=2, monotonic=lambda:clock[0])
        first, second=await asyncio.gather(adapter.search(IndexerSearchRequest.create('Demo')),adapter.search(IndexerSearchRequest.create('Demo',page=2)))
        self.assertEqual(len(http.calls),2)
        self.assertEqual([len(first.items),len(second.items)],[2,1])
        clock[0]=3
        await adapter.search(IndexerSearchRequest.create('Demo'))
        self.assertEqual(len(http.calls),4)

    async def test_empty_native_page_is_skipped_within_read_budget(self):
        search, empty = self.catalog_html(0, next_native=True)
        second_search, second_detail = self.catalog_html(1)
        http=FakeHttpClient(search)
        http.responses=[search,empty,second_search,second_detail]
        adapter=BTBtlaAdapter(http=http)
        result=await adapter.search(IndexerSearchRequest.create('Demo'))
        self.assertEqual(len(result.items),1)
        self.assertFalse(result.has_more)
        self.assertEqual(len(http.calls),4)

    async def test_catalog_never_retains_unrequestable_windows(self):
        search, detail = self.catalog_html(105)
        http=FakeHttpClient(search)
        http.responses=[search,detail]
        adapter=BTBtlaAdapter(http=http,page_size=1)
        await adapter.search(IndexerSearchRequest.create('Demo',sort_mode='source_order'))
        last=await adapter.search(IndexerSearchRequest.create('Demo',page=100,sort_mode='source_order'))
        self.assertFalse(last.has_more)
        self.assertEqual(len(last.items),1)
        self.assertEqual(last.total_items,6)
        catalog=next(iter(adapter._catalogs.values()))
        self.assertEqual(len(catalog.pages),100)
        self.assertEqual(sum(map(len,catalog.pages)),100)
        service=IndexerService(registry=IndexerRegistry({'btbtla':adapter}),result_store=IndexerResultStore(),max_results_per_site=1)
        try:
            result=await service.search('Demo',page=100,site_ids=('btbtla',),sort_mode='source_order')
            self.assertEqual(result.site_truncated_counts['btbtla'],5)
        finally:
            await service.aclose()

    async def test_cancelled_fetch_does_not_publish_or_lock_partial_catalog(self):
        search, detail=self.catalog_html(3)
        entered=asyncio.Event()
        class Blocking(FakeHttpClient):
            async def get(self,url,**kwargs):
                if '/detail/' in url and not entered.is_set():
                    entered.set()
                    await asyncio.Event().wait()
                return await super().get(url,**kwargs)
        http=Blocking(search);http.responses=[search,search,detail]
        adapter=BTBtlaAdapter(http=http,page_size=2)
        task=asyncio.create_task(adapter.search(IndexerSearchRequest.create('Demo')))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(adapter._catalogs)
        result=await adapter.search(IndexerSearchRequest.create('Demo'))
        self.assertEqual(len(result.items),2)
        self.assertTrue(result.has_more)

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
        result = await BTBtlaAdapter(http=http)._search_native(IndexerSearchRequest.create('征途 2026', page=2))
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
