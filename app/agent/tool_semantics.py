"""跨编排、投影与兼容响应共享的工具结果语义。"""
from __future__ import annotations

RESOURCE_CANDIDATE_TOOLS = frozenset({
    "indexer.search_resources",
    "library.search_missing_episode_resources",
    "library.search_missing_season_resources",
})

RESOURCE_EVIDENCE_TOOLS = RESOURCE_CANDIDATE_TOOLS | frozenset({
    "media.subscription_updates",
})


def default_result_presentation(tool_name: object) -> str:
    """为旧插件/测试 ToolSpec 提供集中式兼容默认值。"""
    return (
        "resource_candidates"
        if str(tool_name or "").strip() in RESOURCE_CANDIDATE_TOOLS
        else "narrative"
    )


def default_stages_resource_candidates(tool_name: object) -> bool:
    return str(tool_name or "").strip() in RESOURCE_EVIDENCE_TOOLS
