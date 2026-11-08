"""原生资源展示：真实SDK文件RPC、持久状态及原Web/TG卡片消费者。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from fastapi import Request

from app import sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.ports.resource_presentation import ResourceSelection
from app.agent.domains.resources.selection import current_candidate_view, validate_selection
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.sdk_effects import resource_download_tool
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import CancellationToken, StateUpdate
from app.agent.kernel.tool_calls import ToolCallContext
from app.indexers.models import AggregatedIndexerResult, IndexerItem
from app.indexers.providers.nyaa import NyaaAdapter
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.service import IndexerService
from tests.resource_presentation_test_harness import rpc_call
from tests.support import isolated_test_database

SECRET = 'native-resource-presentation-secret'


class FixtureIndexer(IndexerService):
    test_count = 2


@pytest.fixture
def native(tmp_path, monkeypatch):
    with isolated_test_database():
        service = FixtureIndexer(registry=IndexerRegistry({'nyaa': NyaaAdapter(
            site_id='nyaa', site_name='Nyaa', base_url='https://fixture.invalid',
            http=NS(), default_enabled=True)}), result_store=IndexerResultStore())
        service.test_count = 2
        async def search(request, site_ids=None, **kwargs):
            items = []
            for number in range(1, service.test_count + 1):
                item = IndexerItem('nyaa', 'Nyaa', f'{request.title}.S01E{number:02d}.1080p.WEB-DL',
                    download_state='ready', download_kinds=('magnet',),
                    magnet='magnet:?xt=urn:btih:' + f'{number:040x}')
                identity = service.result_store.put(item)
                items.append(replace(item, result_id=identity))
            return AggregatedIndexerResult(query=request.title, page=request.page, items=items,
                sites_attempted=('nyaa',), sites_succeeded=('nyaa',), has_more=True, partial=True)
        monkeypatch.setattr(service, 'search_media', search)
        monkeypatch.setattr('app.indexers.runtime.get_indexer_service', lambda: service)
        monkeypatch.setattr('app.agent.kernel.ports.sdk_effects.get_indexer_service', lambda: service)
        monkeypatch.setattr('app.agent.domains.resources.selection._target_options', lambda owner: {
            'target': 'guangya', 'target_source': 'default', 'targets': [
                {'value': name, 'label': name, 'available': True} for name in ('guangya', 'qb', 'both')]})
        monkeypatch.setattr(sandbox, 'get_runtime_paths', lambda: NS(data_dir=tmp_path, runtime_dir=tmp_path / 'runtime'))
        store = SQLiteKernelStore(secret_provider=lambda: SECRET)
        def pipeline(current_store=store):
            catalog = ToolCatalog([resource_download_tool()], selection=ResourceSelection())
            executor = ToolPipeline(catalog=catalog, state_store=current_store, reference_store=current_store,
                effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
            catalog.register(sandbox.run_code_tool(executor))
            return executor
        yield NS(service=service, store=store, pipeline=pipeline, root=tmp_path)
        asyncio.run(service.aclose())


async def context_for(store, *, owner='presentation-owner', session_id='presentation_session_1234'):
    lease, _ = await store.begin_turn(owner=owner, session_id=session_id, request_id='native')
    return ToolCallContext(owner=owner, session_id=session_id, request_id='native', turn_id=lease.turn_id,
        lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())


async def read(pipeline, context, title='Example', **arguments):
    response, _ = await rpc_call(pipeline, context, 'resources.search', {'title': title, **arguments})
    assert 'error' not in response, response
    return response['result']


async def display(pipeline, context, identities, **arguments):
    response, rpc = await rpc_call(pipeline, context, 'resources.present', {'result_ids': identities, **arguments})
    return response, rpc.presentation.view


def test_repeated_candidate_reads_load_one_snapshot_and_keep_independent_values(native, monkeypatch):
    import json
    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        native.service.test_count = 1000
        _, rpc = await rpc_call(pipeline, context, 'resources.search', {'title': 'Example'})
        original = native.store._resolve_ref_sync
        counts = {'reads': 0, 'chars': 0}
        def counted(*args, **kwargs):
            value, deadline = original(*args, **kwargs)
            counts['reads'] += 1
            counts['chars'] += len(value if isinstance(value, str) else json.dumps(value))
            return value, deadline
        monkeypatch.setattr(native.store, '_resolve_ref_sync', counted)
        for _ in range(5):
            snapshot = await rpc.presentation.snapshot()
            assert snapshot and len(snapshot['candidates']) == 1000
            assert snapshot['candidates'][0]['title'].startswith('Example')
            size = len(json.dumps(snapshot, ensure_ascii=False))
            snapshot['candidates'][0]['title'] = 'caller mutation'
        print(f'candidate snapshot reads: {counts}')
        assert counts['chars'] < 3 * size, counts
        await read(pipeline, context, 'Second')
        updated = await rpc.presentation.snapshot()
        assert updated and len(updated['candidates']) == 2000
        assert updated['candidates'][-1]['title'].startswith('Second')
    asyncio.run(exercise())


def test_episode_card_uses_matching_results_and_can_rerank_observed_candidates(native, monkeypatch):
    titles = ['Black.Mirror.S01E01.1080p', 'Black.Mirror.S01E02.1080p',
              'Black.Mirror.S03.COMPLETE.1080p', 'Black.Mirror.1080p',
              'Black.Mirror.S01E01-E03.1080p', 'Unrelated.S01E01.1080p']
    async def search(request, site_ids=None, **kwargs):
        items = []
        for number, title in enumerate(titles):
            item = IndexerItem('nyaa', 'Nyaa', title, download_state='ready',
                download_kinds=('magnet',), magnet='magnet:?xt=urn:btih:' + f'{number:040x}')
            items.append(replace(item, result_id=native.service.result_store.put(item)))
        return AggregatedIndexerResult(query=request.title, page=1, items=items,
            sites_attempted=('nyaa',), sites_succeeded=('nyaa',), partial=True, complete=False, has_more=True)
    monkeypatch.setattr(native.service, 'search_media', search)
    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        result = await read(pipeline, context, 'Black Mirror', season=1, episode=1, resolution='1080p')
        ids = [item['result_id'] for item in result['items']]
        assert len(ids) == 6  # 原始结果仍可研究，包含其它集、待映射项与其它作品。
        response, view = await display(pipeline, context, ids)
        expected = [item['result_id'] for item in result['items']
                    if item['quality']['eligible'] and item['quality']['match'] in {'exact_episode', 'episode_pack'}]
        assert response['result']['result_ids'] == expected
        assert view and [item['result_id'] for item in view['items']] == expected
        cold = SQLiteKernelStore(secret_provider=lambda: SECRET)
        pipeline = native.pipeline(cold)
        context = await context_for(cold)
        unknown = [item['result_id'] for item in result['items'] if item['quality']['match'] == 'unknown']
        empty, view = await display(pipeline, context, unknown)
        assert empty['result']['result_ids'] == [] and view is None
        native.service.result_store = IndexerResultStore()  # 冷恢复不依赖进程内临时索引缓存。
        untrusted = {**result, 'partial': False, 'complete': True, 'has_more': False,
                     'verification_targets': [{'result_id': ids[0], 'query': 'invented', 'season': 9, 'episode': 9}]}
        untrusted['items'] = [*result['items'], {**result['items'][0], 'result_id': 'foreign-candidate-1234'}]
        before = await cold.load(owner=context.owner, session_id=context.session_id)
        invalid, _ = await rpc_call(pipeline, context, 'resources.rank_episode', {
            'search': untrusted, 'season': -1, 'episode': 2})
        assert invalid['error']['code'] == 'validation_error'
        after = await cold.load(owner=context.owner, session_id=context.session_id)
        assert after.metadata['resource_candidate_scope'] == before.metadata['resource_candidate_scope']
        reranked, _ = await rpc_call(pipeline, context, 'resources.rank_episode', {
            'search': untrusted, 'season': 1, 'episode': 2})
        assert 'error' not in reranked
        second = next(item['result_id'] for item in result['items'] if item['title'] == titles[1])
        response, view = await display(pipeline, context, [second])
        assert response['result']['result_ids'] == [second] and view
        assert view['items'][0]['match'] == 'exact_episode'
        assert response['result']['verification_targets'] == []
        assert view['coverage_notice'] == '部分来源返回不完整；尚有未读取页，结果仅覆盖已返回范围。'
        assert 'error' in (await display(pipeline, context, ['foreign-candidate-1234']))[0]
        # 同一资源跨目标重排：不能用第2集的冲突标签覆盖第1集的有效证据。
        first = next(item['result_id'] for item in result['items'] if item['title'] == titles[0])
        response, view = await display(pipeline, context, [first])
        assert response['result']['result_ids'] == [first] and view
        assert view['items'][0]['match'] == 'exact_episode'
        # 主动宽泛重搜仍可看全部结果，不把之前的季集条件强加到其它查询。
        broad = await read(pipeline, context, 'Black Mirror')
        broad_ids = [item['result_id'] for item in broad['items']]
        response, view = await display(pipeline, context, broad_ids)
        assert response['result']['result_ids'] == broad_ids and view
    asyncio.run(exercise())


@pytest.mark.parametrize('title,match,coverage', [
    ('Example  S01E02  1080p WEB-DL', 'exact_episode', None),
    ('Example.S01E02.1080p（CHT）', 'exact_episode', None),
    ('Example.Ｓ０１Ｅ０２－Ｅ０３.1080p', 'episode_pack', [1, 2, 3]),
    ('Example.' + 'release.' * 40 + 'S01E02-E03.1080p.WEB-DL', 'episode_pack', [1, 2, 3]),
], ids=['spacing', 'unicode', 'fullwidth-range', 'long'])
def test_rerank_and_presentation_use_original_release_facts(native, monkeypatch, title, match, coverage):
    item = IndexerItem('nyaa', 'Nyaa', title, download_state='ready',
        download_kinds=('magnet',), magnet='magnet:?xt=urn:btih:' + 'a' * 40)
    identity = native.service.result_store.put(item)
    async def search(request, site_ids=None, **kwargs):
        return AggregatedIndexerResult(query=request.title, page=1, items=[replace(item, result_id=identity)],
            sites_attempted=('nyaa',), sites_succeeded=('nyaa',))
    monkeypatch.setattr(native.service, 'search_media', search)
    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        result = await read(pipeline, context, season=1, episode=1, resolution='1080p')
        assert [row['result_id'] for row in result['items']] == [identity]
        response, _ = await rpc_call(pipeline, context, 'resources.rank_episode', {
            'search': result, 'season': 1, 'episode': 2})
        assert 'error' not in response, response
        ranked = response['result']
        assert ranked['recommendation']['selected']['result_id'] == identity
        assert ranked['items'][0]['quality']['tags']['resolution'] == '1080p'
        shown, view = await display(pipeline, context, [identity])
        assert shown['result']['result_ids'] == [identity]
        assert view and view['items'][0]['match'] == match
        assert view['items'][0]['coverage'] == coverage
        assert native.service.result_store.get(identity).title == title
        # 同一ID换成另一个标题不能冒充宿主保存的检索事实。
        altered = deepcopy(result)
        altered['items'][0]['title'] += '.S01E03'
        await rpc_call(pipeline, context, 'resources.rank_episode', {
            'search': altered, 'season': 1, 'episode': 3})
        shown, view = await display(pipeline, context, [identity])
        assert shown['result']['result_ids'] == [identity]
        assert all(query['query']['episode'] != 3 for query in shown['result']['queries'])
        assert 'magnet:?' not in str(shown) + str(view)
    asyncio.run(exercise())


def test_cross_query_card_keeps_each_source_constraints_and_only_selected_items(native):
    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        first = await read(pipeline, context, 'First', resolution='1080p')
        second = await read(pipeline, context, 'Second', exclude_hdr=True)
        # 无结果的第三个不同查询不能抹掉已找到的其他作品，也不能污染其规格。
        await read(pipeline, context, 'Third', resolution='2160p')
        ids = [first['items'][0]['result_id'], second['items'][-1]['result_id']]
        response, view = await display(pipeline, context, ids)
        assert 'error' not in response and response['result']['result_ids'] == ids
        assert view and [item['result_id'] for item in view['items']] == ids
        assert [item['position'] for item in view['items']] == [1, 2]
        assert [query['query']['title'] for query in response['result']['queries']] == ['First', 'Second']
        assert view['coverage_notice']
        cold = SQLiteKernelStore(secret_provider=lambda: SECRET)
        state = await cold.load(owner=context.owner, session_id=context.session_id)
        selection = await validate_selection({'ref': view['selection_ref'], 'positions': [2], 'target': 'guangya'}, state=state, store=cold)
        assert selection.arguments['result_ids'] == ids[1:]
    asyncio.run(exercise())


def test_cached_candidate_can_use_refined_query_and_successful_retry_coverage(native, monkeypatch):
    item = IndexerItem('nyaa', 'Nyaa', 'Example.S01E01.2160p.HDR10.WEB-DL',
        download_state='ready', download_kinds=('magnet',), magnet='magnet:?xt=urn:btih:' + 'a' * 40)
    identity = native.service.result_store.put(item)
    calls = 0
    async def search(request, site_ids=None, **kwargs):
        nonlocal calls
        calls += 1
        return AggregatedIndexerResult(query=request.title, page=1, items=[replace(item, result_id=identity)],
            sites_attempted=('nyaa',), sites_succeeded=('nyaa',), partial=calls < 3, complete=calls >= 3)
    monkeypatch.setattr(native.service, 'search_media', search)
    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        await read(pipeline, context, dynamic_range='HDR')
        await read(pipeline, context, dynamic_range='HDR10')
        first, view = await display(pipeline, context, [identity], dynamic_range='HDR10')
        assert 'error' not in first and view and view['coverage_notice']
        await read(pipeline, context, dynamic_range='HDR10')
        final, view = await display(pipeline, context, [identity], dynamic_range='HDR10')
        assert 'error' not in final and final['result']['result_ids'] == [identity]
        assert view and not view.get('coverage_notice')
    asyncio.run(exercise())


@pytest.fixture(params=['resources.search', 'resources.search_missing'])
def resource_query(native, monkeypatch, request):
    if request.param == 'resources.search':
        return NS(method=request.param, arguments={'title': 'Example'}, defaults={'resolution': '', 'exclude_hdr': False})
    from app.modules import missing_resources
    from tests.test_missing_resources import audit_for

    def audit(arguments, **kwargs):
        return audit_for(arguments['query'], season=arguments['season'], episodes=[1, 2])

    monkeypatch.setattr(missing_resources, 'get_indexer_service', lambda: native.service)
    monkeypatch.setattr(missing_resources, 'audit_series_episodes', audit)
    monkeypatch.setattr('app.indexers.downloads.episode_audit.audit_series_episodes', audit)
    return NS(method=request.param, arguments={'items': [{'query': 'Example', 'season': 1}]},
              defaults={'max_episodes': None, 'limit_per_episode': 8})


async def read_query(pipeline, context, query, **arguments):
    response, _ = await rpc_call(pipeline, context, query.method, {**query.arguments, **arguments})
    assert 'error' not in response, response
    return response['result']


@pytest.mark.parametrize('partial_first', [False, True])
def test_rpc_same_query_pages_and_research_keep_every_real_id(native, resource_query, monkeypatch, partial_first):
    original = native.service.search_media
    async def search(request, site_ids=None, **kwargs):
        result = await original(request, site_ids, **kwargs)
        partial = partial_first and request.page == 1
        return replace(result, partial=partial, complete=not partial, has_more=request.page == 1)
    monkeypatch.setattr(native.service, 'search_media', search)
    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        pages = [await read_query(pipeline, context, resource_query, page=1),
                 await read_query(pipeline, context, resource_query, page=2, **resource_query.defaults),
                 await read_query(pipeline, context, resource_query, page=1)]
        ids = [item['result_id'] for page in pages for item in page['items']]
        assert len(set(ids)) == 6
        response, view = await display(pipeline, context, ids)
        assert 'error' not in response, response
        assert view and [item['result_id'] for item in view['items']] == ids
        coverage = [{key: page[key] for key in ('page', 'partial', 'truncated', 'complete', 'has_more')} for page in pages]
        assert response['result']['scope']['pages'] == coverage
        assert response['result']['scope']['complete'] is True
        assert bool(view.get('coverage_notice')) is partial_first
        assert '尚有未读取页' not in response['result']['coverage_notice']
        goals = [goal for page in pages for goal in page.get('verification_targets', [])]
        assert response['result']['verification_targets'] == goals
        subset, _ = await display(pipeline, context, ids[:1])
        assert subset['result']['scope']['complete'] is False
        assert subset['result']['scope']['pages'] == coverage
        for owner, session_id in ((context.owner, 'other_presentation_session'), ('other-owner', context.session_id)):
            foreign = await context_for(native.store, owner=owner, session_id=session_id)
            rejected, _ = await display(pipeline, foreign, ids[:1])
            assert 'error' in rejected
    asyncio.run(exercise())


@pytest.mark.parametrize('count', [20, 50])
def test_rpc_full_batch_tail_cold_restore_and_explicit_clear(native, count):
    async def exercise():
        pipeline = native.pipeline()
        native.service.test_count = count
        context = await context_for(native.store)
        value = await read(pipeline, context, page=2, resolution='1080p', exclude_hdr=True)
        ids = [item['result_id'] for item in value['items']]
        assert len(ids) == count
        response, view = await display(pipeline, context, ids)
        assert 'error' not in response, response
        assert response['result']['result_ids'] == ids
        assert response['result']['effective_query']['exclude_hdr'] is True
        assert view is not None
        assert [item['result_id'] for item in view['items']] == ids
        cold = SQLiteKernelStore(secret_provider=lambda: SECRET)
        state = await cold.load(owner=context.owner, session_id=context.session_id)
        restored = await current_candidate_view(state=state, store=cold)
        assert restored and [item['result_id'] for item in restored['items']] == ids
        selected = await validate_selection({'ref': restored['selection_ref'], 'positions': [count], 'target': 'both'}, state=state, store=cold)
        assert selected.arguments == {'result_ids': [ids[-1]], 'target': 'both'}
        native.service.result_store = IndexerResultStore()
        from app.modules.resource_candidates import restore_resource_candidate_reference
        frozen = await cold.resolve(restored['ref'], owner=context.owner, session_id=context.session_id, expected_kind='resource_candidates')
        assert restore_resource_candidate_reference(frozen)
        assert native.service.get_result(ids[-1], scope='agent').title.endswith(f'E{count:02d}.1080p.WEB-DL')
        context = await context_for(cold)
        response, view = await display(native.pipeline(cold), context, [ids[-1]])
        assert 'error' not in response and view is not None
        assert [item['result_id'] for item in view['items']] == [ids[-1]]
        response, view = await display(native.pipeline(cold), context, [])
        assert response['result']['status'] == 'empty' and view is None
        assert await current_candidate_view(state=await cold.load(owner=context.owner, session_id=context.session_id), store=cold) is None
    asyncio.run(exercise())


def test_rpc_keeps_cross_query_facts_but_rejects_foreign_session(native):
    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        first = await read(pipeline, context, 'First')
        ids = [item['result_id'] for item in first['items']]
        assert 'error' not in (await display(pipeline, context, ids))[0]
        second = await read(pipeline, context, 'Second')
        assert first['items'][0]['result_id'] != second['items'][0]['result_id']
        response, view = await display(pipeline, context, ids)
        assert 'error' not in response and view
        state = await native.store.load(owner=context.owner, session_id=context.session_id)
        current = await current_candidate_view(state=state, store=native.store)
        assert current and current['ref'] == view['ref']
        foreign = await context_for(native.store, session_id='other_presentation_session')
        response, _ = await display(pipeline, foreign, [second['items'][0]['result_id']])
        assert response['error']['code'] == 'ValueError'
        assert first['partial'] and first['has_more'] and len(first['items']) == 2
    asyncio.run(exercise())


@pytest.mark.parametrize('change', ['target', 'quality', 'sites'])
def test_rpc_other_query_conditions_do_not_discard_observed_ids(native, resource_query, change):
    changes = ({'target': {'season': 2}, 'quality': {'resolution': '2160p'}, 'sites': {'sites': ['nyaa']}}
               if resource_query.method == 'resources.search' else
               {'target': {'items': [{'query': 'Example', 'season': 2}]},
                'quality': {'preference_overrides': {'preferred_resolution': '2160p'}}, 'sites': {'sites': ['nyaa']}})
    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        first = await read_query(pipeline, context, resource_query)
        await read_query(pipeline, context, resource_query, page=2)
        await read_query(pipeline, context, resource_query, **changes[change])
        response, _ = await display(pipeline, context, [first['items'][0]['result_id']])
        assert 'error' not in response and response['result']['result_ids'] == [first['items'][0]['result_id']]
    asyncio.run(exercise())


@pytest.mark.parametrize('layout', ['flat', 'sources'])
@pytest.mark.parametrize('targeted', [False, True])
def test_rpc_saved_snapshot_without_query_metadata_migrates_on_next_search(native, layout, targeted):
    import json

    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        response, rpc = await rpc_call(pipeline, context, 'resources.search', {
            'title': 'Example', **({'season': 1, 'episode': 1} if targeted else {})})
        ids = [item['result_id'] for item in response['result']['items']]
        expected = ids[:1] if targeted else ids
        snapshot = await rpc.presentation.snapshot()
        assert snapshot is not None
        source = snapshot.pop('_sources')[0]
        snapshot['_query_arguments'] = source['query']
        snapshot['coverage'] = source['pages'][0]
        snapshot['_display_items'] = source['_display_items']
        if layout == 'sources':
            snapshot['_sources'] = [{key: value for key, value in source.items() if key not in {'_display_items', 'matching_ids'}}]
        handle = await pipeline.save_result(json.dumps(snapshot), context)
        await native.store.commit(context.lease, updates=(StateUpdate('metadata.resource_candidate_scope', {
            'search_id': snapshot['search_id'][3:], 'result_handle': handle}),))
        cold = SQLiteKernelStore(secret_provider=lambda: SECRET)
        context = await context_for(cold)
        pipeline = native.pipeline(cold)
        response, view = await display(pipeline, context, ids)
        assert 'error' not in response and view, response
        assert response['result']['result_ids'] == expected
        await read(pipeline, context, page=2)
        response, _ = await display(pipeline, context, ids)
        assert 'error' not in response and response['result']['result_ids'] == expected
    asyncio.run(exercise())


def test_rpc_merged_pages_keep_frozen_selection_and_cold_confirmation(native, resource_query, monkeypatch):
    from app.agent.errors import ToolPipelineError
    from app.indexers import downloads

    native.service.test_count = 26
    monkeypatch.setenv('INDEXER_SEARCH_ENABLED', 'true')
    monkeypatch.setattr(downloads, 'download_target_readiness', lambda target: {'qb': True})
    submitted = AsyncMock(return_value={'ok': True, 'status': 'accepted', 'summary': '已受理', 'error': '',
        'items': []})
    monkeypatch.setattr(downloads, 'submit_indexer_download_batch', submitted)
    async def exercise():
        context = await context_for(native.store)
        first = await read_query(native.pipeline(), context, resource_query)
        native.service.result_store = IndexerResultStore()
        cold = SQLiteKernelStore(secret_provider=lambda: SECRET)
        context = await context_for(cold)
        pipeline = native.pipeline(cold)
        second = await read_query(pipeline, context, resource_query, page=2)
        ids = [first['items'][0]['result_id'], second['items'][-1]['result_id']]
        response, view = await display(pipeline, context, ids)
        assert 'error' not in response and view, response
        assert response['result']['scope']['candidate_count'] == 52
        state = await cold.load(owner=context.owner, session_id=context.session_id)
        selection = await validate_selection({'ref': view['selection_ref'], 'positions': [1, 2], 'target': 'qb'}, state=state, store=cold)
        assert selection.arguments['result_ids'] == ids
        frozen = deepcopy(await cold.resolve(view['ref'], owner=context.owner, session_id=context.session_id,
                                             expected_kind='resource_candidates'))
        plan = (await pipeline.prepare('resources.download', dict(selection.arguments), context=context)).effect_plan
        assert plan is not None and plan.arguments['result_ids'] == ids
        submitted.assert_not_awaited()
        await read_query(pipeline, context, resource_query, page=1)
        assert await cold.resolve(view['ref'], owner=context.owner, session_id=context.session_id,
                                  expected_kind='resource_candidates') == frozen
        native.service.result_store = IndexerResultStore()
        rebuilt = native.pipeline(SQLiteKernelStore(secret_provider=lambda: SECRET))
        pending = await rebuilt.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert pending and pending.plan_id == plan.plan_id
        completed = await rebuilt.execute_confirmed(plan.plan_id, context=context)
        assert completed.outcome.public_content['status'] == 'accepted'
        submitted.assert_awaited_once()
        assert submitted.call_args.args[1:3] == (ids, 'qb')
        with pytest.raises(ToolPipelineError):
            await rebuilt.execute_confirmed(plan.plan_id, context=context)
        submitted.assert_awaited_once()
    asyncio.run(exercise())


@pytest.mark.parametrize('bad', [[True], ['invented'], ['a' * 20] * 2, 'all', list(range(51))])
def test_present_requires_real_unique_ids(native, bad):
    async def exercise():
        context = await context_for(native.store)
        pipeline = native.pipeline()
        await read(pipeline, context)
        response, view = await display(pipeline, context, bad)
        assert response['error']['code'] == 'ValueError' and view is None
    asyncio.run(exercise())


class ScriptModel:
    def __init__(self, code):
        self.code = code
        self.requests = []
    async def stream(self, request, *, cancellation):
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall('native-resource', 'agent.run_code', {'code': self.code}))
            yield ModelEvent(ModelEventType.FINISH, finish_reason='tool_calls')
        else:
            yield ModelEvent(ModelEventType.TEXT_DELTA, text='候选已展示，尚未下载。')
            yield ModelEvent(ModelEventType.FINISH, finish_reason='stop')


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason='需要可运行 Bubblewrap 的 Linux 隔离工作区')
@pytest.mark.parametrize('channel,count', [('web', 50), ('telegram', 20)])
def test_real_sandbox_rpc_reaches_event_history_and_original_card_consumers(native, monkeypatch, channel, count):
    from tests.support import initialized_web_runtime
    with initialized_web_runtime():
        from tests.test_agent_kernel_telegram_adapter import TELEBOT, FakeBot, Message, Call
        from app.agent.transports import WebKernelTransport, TelegramKernelTransport, QueryEnvelope
        from app.agent.public_view import public_conversation_messages
        from app.bot import agent_adapter, agent_candidates
        from app.routes import agent_api
        pipeline = native.pipeline()
        native.service.test_count = count
        code = 'from mf import resources\nr=resources.search(title="Example", page=2)\nprint(resources.present([i["result_id"] for i in r["items"]]))'
        if channel == 'web':
            code = ('from mf import resources\nimport json, subprocess\n'
                    'r=resources.search(title="Example", page=2)\n'
                    'with open("/work/candidate-ids.json", "w") as f:\n'
                    '    json.dump([i["result_id"] for i in r["items"]], f)\n'
                    'subprocess.run(["mf", "resources.present", "--result-ids", "@/work/candidate-ids.json"], check=True)')
        model = ScriptModel(code)
        session = AgentSession(model=model, catalog=pipeline.catalog,  pipeline=pipeline,
            state_store=native.store, journal=native.store)
        runtime = NS(store=native.store, session=session, web=WebKernelTransport(session), telegram=TelegramKernelTransport(session))
        message = Message('搜索Example并展示全部候选，不下载', chat_id=-87651, user_id=73)
        bot = FakeBot()
        if channel == 'telegram':
            owner, sid, _ = agent_adapter._session_for_source(message.chat.id, message.from_user.id, message)
            monkeypatch.setattr(agent_adapter, 'get_agent_kernel_runtime', lambda: runtime)
            result = agent_adapter._execute_query(bot, TELEBOT, message, chat_id=str(message.chat.id), user_id='73', text=message.text)
            assert result.status == 'success', result
            assert any(kwargs.get('reply_markup') for _, kwargs in bot.replies) or any(kwargs.get('reply_markup') for _, _, kwargs in bot.sent) or any(kwargs.get('reply_markup') for _, _, _, kwargs in bot.edits)
        else:
            monkeypatch.setattr(agent_api.config, 'web_credentials', lambda: ('presentation', 'unused'))
            owner, sid = agent_api.web_kernel_owner('presentation'), 'presentation_session_1234'
            result = asyncio.run(runtime.web.query_view(QueryEnvelope(message='搜索Example并展示全部候选，不下载', owner=owner, session_id=sid)))
        assert result.status == 'success', result
        assert result.candidate_view and len(result.candidate_view['items']) == count
        assert len(model.requests) == 2
        import json
        tool_messages = [item for item in model.requests[-1].messages if item.role == 'tool']
        model_result = json.loads(tool_messages[-1].content)
        assert model_result['candidate_view'] == {'ref': result.candidate_view['ref']}
        assert 'selection_ref' not in tool_messages[-1].content
        cold = SQLiteKernelStore(secret_provider=lambda: SECRET)
        async def verify():
            state = await cold.load(owner=owner, session_id=sid)
            view = await current_candidate_view(state=state, store=cold)
            assert view and view['ref'] == result.candidate_view['ref']
            assert view['coverage_notice'] == result.candidate_view['coverage_notice']
            messages = public_conversation_messages(state.conversation, candidate_view=view)
            assert sum(row.get('candidate_view', {}).get('ref') == view['ref'] for row in messages) == 1
            journal = await cold.list_events(owner=owner, session_id=sid)
            assert any(event['payload'].get('result', {}).get('candidate_view', {}).get('ref') == view['ref']
                       for event in journal if isinstance(event['payload'].get('result', {}).get('candidate_view'), dict))
            draft = (state.metadata.get('ux_candidate_draft') if channel == 'telegram' else None) or await agent_candidates.start_draft(NS(store=cold), owner=owner, session_id=sid, view=view)
            text, markup = agent_candidates.render(TELEBOT, view, {**draft, "expanded": True})
            assert markup and f'#{count}' in text
            assert any(button.callback_data.endswith(f':i{count}') for button in markup.buttons)
            assert any('agk:s:' in button.callback_data for button in markup.buttons)
            if channel == 'telegram':
                message.reply_to_message = NS(text=text, caption='', message_id=90, chat=message.chat,
                    reply_markup=NS(keyboard=[[{'callback_data': f"agk:s:{draft['handle']}:p"}]]))
                assert await agent_candidates.reply_selection_ref(NS(store=cold), owner=owner, session_id=sid, message=message) == view['selection_ref']
            assert not state.pending_effect_plan_id
            return view
        view = asyncio.run(verify())
        if channel == 'telegram':
            monkeypatch.setattr('app.agent.kernel.bootstrap.get_agent_kernel_runtime', lambda: runtime)
            state = asyncio.run(cold.load(owner=owner, session_id=sid))
            draft = state.metadata['ux_candidate_draft']
            click = Call(f"agk:s:{draft['handle']}:i{count}", message)
            agent_candidates.handle_callback(bot, click, TELEBOT, owner=owner, session_id=sid)
            saved = asyncio.run(cold.load(owner=owner, session_id=sid))
            assert saved.metadata['ux_candidate_draft']['positions'] == [count]
            chosen = asyncio.run(validate_selection({'ref': view['selection_ref'], 'positions': [count], 'target': 'guangya'}, state=saved, store=cold))
            assert chosen.arguments['result_ids'] == [view['items'][-1]['result_id']]
            assert not saved.pending_effect_plan_id
        if channel == 'web':
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            from starlette.middleware.sessions import SessionMiddleware
            application = FastAPI()
            application.include_router(agent_api.router)
            application.add_middleware(SessionMiddleware, secret_key=SECRET)
            @application.get('/_test_login')
            def login(request: Request):
                request.session.update(logged_in=True, csrf_token='test')
                return {'ok': True}
            monkeypatch.setattr(agent_api, 'get_agent_kernel_runtime', lambda: runtime)
            monkeypatch.setattr(agent_api, 'get_web_secret', lambda: SECRET)
            with TestClient(application) as client:
                assert client.get('/_test_login').status_code == 200
                response = client.get('/api/agent/sessions/' + sid)
            assert response.status_code == 200, response.text
            assert [item['result_id'] for item in response.json()['candidate_view']['items']] == [item['result_id'] for item in view['items']]
            assert any(row.get('candidate_view', {}).get('ref') == view['ref'] for row in response.json()['messages'])
        assert sdk.METHODS['resources.present'] is sdk.resources.present
        assert 'resources.present' not in sdk.WRITE_METHODS


@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('same_query', [False, True])
def test_rpc_new_empty_or_failed_search_clears_card_before_completion(native, resource_query, monkeypatch, failure, same_query):
    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        original = await read_query(pipeline, context, resource_query)
        ids = [item['result_id'] for item in original['items']]
        _, view = await display(pipeline, context, ids)
        assert view is not None
        selection = {'ref': view['selection_ref'], 'positions': [1], 'target': 'guangya'}
        assert isinstance(context.report_progress, AsyncMock)
        context.report_progress.reset_mock()
        async def search(request, site_ids=None, **kwargs):
            state = await native.store.load(owner=context.owner, session_id=context.session_id)
            assert state.metadata['ux_candidate_view'] is None
            if failure:
                raise RuntimeError('fixture source unavailable')
            return AggregatedIndexerResult(query=request.title, page=request.page, items=[], sites_attempted=('nyaa',), sites_succeeded=('nyaa',))
        monkeypatch.setattr(native.service, 'search_media', search)
        changed = ({'title': 'New'} if resource_query.method == 'resources.search' else
                   {'items': [{'query': 'New', 'season': 1}]})
        arguments = {**resource_query.arguments, 'page': 2, **({} if same_query else changed)}
        response, _ = await rpc_call(pipeline, context, resource_query.method, arguments)
        assert ('error' in response) is (failure and resource_query.method == 'resources.search')
        if 'result' in response:
            assert response['result']['presentation'] == {'result_ids': []}
        progress = [call.args[0] for call in context.report_progress.call_args_list]
        activities = [row['activity'] for row in progress if row.get('activity', {}).get('kind') == 'tool']
        process_progress = [row for row in progress if row.get('activity', {}).get('kind') != 'tool']
        assert process_progress == [{'tool': resource_query.method, 'candidate_view': None}]
        assert [row['status'] for row in activities] == ['running', 'failed' if failure else 'success']
        assert all(row['method'] == resource_query.method for row in activities)
        assert activities[0]['id'] == activities[1]['id']
        state = await native.store.load(owner=context.owner, session_id=context.session_id)
        assert await current_candidate_view(state=state, store=native.store) is None
        from app.agent.kernel.state_types import SelectionInvalidError
        with pytest.raises(SelectionInvalidError):
            await validate_selection(selection, state=state, store=native.store)
        response, _ = await display(pipeline, context, ids)
        assert 'error' not in response and response['result']['result_ids'] == ids
    asyncio.run(exercise())


def test_rpc_large_observation_keeps_tail_and_filter_scope(native):
    async def exercise():
        pipeline = native.pipeline()
        native.service.test_count = 72
        context = await context_for(native.store)
        value = await read(pipeline, context, resolution='1080p', exclude_hdr=True)
        ids = [item['result_id'] for item in value['items']]
        assert len(ids) == 72
        response, view = await display(pipeline, context, ids[-2:], exclude_hdr=False)
        assert 'error' not in response and response['result']['effective_query']['exclude_hdr'] is True
        assert view is not None
        assert [item['result_id'] for item in view['items']] == ids[-2:]
        assert response['result']['scope']['candidate_count'] == 72
        assert response['result']['scope']['considered_count'] == 2
        assert response['result']['scope']['complete'] is False
        state = await native.store.load(owner=context.owner, session_id=context.session_id)
        selection = await validate_selection({'ref': view['selection_ref'], 'positions': [2], 'target': 'guangya'}, state=state, store=native.store)
        assert selection.arguments['result_ids'] == [ids[-1]]
        changed, _ = await display(pipeline, context, ids[-2:], resolution='2160p')
        assert 'error' in changed
    asyncio.run(exercise())


def test_rpc_ranked_input_cannot_mint_a_search_batch(native):
    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        await read(pipeline, context)
        response, _ = await rpc_call(pipeline, context, 'resources.rank_episode', {
            'search': {'request': {'title': 'Foreign'}, 'items': [{'result_id': 'foreign-candidate-1234', 'title': 'Foreign.S01E01.1080p', 'download_state': 'ready', 'download_kinds': ['magnet']}]}, 'season': 1, 'episode': 1})
        assert 'error' not in response, response
        shown, _ = await display(pipeline, context, ['foreign-candidate-1234'])
        assert 'error' in shown
    asyncio.run(exercise())


@pytest.mark.parametrize('newer', ['search', 'failed_search', 'present', 'clear'])
def test_rpc_late_presentation_cannot_replace_newer_request(native, monkeypatch, newer):
    import functools
    import json
    import os
    import threading
    import uuid

    started, release = threading.Event(), threading.Event()
    original = sdk.resources.present
    @functools.wraps(original)
    def delayed(*args, **kwargs):
        if not started.is_set():
            started.set()
            assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setitem(sandbox.METHODS, 'resources.present', delayed)

    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        seed = await read(pipeline, context)
        ids = [item['result_id'] for item in seed['items']]
        root = native.root / 'rpc-ordering'
        (root / 'run').mkdir(parents=True)
        (root / 'output').mkdir()
        rpc = sandbox._SDKRequests(context, root, pipeline)
        rpc.uid = os.getuid()
        async def call(method, **arguments):
            name = uuid.uuid4().hex + '.json'
            (root / 'run/mf-requests' / name).write_text(json.dumps({'method': method, 'arguments': arguments}))
            await rpc.dispatch(name)
            return json.loads((root / 'run/mf-responses' / name).read_text())
        pending = asyncio.create_task(call('resources.present', result_ids=ids[:1]))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            if newer == 'failed_search':
                async def fail(*args, **kwargs):
                    raise RuntimeError('fixture source unavailable')
                monkeypatch.setattr(native.service, 'search_media', fail)
            if newer in {'search', 'failed_search'}:
                response = await call('resources.search', title='New')
                assert ('error' in response) is (newer == 'failed_search')
            else:
                response = await call('resources.present', result_ids=ids[1:] if newer == 'present' else [])
                assert 'error' not in response, response
            expected = await current_candidate_view(
                state=await native.store.load(owner=context.owner, session_id=context.session_id), store=native.store)
            release.set()
            late = await pending
            assert late['result']['presented'] is False
            assert late['result']['status'] == 'superseded'
            actual = await current_candidate_view(
                state=await native.store.load(owner=context.owner, session_id=context.session_id), store=native.store)
            assert actual == expected
            # 只阻止迟到回写；之后明确选择以前查到的资源仍然可以展示。
            response = await call('resources.present', result_ids=ids[:1])
            assert response['result']['presented'] is True
            assert rpc.presentation.view is not None
            assert [item['result_id'] for item in rpc.presentation.view['items']] == ids[:1]
        finally:
            release.set()
            await pending
            await rpc.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('same_query', [False, True])
def test_rpc_parallel_search_completion_keeps_facts_without_replacing_card(native, resource_query, monkeypatch, same_query):
    import threading
    started, release = threading.Event(), threading.Event()
    original = native.service.search_media
    async def search(request, site_ids=None, **kwargs):
        if request.page == 2:
            started.set()
            assert await asyncio.to_thread(release.wait, 5)
        return await original(request, site_ids, **kwargs)
    monkeypatch.setattr(native.service, 'search_media', search)
    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        seed = await read_query(pipeline, context, resource_query)
        def arguments(title, page):
            changed = ({'title': title} if resource_query.method == 'resources.search' else
                       {'items': [{'query': title, 'season': 1}]})
            return {'page': page, **({} if same_query else changed)}
        first = asyncio.create_task(read_query(pipeline, context, resource_query, **arguments('First', 2)))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            value = await read_query(pipeline, context, resource_query, **arguments('Second', 3))
            ids = [item['result_id'] for page in ([seed, value] if same_query else [value]) for item in page['items']]
            assert 'error' not in (await display(pipeline, context, ids))[0]
        finally:
            release.set()
        previous = await first
        view = await current_candidate_view(state=await native.store.load(owner=context.owner, session_id=context.session_id), store=native.store)
        assert view and [item['result_id'] for item in view['items']] == ids
        combined = ids + [previous['items'][0]['result_id']]
        response, view = await display(pipeline, context, combined)
        assert 'error' not in response and view and [item['result_id'] for item in view['items']] == combined
    asyncio.run(exercise())


def test_rpc_missing_pack_preserves_every_verification_goal_through_selection_and_effect_preview(native, monkeypatch):
    from app.modules import missing_resources
    from app.indexers import downloads
    from tests.test_missing_resources import audit_for
    from tests.support import _prepare_batch
    async def search(request, site_ids=None, **kwargs):
        item = IndexerItem('nyaa', 'Nyaa', f'{request.title}.S01E01-02.1080p.WEB-DL', download_state='ready',
            download_kinds=('magnet',), magnet='magnet:?xt=urn:btih:'+('a' if request.title == 'First' else 'b')*40)
        identity = native.service.result_store.put(item)
        return AggregatedIndexerResult(query=request.title, page=1, items=[replace(item, result_id=identity)], sites_attempted=('nyaa',), sites_succeeded=('nyaa',))
    def audit(arguments, **kwargs):
        return audit_for(arguments['query'], episodes=[1, 2], identity='1' if arguments['query'] == 'First' else '2')
    monkeypatch.setattr(native.service, 'search_media', search)
    monkeypatch.setattr(missing_resources, 'get_indexer_service', lambda: native.service)
    monkeypatch.setattr(missing_resources, 'audit_series_episodes', audit)
    monkeypatch.setattr(downloads.episode_audit, 'audit_series_episodes', audit)
    monkeypatch.setattr(downloads, 'preview_indexer_download_batch', _prepare_batch)
    monkeypatch.setattr(missing_resources.config, 'get_bool', lambda *args, **kwargs: True)
    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        response, _ = await rpc_call(pipeline, context, 'resources.search_missing', {'items':[{'query':'First','season':1},{'query':'Second','season':1}]})
        assert 'error' not in response, response
        value = response['result']
        assert len(value['result_ids']) == 2 and len(value['verification_targets']) == 4
        response, view = await display(pipeline, context, value['result_ids'])
        assert 'error' not in response and view, response
        assert response['result']['verification_targets'] == value['verification_targets']
        state = await native.store.load(owner=context.owner, session_id=context.session_id)
        selection = await validate_selection({'ref':view['selection_ref'],'positions':[1,2],'target':'guangya'}, state=state, store=native.store)
        assert list(selection.arguments['verification_targets']) == value['verification_targets']
        prepared = await pipeline.prepare('resources.download', {'result_ids':value['result_ids'],'target':'guangya'}, context=context)
        assert prepared.effect_plan is not None
        assert sum(map(len, prepared.effect_plan.metadata['verification_targets'].values())) == 4
        assert list(prepared.effect_plan.arguments['verification_targets']) == value['verification_targets']
        assert prepared.effect_plan.metadata['resource_selection']['ref'] == view['ref']
        from app import database as db
        with db.get_conn() as conn:
            assert conn.execute('SELECT COUNT(*) FROM download_requests').fetchone()[0] == 0
    asyncio.run(exercise())


@pytest.mark.parametrize('attack', ['expired', 'owner', 'generation'])
def test_rpc_presentation_requires_current_owner_lease_and_live_observation(native, attack):
    async def exercise():
        pipeline = native.pipeline()
        context = await context_for(native.store)
        value = await read(pipeline, context)
        identity = value['items'][0]['result_id']
        if attack == 'expired':
            native.store._clock = lambda: __import__('time').time()+901
        elif attack == 'owner':
            context = await context_for(native.store, owner='other-owner')
        else:
            await context_for(native.store)
        response, view = await display(pipeline, context, [identity])
        assert 'error' in response and view is None
    asyncio.run(exercise())
