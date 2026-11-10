"""原推荐行为迁至原生 catalog；复用公共收集器，不再调用 Agent 动作。"""
from __future__ import annotations
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Mapping, Callable, Any, cast
from unittest.mock import Mock, patch
import pytest
from app import sdk
from app import services
from app.clients.base import MediaIdentityCandidate, MediaIdentityInventory
from app.discovery.models import DiscoveryPage, MediaCard, ProviderHealth, ProviderTimeout, ProviderRateLimited
from app.discovery.registry import list_filter_definitions, validate_filters
def _card(index: int=1, **overrides) -> MediaCard:
    values = {'provider': 'tmdb', 'external_id': str(2000 + index), 'media_type': 'movie', 'title': f'推荐影片 {index}', 'original_title': f'Recommended Movie {index}', 'year': '2026', 'overview': '安全简介\x00' * 80, 'poster_key': 'https://image.example/poster?token=secret', 'backdrop_key': 'https://image.example/backdrop?api_key=secret', 'rating': 8.6, 'rating_source': 'tmdb', 'release_date': '2026-08-01', 'tmdb_id': str(2000 + index), 'state': 'watchlisted'}
    values.update(overrides)
    return MediaCard(**values)

class FakeDiscoveryService:

    def __init__(self, page: DiscoveryPage):
        self.page = page
        self.calls: list[tuple[str, str, str, int, dict]] = []

    def list_items(self, provider, category, media_type, page, filters):
        self.calls.append((provider, category, media_type, page, dict(filters)))
        return self.page

class PagedDiscoveryService(FakeDiscoveryService):

    def __init__(self, pages: Mapping[int, DiscoveryPage | Exception] | Callable[[int], DiscoveryPage | Exception], after_page=None):
        self.pages, self.after_page, self.calls = (pages, after_page, [])

    def list_items(self, provider, category, media_type, page, filters):
        self.calls.append((provider, category, media_type, page, dict(filters)))
        value = self.pages(page) if callable(self.pages) else self.pages[page]
        if isinstance(value, Exception):
            raise value
        if self.after_page:
            self.after_page(page)
        return value

def _page(items=(), *, number=1, has_more=False):
    return DiscoveryPage(items=tuple(items), page=number, has_more=has_more, provider=ProviderHealth(name='tmdb', status='healthy'))

@contextmanager
def _recommend_fixture(service, inventory=None):
    client = Mock()
    client.list_media_identity_inventory.return_value = inventory or MediaIdentityInventory(candidates=[], total=0)
    with patch('app.config.get_bool', return_value=True), patch('app.discovery.service.get_discovery_service', return_value=service), patch('app.services._configured_media_sources', return_value=[('jellyfin', 'Jellyfin', '', client)]):
        yield client

