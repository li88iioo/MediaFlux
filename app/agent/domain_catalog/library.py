"""library 领域的 Agent 原子工具声明。"""

from __future__ import annotations

from app.agent.durable_job_actions import (
    prepare_start_episode_audit,
    start_episode_audit_arguments,
    start_episode_audit_confirmed,
)
from app.agent.episode_audit import audit_series_episodes
from app.agent.episode_resource_actions import (
    missing_episode_resource_arguments,
    missing_season_resource_arguments,
    search_missing_episode_resources,
    search_missing_season_resources,
)
from app.agent.library_episode_audit import audit_library_episodes
from app.agent.library_episode_count import (
    count_series_episodes,
    count_series_episodes_arguments,
)
from app.agent.library_patrol_config_actions import (
    patrol_policy_arguments,
    patrol_policy_summary_arguments,
    prepare_patrol_policy_confirmation,
    set_patrol_policy_confirmed,
    summarize_patrol_policy,
)
from app.agent.library_patrol_status import (
    get_library_patrol_status,
    patrol_status_arguments,
)
from app.agent.library_patrol_trigger_actions import (
    patrol_trigger_arguments,
    prepare_trigger_patrol_now,
    trigger_patrol_now_confirmed,
)
from app.agent.missing_media_workflow_runtime import MissingMediaWorkflowRuntime
from app.agent.missing_media_workflows import (
    list_missing_workflows,
    missing_workflow_arguments,
)
from app.agent.models import (
    RiskLevel,
    ToolSpec,
)
from app.agent.update_actions import check_library_updates

from .library_search import search_library
from .shared import (
    _episode_audit_arguments,
    _library_episode_audit_arguments,
    _library_update_arguments,
    _search_arguments,
)


