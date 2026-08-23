"""媒体业务案件的安全阶段标记。

案件阶段只描述用户当前处理到哪一步，不携带资源句柄、确认票据、路径或后端
任务标识。媒体身份本身继续由 conversation history 的 ``media_context`` 保存。
"""
from __future__ import annotations

from typing import Final

MEDIA_CASE_STAGES: Final[frozenset[str]] = frozenset({
    "identified",
    "library_checked",
    "updates_checked",
    "resource_candidates",
    "subscription_review",
    "download_submitted",
    "acquisition_tracking",
    "organize_review",
    "library_verification",
})

_MEDIA_CASE_STAGE_BY_TOOL: Final[dict[str, str]] = {
    "library.search": "library_checked",
    "library.count_series_episodes": "library_checked",
    "library.audit_episodes": "library_checked",
    "library.audit_library_episodes": "library_checked",
    "library.check_updates": "updates_checked",
    "library.search_missing_episode_resources": "resource_candidates",
    "library.search_missing_season_resources": "resource_candidates",
    "media.subscription_updates": "subscription_review",
    "discovery.search": "identified",
    "discovery.recommend": "identified",
    "discovery.lookup_rating": "identified",
    "discovery.add_watchlist": "identified",
    "indexer.search_resources": "resource_candidates",
    "indexer.submit_resource": "download_submitted",
    "indexer.submit_resource_batch": "download_submitted",
    "downloads.recent_submission_status": "acquisition_tracking",
    "downloads.verify_recent_submission_library": "library_verification",
    "library.missing_media_workflows": "acquisition_tracking",
}


def normalize_media_case_stage(value: object) -> str:
    stage = str(value or "").strip().lower()
    return stage if stage in MEDIA_CASE_STAGES else ""


def media_case_stage_for_tool(tool_name: object) -> str:
    return _MEDIA_CASE_STAGE_BY_TOOL.get(str(tool_name or "").strip(), "")