class NativeRecommendationTests(unittest.TestCase):

    def test_confirmed_douban_mapping_reaches_real_catalog_library_exclusion(self):
        from app.repositories import discovery as discovery_repo
        from app.discovery.registry import ProviderRegistry
        from app.discovery.service import DiscoveryService
        from tests.support import isolated_test_database

        with isolated_test_database():
            cards = tuple(_card(i, provider='douban', tmdb_id='') for i in range(1, 4))
            provider = Mock()
            provider.list_items.return_value = DiscoveryPage(items=cards, provider=ProviderHealth(name='douban'))
            service = DiscoveryService(registry=ProviderRegistry({'douban': provider}))
            self.addCleanup(service.shutdown)
            for i, confirmed in ((1, True), (2, True), (3, False)):
                discovery_repo.upsert_media_external_id('douban', str(2000 + i), 'movie', str(3000 + i), confirmed=confirmed)
            inventory = MediaIdentityInventory(candidates=[MediaIdentityCandidate(name='库内作品', tmdb_id='3002')], total=1)
            with _recommend_fixture(service, inventory):
                result = sdk.media.catalog('douban', 'recommend', 'movie', limit=1, exclude_library=True)
            self.assertEqual([(item['external_id'], item['tmdb_id']) for item in result['items']], [('2001', '3001')])
            self.assertEqual(result['library_check']['counts']['present'], 1)
            self.assertEqual(result['library_check']['unmapped_candidates'], 1)
            self.assertEqual([item.tmdb_id for item in cards], ['', '', ''])

    def test_source_catalog_exposes_filterable_collection_entry(self):
        from app.discovery.registry import list_section_definitions
        service = Mock()
        service.list_sections.return_value = list_section_definitions(douban_enabled=False)
        with _recommend_fixture(service), patch('app.discovery.registry.config.get_bool', return_value=False):
            sources = sdk.media.sources()
        self.assertEqual(sources['sections'], service.list_sections.return_value)
        catalog = next(item for item in sources['catalogs']
                       if item['provider'] == 'tmdb' and item['category'] == 'discover')
        self.assertTrue(catalog['supports_collection'])
        self.assertEqual(set(catalog['media_types']), {'movie', 'tv'})
        self.assertTrue(all(not item['enabled'] for item in sources['catalogs'] if item['provider'] == 'douban'))
        for item in sources['catalogs']:
            if not item['supports_collection']:
                continue
            for media_type in item['media_types']:
                service = PagedDiscoveryService({1: _page([])})
                with _recommend_fixture(service):
                    result = sdk.media.catalog(item['provider'], item['category'], media_type, limit=10)
                self.assertEqual(result['status'], 'empty')

    def test_discovered_country_and_date_filters_reach_catalog(self):
        for media_type, date_field in (('movie', 'release_date'), ('tv', 'first_air_date')):
            with self.subTest(media_type=media_type):
                definition = sdk.media.filters('tmdb', media_type)
                fields = {item['key']: item for item in definition['extra_filters']}
                filters = {'with_origin_country': fields['with_origin_country']['example'],
                           f'{date_field}.gte': fields[f'{date_field}.gte']['example'],
                           **{key: fields[key]['example'] for key in ('vote_count.gte', 'vote_average.gte', 'vote_average.lte')}}
                card = _card(media_type=media_type, vote_count=100, popularity=12.5)
                service = PagedDiscoveryService({1: _page([card])})
                with _recommend_fixture(service):
                    result = sdk.media.catalog('tmdb', definition['category'], media_type,
                                               filters=filters, limit=10, exclude_library=True)
                self.assertEqual(service.calls[0][4], {**filters, 'sort_by': 'popularity.desc'})
                self.assertEqual(result['items'], [{**card.to_dict(), 'library_status': 'missing'}])

    def test_catalog_accepts_genre_values_returned_by_filter_discovery(self):
        for media_type, label in (("movie", "动画"), ("tv", "动作冒险")):
            with self.subTest(media_type=media_type):
                definition = sdk.media.filters("tmdb", media_type)
                field = next(f for f in definition["filters"] if f["key"] == "with_genres")
                value = next(o["value"] for o in field["options"] if o["label"] == label)
                service = PagedDiscoveryService({1: _page([])})
                with _recommend_fixture(service):
                    sdk.media.catalog("tmdb", "discover", media_type, genre=value, region="CN",
                                      year_from="2016", limit=10, exclude_library=True)
                self.assertEqual(service.calls[0][4], {
                    "sort_by": "popularity.desc",
                    "with_genres": value, "with_origin_country": "CN",
                    ("release_date.gte" if media_type == "movie" else "first_air_date.gte"): "2016-01-01",
                })

    def test_recommendation_uses_shared_provider_genre_options(self):
        for provider, media_type, key in (('tmdb', 'movie', 'with_genres'), ('tmdb', 'tv', 'with_genres'), ('douban', 'movie', 'tags')):
            definition = list_filter_definitions(provider, media_type)
            choices = next((item['options'] for item in definition['filters'] if item['key'] == key))
            for option in choices:
                with self.subTest(provider=provider, media_type=media_type, genre=option['label']):
                    service = PagedDiscoveryService({1: _page([])})
                    with _recommend_fixture(service):
                        sdk.media.catalog(provider=provider, category='', media_type=media_type, limit=10, genre=option['label'])
                    self.assertEqual(service.calls[0][4][key], option['value'])

    def test_unsupported_constraints_cannot_be_silently_dropped(self):
        for arguments in ({'provider': 'douban', 'media_type': 'movie', 'region': '日本', 'genre': '科幻'}, {'provider': 'douban', 'media_type': 'tv', 'region': '日本', 'genre': '科幻'}, {'provider': 'douban', 'media_type': 'tv', 'region': '意大利'}, {'provider': 'tmdb', 'media_type': 'movie', 'region': '不存在的地区'}, {'provider': 'tmdb', 'media_type': 'movie', 'region': '欧美'}, {'provider': 'douban', 'media_type': 'tv', 'region': '欧美'}, {'provider': 'tmdb', 'media_type': 'tv', 'genre': '不存在的题材'}, {'provider': 'douban', 'media_type': 'movie', 'genre': '不存在的题材'}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    with _recommend_fixture(PagedDiscoveryService({})):
                        sdk.media.catalog(**cast(dict[str, Any], {'provider': 'tmdb', 'category': '', 'media_type': 'movie', 'limit': 10, **arguments}))
                service = PagedDiscoveryService({1: _page([_card()])})
                with _recommend_fixture(service), self.assertRaises(ValueError):
                    sdk.media.catalog(**cast(dict[str, Any], {'provider': 'tmdb', 'category': '', 'media_type': 'movie', 'limit': 10, **arguments}))
                self.assertEqual(service.calls, [])

    def test_tmdb_region_accepts_country_codes_and_common_names(self):
        for region, expected in (('意大利', 'IT'), ('it', 'IT'), ('ＥＳ', 'ES')):
            with self.subTest(region=region):
                service = PagedDiscoveryService({1: _page([])})
                with _recommend_fixture(service):
                    sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=10, region=region)
                self.assertEqual(service.calls[0][4]['with_origin_country'], expected)

    def test_supported_recommendation_aliases_keep_their_original_meaning(self):
        for provider, media_type, argument, expected in (('tmdb', 'movie', {'genre': '动漫'}, {'with_genres': '16'}), ('tmdb', 'tv', {'genre': '动作'}, {'with_genres': '10759'}), ('tmdb', 'tv', {'genre': '科幻'}, {'with_genres': '10765'}), ('tmdb', 'tv', {'genre': '历史'}, {'with_genres': '10768'}), ('douban', 'tv', {'region': '国产'}, {'tags': '国产剧'}), ('douban', 'tv', {'region': '日本'}, {'tags': '日剧'})):
            with self.subTest(provider=provider, media_type=media_type, argument=argument):
                service = PagedDiscoveryService({1: _page([])})
                with _recommend_fixture(service):
                    sdk.media.catalog(**cast(dict[str, Any], {'provider': 'tmdb', 'category': '', 'media_type': 'movie', 'limit': 10, **{'provider': provider, 'media_type': media_type, **argument}}))
                self.assertEqual(service.calls[0][4], validate_filters(provider, "discover" if provider == "tmdb" else "recommend", media_type, expected))

    def test_continuation_consumes_every_card_including_last_source_page_remainder(self):
        for exclude_library in (False, True):
            with self.subTest(exclude_library=exclude_library):
                service = PagedDiscoveryService({1: _page([_card(i) for i in range(1, 21)], has_more=True), 2: _page([_card(i) for i in range(21, 41)], number=2)})
                results = []
                arguments = {'exclude_library': exclude_library}
                with _recommend_fixture(service):
                    for _ in range(4):
                        result = sdk.media.catalog(**cast(dict[str, Any], {'provider': 'tmdb', 'category': '', 'media_type': 'movie', 'limit': 10, **arguments}))
                        self.assertEqual(result['returned'], 10)
                        results.extend((item['tmdb_id'] for item in result['items']))
                        scope = result['scope']
                        arguments.update(page=scope['next_page'], offset=scope.get('next_offset', 0))
                self.assertEqual(results, [str(2000 + i) for i in range(1, 41)])
                self.assertIsNone(scope['next_page'])
                self.assertFalse(result['has_more'])
                self.assertEqual([call[3] for call in service.calls], [1, 1, 2, 2])

    def test_continuation_offset_uses_source_positions_not_filtered_positions(self):
        service = PagedDiscoveryService({1: _page([_card(1, year='2010'), _card(2), _card(3), _card(3), _card(4, media_type='tv'), _card(5), _card(6)])})
        inventory = MediaIdentityInventory(candidates=[MediaIdentityCandidate(name='推荐影片 2', tmdb_id='2002', media_type='movie')], total=1)
        with _recommend_fixture(service, inventory):
            first = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=1, year_from='2020', exclude_library=True)
            self.assertEqual(first['scope']['next_page'], 1)
            second = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=2, year_from='2020', exclude_library=True, page=1, offset=first['scope']['next_offset'])
        self.assertEqual([i['tmdb_id'] for i in first['items']], ['2003'])
        self.assertEqual([i['tmdb_id'] for i in second['items']], ['2005', '2006'])
        self.assertIsNone(second['scope']['next_page'])

    def test_continuation_retries_failed_page_without_dropping_offset(self):
        service = PagedDiscoveryService({1: ProviderTimeout('timeout')})
        with _recommend_fixture(service):
            result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=10, page=1, offset=10)
        self.assertEqual(result['scope']['next_page'], 1)
        self.assertEqual(result['scope']['next_offset'], 10)

    def test_named_region_is_origin_country_not_original_language(self):
        for region, country in (('中国大陆', 'CN'), ('台湾', 'TW'), ('香港', 'HK'), ('英国', 'GB'), ('美国', 'US'), ('日本', 'JP')):
            for media_type in ('tv', 'movie'):
                with self.subTest(region=region, media_type=media_type):
                    service = PagedDiscoveryService({1: _page([])})
                    with _recommend_fixture(service):
                        sdk.media.catalog(provider='tmdb', category='', media_type=media_type, limit=10, region=region)
                    filters = validate_filters('tmdb', 'discover', media_type, service.calls[0][4])
                    self.assertEqual(filters['with_origin_country'], country)
                    self.assertNotIn('with_original_language', filters)

    def test_language_filter_stays_separate_from_origin_country(self):
        filters = validate_filters('tmdb', 'discover', 'tv', {'with_origin_country': 'cn', 'with_original_language': 'JA'})
        self.assertEqual(filters['with_origin_country'], 'CN')
        self.assertEqual(filters['with_original_language'], 'ja')
        for value in ('', 'CN|JP', '中国', 'C', 'CNA', '1A'):
            if value:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    validate_filters('tmdb', 'discover', 'tv', {'with_origin_country': value})

    def test_empty_front_pages_and_library_filter_still_fill_requested_number(self):
        service = PagedDiscoveryService({1: _page(has_more=True), 2: _page(number=2, has_more=True), 3: _page([_card(1), _card(2, year='2010'), _card(3)], number=3, has_more=True), 4: _page([_card(3), _card(4), _card(5)], number=4)})
        inventory = MediaIdentityInventory(candidates=[MediaIdentityCandidate(name='推荐影片 1', year='2026', tmdb_id='2001', media_type='movie')], total=1)
        with _recommend_fixture(service, inventory) as client:
            result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=3, year_from='2016', exclude_library=True)
        self.assertEqual([item['tmdb_id'] for item in result['items']], ['2003', '2004', '2005'])
        self.assertEqual(client.list_media_identity_inventory.call_count, 1)
        self.assertEqual([call[3] for call in service.calls], [1, 2, 3, 4])
        self.assertEqual(result['scope']['stop_reason'], 'target_reached')
        self.assertEqual(result['scope']['shortfall'], 0)
        self.assertTrue(result['scope']['complete'])
        self.assertTrue(all((item['library_status'] == 'missing' for item in result['items'])))
        self.assertEqual(result['library_check']['total'], 4)
        self.assertEqual(result['library_check']['counts'], {'present': 1, 'possible': 0, 'missing': 3, 'indeterminate': 0})
        self.assertEqual([item['tmdb_id'] for item in result['library_check']['items']], ['2001', '2003', '2004', '2005'])

    def test_candidates_are_deduplicated_before_counting_and_preserve_provider_order(self):
        service = PagedDiscoveryService({1: _page([_card(1)], has_more=True), 2: _page([_card(1), _card(2)], number=2, has_more=True), 3: _page([_card(3)], number=3)})
        with _recommend_fixture(service):
            result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=3)
        self.assertEqual([item['tmdb_id'] for item in result['items']], ['2001', '2002', '2003'])
        self.assertEqual(result['scope']['duplicates'], 1)
        self.assertEqual(result['scope']['pages_read'], [1, 2, 3])

    def test_real_last_page_reports_shortfall_without_inventing_next_page(self):
        service = PagedDiscoveryService({1: _page([_card(1)])})
        with _recommend_fixture(service):
            result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=3)
        scope = result['scope']
        self.assertEqual((scope['requested'], scope['returned'], scope['shortfall']), (3, 1, 2))
        self.assertEqual(scope['stop_reason'], 'source_exhausted')
        self.assertIsNone(scope['next_page'])
        self.assertFalse(scope['complete'])
        self.assertFalse(result['has_more'])

    def test_repeated_or_empty_pages_stop_at_bound_without_claiming_exhaustion(self):
        for items in ([], [_card(1)]):
            service = PagedDiscoveryService(lambda page, items=items: _page(items, number=page, has_more=True))
            with self.subTest(items=len(items)), _recommend_fixture(service):
                result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=3)
            self.assertEqual(len(service.calls), 8)
            self.assertEqual(result['scope']['stop_reason'], 'page_limit')
            self.assertEqual(result['scope']['next_page'], 9)
            self.assertFalse(result['scope']['complete'])
            self.assertTrue(result['has_more'])

    def test_source_page_hundred_is_never_followed_by_invalid_page_101(self):
        service = PagedDiscoveryService({100: _page([], number=100, has_more=True)})
        with _recommend_fixture(service):
            result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=10, page=100)
        self.assertEqual([call[3] for call in service.calls], [100])
        self.assertEqual(result['scope']['stop_reason'], 'page_limit')
        self.assertIsNone(result['scope']['next_page'])

    def test_unknown_and_truncated_inventory_never_become_missing(self):
        service = PagedDiscoveryService({1: _page([_card(1), _card(2, tmdb_id='')])})
        for inventory in (MediaIdentityInventory(candidates=[], total=10, truncated=True), RuntimeError('private outage')):
            with self.subTest(inventory=type(inventory).__name__), _recommend_fixture(service) as client:
                if isinstance(inventory, Exception):
                    client.list_media_identity_inventory.side_effect = inventory
                else:
                    client.list_media_identity_inventory.return_value = inventory
                result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=2, exclude_library=True)
            self.assertEqual(result['items'], [])
            self.assertEqual(result['library_check']['counts']['indeterminate'], 1)
            self.assertEqual(result['library_check']['unmapped_candidates'], 1)
            self.assertEqual(result['scope']['shortfall'], 2)
            self.assertEqual(result['status'], 'partial')
            self.assertNotIn('private outage', repr(result))

    def test_provider_failure_after_first_page_preserves_verified_partial_result(self):
        for error, reason in ((ProviderTimeout('源超时'), 'timeout'), (ProviderRateLimited('源限流', retry_after=12), 'provider_error')):
            service = PagedDiscoveryService({1: _page([_card(1)], has_more=True), 2: error})
            with self.subTest(reason=reason), _recommend_fixture(service):
                result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=3, exclude_library=True)
            self.assertEqual([item['tmdb_id'] for item in result['items']], ['2001'])
            self.assertEqual(result['scope']['pages_read'], [1])
            self.assertEqual(result['scope']['stop_reason'], reason)
            self.assertEqual(result['status'], 'partial')
            self.assertTrue(result['ok'])

    def test_cooperative_deadline_preserves_received_page_and_stops_more_io(self):
        clock = [0.0]
        service = PagedDiscoveryService(lambda page: _page([_card(page)], number=page, has_more=True), after_page=lambda _: clock.__setitem__(0, 21.0))
        with _recommend_fixture(service), patch('app.services.time.monotonic', side_effect=lambda: clock[0]):
            result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=3)
        self.assertEqual(result['returned'], 1)
        self.assertEqual([call[3] for call in service.calls], [1])
        self.assertEqual(result['scope']['stop_reason'], 'timeout')

    def test_deadline_during_local_matching_does_not_start_another_page(self):
        from app.services import summarize_media_identity_batch
        clock = [0.0]
        service = PagedDiscoveryService(lambda page: _page([_card(page)], number=page, has_more=True))

        def summarize(identities, sources):
            result = summarize_media_identity_batch(identities, sources)
            clock[0] = 21.0
            return result
        with _recommend_fixture(service), patch('app.services.time.monotonic', side_effect=lambda: clock[0]), patch('app.services.summarize_media_identity_batch', side_effect=summarize):
            result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=3, exclude_library=True)
        self.assertEqual([call[3] for call in service.calls], [1])
        self.assertEqual(result['returned'], 1)
        self.assertEqual(result['scope']['stop_reason'], 'timeout')

    def test_failed_page_hundred_remains_a_valid_retry_scope(self):
        service = PagedDiscoveryService({99: _page([_card(1)], number=99, has_more=True), 100: ProviderTimeout('源超时')})
        with _recommend_fixture(service):
            result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=3, page=99)
        self.assertEqual(result['scope']['pages_read'], [99])
        self.assertEqual(result['scope']['next_page'], 100)

    def test_independent_recommendation_requests_refresh_library_inventory(self):
        service = PagedDiscoveryService({1: _page([_card(1), _card(2)])})
        with _recommend_fixture(service) as client:
            results = []
            for existing in (1, 2):
                client.list_media_identity_inventory.return_value = MediaIdentityInventory(candidates=[MediaIdentityCandidate(name='库内作品', tmdb_id=str(2000 + existing))], total=1)
                results.append(sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=1, exclude_library=True))
        self.assertEqual([result['items'][0]['tmdb_id'] for result in results], ['2002', '2001'])
        self.assertEqual(client.list_media_identity_inventory.call_count, 2)

    def test_interrupted_inventory_preserves_resume_position(self):
        cases = [('cancelled', 1, True, 0), ('cancelled', 1, False, 0),
                 ('timeout', 1, True, 0), ('timeout', 1, False, 0),
                 ('timeout', 1, True, 1), ('timeout', 100, False, 0)]
        for reason, page, has_more, offset in cases:
            with self.subTest(reason=reason, page=page, has_more=has_more, offset=offset):
                clock, cancelled = [0.0], [False]

                def interrupt(_):
                    if reason == 'cancelled':
                        cancelled[0] = True
                    else:
                        clock[0] = 21.0

                service = PagedDiscoveryService({
                    page: _page([_card(1), _card(2)], number=page, has_more=has_more),
                    page + 1: _page([_card(3)], number=page + 1),
                }, after_page=interrupt)
                with _recommend_fixture(service) as client, \
                        sdk.call_owner('test', cancelled=lambda: cancelled[0]), \
                        patch('app.services.time.monotonic', side_effect=lambda: clock[0]):
                    result = sdk.media.catalog('tmdb', 'discover', 'movie', page=page, offset=offset,
                                               limit=2, exclude_library=True)
                    client.list_media_identity_inventory.assert_not_called()
                    self.assertEqual(result['scope']['stop_reason'], reason)
                    self.assertEqual(result['items'], [])
                    self.assertEqual(result['library_check']['counts']['indeterminate'], 2 - offset)
                    self.assertEqual((result['scope']['next_page'], result['scope']['next_offset']), (page, offset))
                    self.assertTrue(result['has_more'])
                    clock[0], cancelled[0], service.after_page = 0.0, False, None
                    resumed = sdk.media.catalog('tmdb', 'discover', 'movie',
                        page=result['scope']['next_page'], offset=result['scope']['next_offset'],
                        limit=2, exclude_library=True)
                expected = ['2001', '2002'] if not offset else ['2002', '2003']
                self.assertEqual([item['tmdb_id'] for item in resumed['items']], expected)
                self.assertTrue(all(item['library_status'] == 'missing' for item in resumed['items']))

    def test_cancel_during_inventory_keeps_verified_items_but_never_claims_complete(self):
        cancelled = [False]
        context = SimpleNamespace(owner='test', session_id='test', cancelled=lambda: cancelled[0])
        service = PagedDiscoveryService({1: _page([_card(1)], has_more=True)})

        def inventory(*_, **__):
            cancelled[0] = True
            return MediaIdentityInventory(candidates=[], total=0)
        with _recommend_fixture(service) as client, sdk.call_owner('test', cancelled=context.cancelled):
            client.list_media_identity_inventory.side_effect = inventory
            result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=1, exclude_library=True)
        self.assertEqual(result['returned'], 1)
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(result['scope']['complete'])
        self.assertEqual([call[3] for call in service.calls], [1])

    def test_collection_exceeds_old_display_limit_and_resumes_remaining_page(self):
        service = PagedDiscoveryService({
            page: _page([_card(index) for index in range((page - 1) * 20 + 1, page * 20 + 1)], number=page, has_more=True)
            for page in range(1, 4)
        })
        with _recommend_fixture(service) as client, patch('app.services.inspect_media_identity_batch', wraps=services.inspect_media_identity_batch) as inspect:
            result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=50, exclude_library=True)
            client.list_media_identity_inventory.assert_called_once()
        checked = [item['tmdb_id'] for call in inspect.call_args_list for item in call.args[0]]
        self.assertEqual(checked, [str(2000 + index) for index in range(1, 61)])
        self.assertEqual([item['tmdb_id'] for item in result['library_check']['items']], checked)
        self.assertEqual(result['library_check']['counts'], {'present': 0, 'possible': 0, 'missing': 60, 'indeterminate': 0})
        self.assertEqual([call[3] for call in service.calls], [1, 2, 3])
        self.assertEqual(result['returned'], 50)
        self.assertTrue(result['scope']['complete'])
        self.assertTrue(result['has_more'])
        self.assertEqual(result['scope']['next_page'], 3)
        self.assertEqual(result['scope']['next_offset'], 10)
        with _recommend_fixture(service):
            tail = sdk.media.catalog('tmdb', 'discover', 'movie', page=3, offset=10, limit=10, exclude_library=True)
        self.assertEqual([item['tmdb_id'] for item in result['items'] + tail['items']], [str(2000 + index) for index in range(1, 61)])

    def test_exclusion_never_turns_unknown_inventory_or_identity_into_missing(self):
        page = DiscoveryPage(items=(_card(1), _card(2, tmdb_id='')), page=1, has_more=True, provider=ProviderHealth(name='tmdb', status='healthy'))
        for sources in ([], [{'server_type': 'jellyfin', 'server_name': 'Jellyfin', 'status': 'unavailable', 'items': []}]):
            with patch('app.config.get_bool', return_value=True), patch('app.discovery.service.get_discovery_service', return_value=FakeDiscoveryService(page)), patch('app.services.inspect_media_identity_batch', return_value=sources):
                result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=10, exclude_library=True)
            self.assertEqual(result['items'], [])
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(result['library_check']['unmapped_candidates'], 1)
            self.assertEqual(result['library_check']['counts']['indeterminate'], 1)
        with patch('app.config.get_bool', return_value=True), patch('app.discovery.service.get_discovery_service', return_value=FakeDiscoveryService(page)), patch('app.services.inspect_media_identity_batch') as inspect:
            result = sdk.media.catalog(provider='tmdb', category='', media_type='movie', limit=10)
        inspect.assert_not_called()
        self.assertEqual(len(result['items']), 2)
        self.assertNotIn('library_check', result)