def register_specs(
    registry,
    *,
    resource_store,
    active_ingest_store,
    ingest_actions,
    missing_media_runtime: MissingMediaWorkflowRuntime | None = None,
) -> None:
    del resource_store, active_ingest_store, ingest_actions

    def missing_episode_search(arguments, context):
        result = search_missing_episode_resources(arguments)
        if missing_media_runtime is not None:
            missing_media_runtime.capture_search(
                owner=context.owner,
                tool_name="library.search_missing_episode_resources",
                result=result,
            )
        return result

    def missing_season_search(arguments, context):
        result = search_missing_season_resources(arguments)
        if missing_media_runtime is not None:
            missing_media_runtime.capture_search(
                owner=context.owner,
                tool_name="library.search_missing_season_resources",
                result=result,
            )
        return result

    def missing_workflows(arguments, context):
        return list_missing_workflows(
            arguments,
            context,
            repository=(
                missing_media_runtime.repository
                if missing_media_runtime is not None
                else None
            ),
        )

    registry.register(
        ToolSpec(
            name="library.search",
            description="在已配置的 Jellyfin / Emby 媒体库中搜索标题。",
            risk=RiskLevel.READ,
            domains=("library", "media_identity"),
            source_kind="local_library",
            freshness="live",
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 120},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 8,
                    },
                },
                "additionalProperties": False,
            },
            handler=search_library,
            validator=_search_arguments,
            examples=(
                "媒体库里有没有《某片》",
                "在 Jellyfin 或 Emby 搜索这个标题",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="library.search_missing_episode_resources",
            description="先确认指定季集属于已播缺集，再定向搜索多站资源；不会自动下载。",
            risk=RiskLevel.READ,
            domains=("resource_search", "library"),
            source_kind="resource_index",
            freshness="realtime",
            parameters={
                "type": "object",
                "required": ["query", "season", "episode"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 120},
                    "tmdb_id": {"type": "string", "pattern": "^[0-9]{1,10}$"},
                    "library_name": {"type": "string", "minLength": 1, "maxLength": 80},
                    "season": {"type": "integer", "minimum": 1, "maximum": 100},
                    "episode": {"type": "integer", "minimum": 1, "maximum": 1000},
                    "as_of": {"type": "string", "format": "date"},
                    "sites": {
                        "type": "array",
                        "maxItems": 16,
                        "items": {"type": "string", "pattern": "^[a-z0-9_-]{1,32}$"},
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "additionalProperties": False,
            },
            context_handler=missing_episode_search,
            validator=missing_episode_resource_arguments,
            examples=(
                "搜索某剧第 2 季第 3 集的缺集资源",
                "确认 S02E03 缺失后找资源",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="library.search_missing_season_resources",
            description="先完整核对指定季度，再按顺序搜索最多 3 个已播缺集的多站资源；不会自动下载。",
            risk=RiskLevel.READ,
            domains=("resource_search", "library"),
            source_kind="resource_index",
            freshness="realtime",
            parameters={
                "type": "object",
                "required": ["query", "season"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 120},
                    "tmdb_id": {"type": "string", "pattern": "^[0-9]{1,10}$"},
                    "library_name": {"type": "string", "minLength": 1, "maxLength": 80},
                    "season": {"type": "integer", "minimum": 1, "maximum": 100},
                    "as_of": {"type": "string", "format": "date"},
                    "sites": {
                        "type": "array",
                        "maxItems": 16,
                        "items": {"type": "string", "pattern": "^[a-z0-9_-]{1,32}$"},
                    },
                    "max_episodes": {"type": "integer", "minimum": 1, "maximum": 3},
                    "limit_per_episode": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "additionalProperties": False,
            },
            context_handler=missing_season_search,
            validator=missing_season_resource_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="library.missing_media_workflows",
            description=(
                "查看当前用户最近缺集补库流程的安全状态；只返回剧名、季集、阶段、"
                "目标类型与是否已建立下载任务，不返回资源句柄、磁力、URL、路径或凭据。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 30,
                        "default": 10,
                    },
                },
                "additionalProperties": False,
            },
            validator=missing_workflow_arguments,
            context_handler=missing_workflows,
        )
    )
    registry.register(
        ToolSpec(
            name="library.check_updates",
            description=(
                "核对某部媒体是否有更新；剧集比较 TMDB 已播普通集与 Jellyfin / Emby 本地收录，"
                "电影核对本地存在性并提供需人工判断的资源站跟进。该结果用于本地/TMDB 对照，"
                "不能替代官方平台的实时更新公告。"
            ),
            risk=RiskLevel.READ,
            domains=("library", "official_progress"),
            source_kind="local_library",
            freshness="live",
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 120},
                    "media_type": {"type": "string", "enum": ["auto", "tv", "movie"]},
                    "tmdb_id": {"type": "string", "pattern": "^[0-9]{1,10}$"},
                    "season": {"type": "integer", "minimum": 1, "maximum": 100},
                    "as_of": {"type": "string", "format": "date"},
                },
                "additionalProperties": False,
            },
            handler=check_library_updates,
            validator=_library_update_arguments,
            examples=(
                "检查《某剧》有没有更新",
                "这部剧最新播到哪里而本地有多少",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="library.audit_library_episodes",
            description="有界枚举已配置媒体服务器中的剧集，并按可靠 TMDB 映射巡检截至指定日期的已播缺集。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "as_of": {"type": "string", "format": "date"},
                    "max_series": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "additionalProperties": False,
            },
            handler=audit_library_episodes,
            validator=_library_episode_audit_arguments,
            examples=(
                "巡检整个媒体库有没有缺集",
                "检查全部剧集的完整性",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="library.start_episode_audit",
            description="在用户确认后创建可恢复、可查询进度、可取消的后台全库剧集完整性检查。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "as_of": {"type": "string", "format": "date"},
                    "max_series": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "additionalProperties": False,
            },
            validator=start_episode_audit_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_start_episode_audit,
            context_confirmed_handler=start_episode_audit_confirmed,
        )
    )
    registry.register(
        ToolSpec(
            name="library.patrol_status",
            description="查询最近一次后台全库缺集巡检的安全摘要；不会触发巡检、资源搜索或下载。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=get_library_patrol_status,
            validator=patrol_status_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="library.patrol_policy",
            description="读取后台全库缺集巡检的启用、通知、间隔和单轮检查上限，不返回其他配置。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=summarize_patrol_policy,
            validator=patrol_policy_summary_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="library.set_patrol_policy",
            description="预检并在用户确认后修改全库缺集巡检的四项白名单策略，不接受配置键、凭据、URL 或路径。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "minProperties": 1,
                "properties": {
                    "enabled": {"type": "boolean"},
                    "notify_enabled": {"type": "boolean"},
                    "interval_hours": {"type": "integer", "minimum": 1, "maximum": 168},
                    "max_series": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "additionalProperties": False,
            },
            validator=patrol_policy_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                set_patrol_policy_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_patrol_policy_confirmation
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="library.trigger_patrol_now",
            description="预检并在用户确认后，按当前全库缺集巡检策略把单例后台任务排到现在；不修改策略、不搜索资源、不下载。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=patrol_trigger_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                trigger_patrol_now_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_trigger_patrol_now
            ),
            examples=("按当前策略立即巡检媒体库", "现在执行一次自动缺集巡检"),
        )
    )
    registry.register(
        ToolSpec(
            name="library.count_series_episodes",
            description="直接读取已配置 Jellyfin / Emby 中指定剧集的本地普通集数量与季度分布；不访问 TMDB，也不判断缺集。",
            risk=RiskLevel.READ,
            domains=("library", "episode_numbering"),
            source_kind="local_library",
            freshness="live",
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 120},
                    "tmdb_id": {"type": "string", "pattern": "^[0-9]{1,10}$"},
                    "library_name": {"type": "string", "minLength": 1, "maxLength": 80},
                },
                "additionalProperties": False,
            },
            handler=count_series_episodes,
            validator=count_series_episodes_arguments,
            examples=(
                "媒体库中《某剧》一共有多少集",
                "这部剧本地有几季几集",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="library.audit_episodes",
            description="核对媒体库剧集与 TMDB 截止日期前已播普通集，报告缺集或可更新集。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 120},
                    "tmdb_id": {"type": "string", "pattern": "^[0-9]{1,10}$"},
                    "library_name": {"type": "string", "minLength": 1, "maxLength": 80},
                    "season": {"type": "integer", "minimum": 1, "maximum": 100},
                    "target_episode": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                    },
                    "as_of": {"type": "string", "format": "date"},
                },
                "additionalProperties": False,
            },
            handler=audit_series_episodes,
            validator=_episode_audit_arguments,
        )
    )
