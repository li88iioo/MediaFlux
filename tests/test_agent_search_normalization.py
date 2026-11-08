"""原生SDK入口保持标题规范化与非法查询拒绝，不保留已退役工具校验器。"""

import pytest

from app import sdk
from tests.test_agent_task_entry import isolated_db as isolated_db


@pytest.fixture(autouse=True)
def sdk_owner():
    with sdk.call_owner("webk:v1:" + "a" * 64):
        yield


@pytest.mark.parametrize("raw,expected", [("  沙丘２  ", "沙丘2"), ("Ｄｕｎｅ", "Dune"), ("a" * 120, "a" * 120)])
def test_native_workspace_search_normalizes_title(isolated_db, raw, expected):
    result = sdk.diagnostics.search(raw, sections=["rss"])
    assert result["data"]["query"] == expected


@pytest.mark.parametrize("query", [" ", "a" * 121, "Dune\u200b", "https://example.com/movie", "/media/Movie.mkv"])
def test_native_workspace_search_rejects_invalid_query(query):
    with pytest.raises(ValueError):
        sdk.diagnostics.search(query, sections=["rss"])