@pytest.mark.parametrize('changes', [
    {'provider': 'bangumi'}, {'category': 'popular'}, {'media_type': 'all'},
    {'page': True}, {'page': 101}, {'limit': 0}, {'limit': True}, {'offset': -1},
    {'offset': True}, {'year': '1899'}, {'year_from': '2025', 'year_to': '2016'},
    {'year': '2015', 'year_from': '2016'}, {'exclude_library': 'true'},
    {'region': '\0'}, {'genre': '\0'}, {'filters': {'unknown': '1'}},
    {'region': '日本', 'filters': {'with_origin_country': 'US'}},
])
def test_catalog_collection_rejects_constraints_before_any_io(changes):
    service = PagedDiscoveryService({})
    with _recommend_fixture(service), pytest.raises(ValueError):
        sdk.media.catalog(**cast(dict[str, Any], {'provider': 'tmdb', 'category': 'discover', 'media_type': 'movie', 'limit': 10, **changes}))
    assert service.calls == []


@pytest.mark.parametrize('fault,page,offset,has_more', [
    ('outage', 1, 0, True), ('unconfigured', 1, 0, True),
    ('truncated', 100, 1, False), ('cancelled', 1, 0, True),
    ('timeout', 1, 0, False), ('second_source', 1, 0, True),
])
def test_inventory_failure_stops_unusable_pages_and_resumes_unverified_candidates(fault, page, offset, has_more):
    stopped, clock = [False], [0.0]
    service = PagedDiscoveryService(lambda number: _page(
        [_card(1), _card(2), _card(3)], number=number, has_more=has_more))
    present = MediaIdentityCandidate(name='推荐影片 1', year='2026', tmdb_id='2001', media_type='movie')

    def incomplete(*_, **__):
        if fault in {'outage', 'second_source'}:
            raise RuntimeError('private outage')
        if fault == 'cancelled':
            stopped[0] = True
        if fault == 'timeout':
            clock[0] = 21.0
        return MediaIdentityInventory(candidates=[present], total=10, truncated=True)

    with _recommend_fixture(service) as client, \
            sdk.call_owner('test', cancelled=lambda: stopped[0]), \
            patch('app.services.time.monotonic', side_effect=lambda: clock[0]):
        client.list_media_identity_inventory.side_effect = incomplete
        with patch('app.services._configured_media_sources') as configured:
            sources = [('jellyfin', 'Jellyfin', '', client)]
            if fault == 'unconfigured':
                sources = []
            elif fault == 'second_source':
                healthy = Mock()
                healthy.list_media_identity_inventory.return_value = MediaIdentityInventory(candidates=[present], total=1)
                sources.insert(0, ('emby', 'Emby', '', healthy))
            configured.return_value = sources
            result = sdk.media.catalog('tmdb', 'discover', 'movie', page=page, offset=offset,
                                       limit=2, exclude_library=True)
        assert result['items'] == []
        assert [call[3] for call in service.calls] == [page]
        reason = fault if fault in {'cancelled', 'timeout'} else 'library_incomplete'
        assert result['scope']['stop_reason'] == reason
        resume_offset = 0 if fault in {'outage', 'unconfigured'} else 1
        assert (result['scope']['next_page'], result['scope']['next_offset']) == (page, resume_offset)
        assert result['has_more'] and not result['scope']['complete']
        assert result['status'] == ('cancelled' if fault == 'cancelled' else 'partial')
        assert 'private outage' not in str(result)
        if resume_offset and not offset:
            assert result['library_check']['counts']['present'] == 1

        stopped[0], clock[0] = False, 0.0
        client.list_media_identity_inventory.side_effect = None
        client.list_media_identity_inventory.return_value = MediaIdentityInventory(candidates=[present], total=1)
        resumed = sdk.media.catalog('tmdb', 'discover', 'movie', page=result['scope']['next_page'],
                                   offset=result['scope']['next_offset'], limit=2, exclude_library=True)
    assert [item['tmdb_id'] for item in resumed['items']] == ['2002', '2003']
    assert all(item['library_status'] == 'missing' for item in resumed['items'])


def test_catalog_cancel_before_collection_does_not_construct_service():
    with patch('app.config.get_bool', return_value=True), patch('app.discovery.service.get_discovery_service') as factory, sdk.call_owner('owner', cancelled=lambda: True):
        result = sdk.media.catalog('tmdb', 'discover', 'movie', limit=1)
    factory.assert_not_called()
    assert result['status'] == 'cancelled' and not result['scope']['complete']
    assert result['scope']['pages_read'] == []


def test_collection_keeps_full_results_and_inventory_evidence():
    overview = '完整概况' * 800
    service = PagedDiscoveryService({1: _page([_card(1, overview=overview)])})
    with _recommend_fixture(service):
        result = sdk.media.catalog('tmdb', 'discover', 'movie', limit=1, exclude_library=True)
    assert result['items'][0]['overview'] == overview
    assert len(result['library_check']['items']) == 1
    assert result['items'][0]['library_status'] == 'missing'
