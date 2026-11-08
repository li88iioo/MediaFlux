"""资源展示测试工厂：真实SDK文件RPC与历史卡片样本；不提供生产工具别名。"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from typing import Any

from app.agent.kernel.projection import ToolOutcome
from app.agent.kernel.sandbox import _SDKRequests
from app.agent.models import ToolReference
from app.modules.resource_candidates import (
    _safe_candidate, _safe_generic_candidate, _safe_verification_context,
    _serialize_private_item,
)


def new_resource_search_id():
    return 'rs_' + secrets.token_urlsafe(16)


def safe_resource_snapshot(result, *, search_id) -> dict[str, Any]:
    """只构造旧历史格式样本，不执行旧搜索/展示/上下文接口。"""
    data = result.data
    candidates = []
    seen = set()
    if isinstance(data.get('items'), list):
        raw = [_safe_generic_candidate(item) for item in data['items']]
    else:
        proof = data.get('verification') or {}
        entries = data.get('episodes') or [{**proof, 'search': data.get('search') or {}}]
        raw = []
        for entry in entries:
            season, episode = entry.get('season'), entry.get('episode')
            if not season or not episode:
                continue
            context = _safe_verification_context(proof, season=season, episode=episode)
            for item in entry['search'].get('items') or []:
                quality = item.get('quality') or item
                if quality.get('eligible') is False:
                    continue
                raw.append(_safe_candidate({**item, **quality}, season=season, episode=episode,
                    episode_label=f'S{season:02d}E{episode:02d}', verification_context=context))
    for item in raw:
        if item and item['result_id'] not in seen:
            seen.add(item['result_id'])
            candidates.append({**item, 'position': len(candidates) + 1})
    return {'search_id': search_id, 'search_status': result.status, 'candidates': candidates}


def attach_resource_candidate_reference(result, *, ttl_seconds=900, result_store=None, query_arguments=None):
    snapshot = safe_resource_snapshot(result, search_id=new_resource_search_id())
    if result_store is not None:
        snapshot.update(_private_version=1, _private_items=[_serialize_private_item(item['result_id'], result_store.get(item['result_id'])) for item in snapshot['candidates']])
    if query_arguments is not None:
        snapshot['_query_arguments'] = deepcopy(query_arguments)
    result.references = [ToolReference('resource_candidates', snapshot, ttl_seconds)]
    return result


async def rpc_call(pipeline, context, method, arguments, *, functions=None):
    """穿过生产请求校验、SDK宿主身份、观察提交、响应归档；无子进程能力要求。"""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'run').mkdir()
        (root / 'output').mkdir()
        rpc = _SDKRequests(context, root, pipeline)
        rpc.uid = os.getuid()
        name = uuid.uuid4().hex + '.json'
        (root / 'run/mf-requests' / name).write_text(json.dumps({'method': method, 'arguments': arguments}))
        try:
            with patch.dict('app.agent.kernel.sandbox.METHODS', functions or {}):
                await rpc.dispatch(name)
            response = json.loads((root / 'run/mf-responses' / name).read_text())
        finally:
            await rpc.close()
        return response, rpc


async def present(pipeline, context, result_ids, **specifications):
    response, rpc = await rpc_call(pipeline, context, 'resources.present', {'result_ids': result_ids, **specifications})
    if 'error' in response:
        from app.agent.errors import ToolPipelineError
        raise ToolPipelineError(response['error']['message'], code=response['error']['code'])
    content = {**response['result'], 'candidate_view': {key: value for key, value in rpc.presentation.view.items() if key != 'generation'} if rpc.presentation.view else None}
    from app.agent.kernel.tool_calls import PipelineResult
    return PipelineResult(tool=pipeline.catalog.get("fixture.resource_read") if pipeline.catalog.has("fixture.resource_read") else pipeline.catalog.get("agent.run_code"),
        arguments={"result_ids": result_ids, **specifications}, outcome=ToolOutcome(model_content=json.dumps(content, ensure_ascii=False), public_content=content))


async def observe_fixture(pipeline, context, result, arguments):
    """外部I/O替身仅提供DTO，候选绑定与发布运行原生宿主实现。"""
    from app.agent.domains.resources.selection import candidate_item
    legacy = safe_resource_snapshot(result, search_id=new_resource_search_id())
    data = deepcopy(result.data)
    if result.references:
        legacy = deepcopy(result.references[0].value)
    if 'items' not in data:
        data['items'] = [{**item, 'download_kinds': ['magnet']} for item in legacy['candidates']]
        goals = []
        for item in legacy['candidates']:
            proof = item.get('_verification_context')
            if isinstance(proof, dict):
                goals.append({'result_id': item['result_id'], 'query': proof['title'], **{k: v for k, v in proof.items() if k != 'title'}})
        data['verification_targets'] = goals
    data['items'] = [{**item, 'download_kinds': item.get('download_kinds') or ['magnet']} for item in data['items']]
    data.setdefault('verification_targets', [
        {'result_id': item['result_id'], 'query': proof['title'], **{k: v for k, v in proof.items() if k != 'title'}}
        for item in legacy['candidates'] if isinstance((proof := item.get('_verification_context')), dict)])
    payload = {**data, 'ok': result.ok, 'status': result.status, 'summary': result.summary}
    import inspect
    from types import SimpleNamespace
    from app import sdk
    from app.indexers.runtime import get_indexer_service
    service = get_indexer_service()
    def search(**kwargs):
        return payload
    setattr(search, "__signature__", inspect.signature(sdk.resources.search))
    service = SimpleNamespace(get_result=lambda identity, scope: service_store.get(identity))
    service_store = get_indexer_service().result_store
    with patch('app.indexers.runtime.get_indexer_service', return_value=service):
        response, rpc = await rpc_call(pipeline, context, 'resources.search', arguments, functions={'resources.search': search})
    if 'error' in response:
        from app.agent.errors import ToolPipelineError
        raise ToolPipelineError(response['error']['message'], code=response['error']['code'])
    # 业务事实回执保留安全投影；卡片只经resources.present签发。
    safe_items = [candidate_item(item, item['position']) for item in legacy['candidates']]
    from app.agent.kernel.ux_display import display_text
    public = {'ok': result.ok, 'status': result.status, 'summary': display_text(result.summary, limit=600), 'candidate_view': None}
    model = {**public, 'data': {**{k: v for k, v in data.items() if k not in {'items','rawresult','search','episodes','verification'}}, 'items': safe_items}}
    return ToolOutcome(model_content=json.dumps(model, ensure_ascii=False), public_content=public)


def model_resource_candidates(payload):
    """按真实ID读取本次事实，历史位置不再作为模型选择协议。"""
    candidates = {}
    def collect(value):
        if isinstance(value, dict):
            if value.get('result_id') and value.get('title'):
                candidates.setdefault(value['result_id'], value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect(payload)
    return list(candidates.values())
