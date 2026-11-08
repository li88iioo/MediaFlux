"""SDK完整搜索事实保留；卡片选择不改变来源或推断未知媒体规格。"""
from unittest.mock import patch
from app import sdk
from tests.test_agent_indexer_actions import _resource_item


def test_conflicting_search_hits_stay_in_frozen_scope_but_not_default_recommendation():
    from app.indexers.models import IndexerMediaSearchRequest
    from app.indexers.ranking import rank_item
    from app.indexers.result_store import IndexerResultStore
    from tests.test_agent_indexer_actions import FakeIndexerService

    request = IndexerMediaSearchRequest.create(title="Example", year=2026, season=2, episode=1)
    titles = ["Example.2026.S04E01.1080p", "Example.2025.S02E01.1080p", "Example.2026.S02E01.1080p"]
    service = FakeIndexerService(items=[rank_item(_resource_item(title=title, result_id=f"opaque-resource-{i:04d}"),
                                                 media=request, fallback_query=request.title) for i, title in enumerate(titles)])
    real_results = IndexerResultStore()
    for item in service.items:
        assert item.result_id is not None
        real_results.restore(item.result_id, item)
    with patch.object(service, "result_store", real_results), patch("app.indexers.runtime.get_indexer_service", return_value=service):
        result = sdk.resources.search("Example", year=2026, season=2, episode=1)
    assert {row["result_id"] for row in result["items"]} == {item.result_id for item in service.items}
    by_id = {row["result_id"]: row for row in result["items"]}
    conflict = by_id[service.items[0].result_id]
    assert conflict["quality"]["match"] == "conflict" and not conflict["quality"]["eligible"]
    current = by_id[service.items[1].result_id]
    assert "year_conflict" in current["match_reasons"]
    assert current["quality"]["match"] == "exact_episode" and current["quality"]["eligible"]
    assert result["recommendation"]["selected"]["result_id"] in {service.items[1].result_id, service.items[2].result_id}
    assert result["partial"] and result["has_more"]
