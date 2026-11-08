"""资源原生观察与确认的模型回归工厂；集成验收见test_resource_presentation_native。"""
from __future__ import annotations
import json
from typing import Any
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.models import ToolResult, ToolReference
from tests.resource_presentation_test_harness import safe_resource_snapshot, new_resource_search_id, model_resource_candidates
from tests import test_agent_ux_backend as ux


def _search_result() -> ToolResult:
    result = ToolResult(
        True,
        "success",
        "找到 1 项可提交资源",
        data={
            "query": "绿灯军团",
            "items": [
                {
                    "result_id": "green-lantern-4k-001",
                    "title": "Lanterns.S01E01.2160p.WEB-DL.DV.HDR",
                    "site_id": "demo",
                    "site_name": "Demo",
                    "size_text": "3.60 GB",
                    "download_state": "ready",
                    "download_kinds": ["magnet"],
                }
            ],
        },
    )
    result.references.append(
        ToolReference(
            "resource_candidates",
            safe_resource_snapshot(result, search_id=new_resource_search_id()),
        )
    )
    return result


def _multi_work_candidate(
    position: int,
    title: str,
    tmdb_id: str,
) -> dict[str, Any]:
    release_range = (
        "S01E07-08"
        if position == 1
        else "S01E08-09"
        if position == 6
        else "S01E08"
    )
    match = "episode_pack" if position in {1, 6} else "exact_episode"
    result_id = f"multi-work-candidate-{position:02d}"
    return {
        "position": position,
        "season": 1,
        "episode": 8,
        "episode_label": "S01E08",
        "result_id": result_id,
        "title": f"{title}.{release_range}.2160p.WEB-DL",
        "site_id": "fixture-indexer",
        "site_name": "离线索引夹具",
        "rank": position,
        "score": 100 - position,
        "confidence": "high",
        "match": match,
        "download_state": "ready",
        "reasons": ["已核验媒体库缺集"],
        "warnings": [],
        "tags": {"resolution": "2160p"},
        "_verification_context": {
            "title": title,
            "tmdb_id": tmdb_id,
            "season": 1,
            "episode": 8,
            "as_of": "2026-09-13",
        },
    }


def _model_json_field(request, field):
    for message in reversed(request.messages):
        if message.role != 'tool':
            continue
        payload = json.loads(message.content)
        if field == 'items':
            return model_resource_candidates(payload)
        if field in payload:
            return payload[field]
    raise AssertionError(f'本轮实际模型观察缺少{field}')


def _native_arguments(request, positions, target='guangya'):
    candidates = _model_json_field(request, 'items')
    return {'result_ids': [candidates[position-1]['result_id'] for position in positions], 'target': target}


class SameTurnSearchSubmitModel:
    def __init__(self):
        self.requests = []
    async def stream(self, request, *, cancellation):
        self.requests.append(request)
        if len(self.requests) == 1:
            call = ModelToolCall('search-1', 'fixture.resource_read', {'title': '绿灯军团'})
        else:
            call = ModelToolCall('submit-1', 'resources.download', _native_arguments(request, [1]))
        yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=call)
        yield ModelEvent(ModelEventType.FINISH, finish_reason='tool_calls')


class SearchThenAnswerModel:
    def __init__(self, tool_name='fixture.resource_read', arguments=None):
        self.requests = []
        self.tool_name = tool_name
        self.arguments = arguments or {'title': '绿灯军团'}
    async def stream(self, request, *, cancellation):
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall('read', self.tool_name, self.arguments))
            yield ModelEvent(ModelEventType.FINISH, finish_reason='tool_calls')
        else:
            yield ModelEvent(ModelEventType.TEXT_DELTA, text='已读取实际候选，尚未下载。')
            yield ModelEvent(ModelEventType.FINISH, finish_reason='stop')


async def _collect(stream):
    return [event async for event in stream]


def _runtime(model, search_result=None):
    return ux._runtime(model=model, resource_result=search_result or _search_result())
