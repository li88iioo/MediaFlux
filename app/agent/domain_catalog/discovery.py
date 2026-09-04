"""discovery 领域的 Agent 原子工具声明。"""

from __future__ import annotations

from app.agent.discovery_actions import (
    bangumi_calendar,
    recommend_discovery,
    search_discovery,
)
from app.agent.discovery_actions import (
    calendar_arguments as bangumi_calendar_arguments,
)
from app.agent.discovery_actions import (
    recommend_arguments as discovery_recommend_arguments,
)
from app.agent.discovery_actions import (
    search_arguments as discovery_search_arguments,
)
from app.agent.discovery_mapping_actions import (
    confirm_discovery_mapping_confirmed,
    discovery_confirm_mapping_arguments,
    discovery_detail_arguments,
    discovery_mapping_candidates_arguments,
    get_discovery_detail,
    get_discovery_mapping_candidates,
    prepare_confirm_discovery_mapping,
)
from app.agent.discovery_watchlist_actions import (
    add_watchlist_arguments,
    add_watchlist_confirmed,
    get_watchlist_summary,
    list_watchlist_summaries,
    prepare_add_watchlist,
    prepare_remove_watchlist,
    remove_watchlist_arguments,
    remove_watchlist_confirmed,
    watchlist_summaries_arguments,
    watchlist_summary_arguments,
)
from app.agent.media_rating_actions import (
    lookup_media_rating,
    media_rating_arguments,
)
from app.agent.models import (
    RiskLevel,
    ToolSpec,
)
from app.agent.web_search_actions import (
    search_web,
    web_search_arguments,
)


def register_specs(
    registry, *, resource_store, active_ingest_store, ingest_actions
) -> None:
    registry.register(
        ToolSpec(
            name="web.search",
            description=(
                "通过受控 Tavily Provider 搜索公开网页；用于核对官方平台当前更新进度、最新播出信息"
                "和其他时效性事实。结果受固定主机、缓存、频率和每日额度限制。"
            ),
            risk=RiskLevel.READ,
            domains=("official_progress", "research"),
            source_kind="public_web",
            freshness="cached",
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                    },
                    "topic": {
                        "type": "string",
                        "enum": ["general", "news"],
                        "default": "general",
                    },
                    "time_range": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year"],
                    },
                },
                "additionalProperties": False,
            },
            handler=search_web,
            validator=web_search_arguments,
            examples=(
                "搜索网上的最新消息",
                "联网查公开网页信息",
                "核对某部动画官方最新更新到第几集",
                "查询官方平台目前播到哪里",
                "最近有什么推荐的国漫",
                "今年或指定年份有哪些新剧",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="discovery.search",
            description="在已启用的 TMDB、豆瓣与 Bangumi 外部数据源中搜索影视元数据，不返回海报原始地址或配置值。",
            risk=RiskLevel.READ,
            domains=("discovery", "media_identity"),
            source_kind="metadata_catalog",
            freshness="live",
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 120,
                        "description": "1 到 120 个非控制可见字符；服务端会执行 NFKC 规范化并去除首尾空白。",
                    },
                    "page": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 1,
                    },
                    "providers": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "items": {
                            "type": "string",
                            "enum": ["tmdb", "douban", "bangumi"],
                        },
                    },
                    "media_type": {
                        "type": "string",
                        "enum": ["movie", "tv"],
                        "description": "用户明确要求电影或剧集时填写；服务端会再次过滤结果。",
                    },
                    "year": {
                        "type": "string",
                        "pattern": "^(?:19|20)[0-9]{2}$",
                        "description": "用户明确给出的四位年份；不得自行猜测或替换。",
                    },
                    "region": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 24,
                        "description": "用户明确给出的地区，例如欧美、日本、中国大陆。",
                    },
                    "genre": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 24,
                        "description": "用户明确给出的题材，例如科幻、悬疑、喜剧。",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 20,
                    },
                },
                "additionalProperties": False,
            },
            handler=search_discovery,
            validator=discovery_search_arguments,
            examples=(
                "从 TMDB 或豆瓣搜索影视资料",
                "查一部电影的外部元数据",
                "搜索 2026 年欧美科幻剧集",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="discovery.lookup_rating",
            description="按明确影视名称、类型和年份查询豆瓣评分；优先使用豆瓣结构化数据，必要时受控检索并读取已验证的豆瓣条目页。",
            risk=RiskLevel.READ,
            domains=("rating", "discovery"),
            source_kind="metadata_catalog",
            freshness="live",
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 120},
                    "media_type": {"type": "string", "enum": ["movie", "tv"]},
                    "year": {"type": "string", "pattern": "^(?:19|20)\\d{2}$"},
                    "allow_web_fallback": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            },
            handler=lookup_media_rating,
            validator=media_rating_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="discovery.detail",
            description="读取一个精确影视来源条目的安全详情和映射确认状态；不会写入映射、收藏、订阅或下载任务。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["provider", "external_id", "media_type"],
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["tmdb", "douban", "bangumi"],
                    },
                    "external_id": {"type": "string", "minLength": 1, "maxLength": 180},
                    "media_type": {"type": "string", "enum": ["movie", "tv"]},
                },
                "additionalProperties": False,
            },
            context_handler=get_discovery_detail,
            validator=discovery_detail_arguments,
            examples=("查看刚才第 2 个影视详情",),
        )
    )
    registry.register(
        ToolSpec(
            name="discovery.mapping_candidates",
            description="只读查询一个非 TMDB 来源条目的 TMDB 映射候选，并将内部候选身份短期绑定到当前会话；不会自动保存高置信映射。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["provider", "external_id", "media_type"],
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["tmdb", "douban", "bangumi"],
                    },
                    "external_id": {"type": "string", "minLength": 1, "maxLength": 180},
                    "media_type": {"type": "string", "enum": ["movie", "tv"]},
                },
                "additionalProperties": False,
            },
            context_handler=get_discovery_mapping_candidates,
            validator=discovery_mapping_candidates_arguments,
            workflow="discovery_mapping",
            workflow_stage=10,
            examples=("查看刚才第 2 个的 TMDB 映射候选",),
        )
    )
    registry.register(
        ToolSpec(
            name="discovery.confirm_mapping",
            description="预检并在用户确认后保存当前会话最近映射候选中的一个；候选会重新通过 TMDB 详情核验。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["candidate_number"],
                "properties": {
                    "candidate_number": {"type": "integer", "minimum": 1, "maximum": 5}
                },
                "additionalProperties": False,
            },
            validator=discovery_confirm_mapping_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_confirm_discovery_mapping,
            context_confirmed_handler=confirm_discovery_mapping_confirmed,
            workflow="discovery_mapping",
            workflow_stage=20,
            examples=("确认第 1 个映射",),
        )
    )
    registry.register(
        ToolSpec(
            name="discovery.watchlist_summaries",
            description="只读列出有界探索收藏摘要，仅含收藏编号、来源、媒体类型、标题和年份。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=list_watchlist_summaries,
            validator=watchlist_summaries_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="discovery.get_watchlist_summary",
            description="按精确收藏编号读取一条探索收藏的安全摘要。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["watchlist_number"],
                "properties": {"watchlist_number": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
            handler=get_watchlist_summary,
            validator=watchlist_summary_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="discovery.add_watchlist",
            description="预检并在用户确认后把一个精确影视条目加入本地探索收藏；不会下载资源。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["provider", "external_id", "media_type"],
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["tmdb", "douban", "bangumi"],
                    },
                    "external_id": {"type": "string", "minLength": 1, "maxLength": 180},
                    "media_type": {"type": "string", "enum": ["movie", "tv"]},
                },
                "additionalProperties": False,
            },
            validator=add_watchlist_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                add_watchlist_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_add_watchlist
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="discovery.remove_watchlist",
            description="预检并在用户确认后按精确收藏编号移除本地探索收藏；不会删除媒体文件或下载任务。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["watchlist_number"],
                "properties": {"watchlist_number": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
            validator=remove_watchlist_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                remove_watchlist_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_remove_watchlist
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="discovery.recommend",
            description=(
                "读取已启用的 TMDB 或豆瓣推荐列表；可按用户明确给出的年份、地区和题材做受控筛选，"
                "不返回海报地址、收藏状态或配置值。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["tmdb", "douban"],
                        "default": "tmdb",
                    },
                    "media_type": {
                        "type": "string",
                        "enum": ["movie", "tv"],
                        "default": "movie",
                    },
                    "page": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 1,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 10,
                    },
                    "year": {
                        "type": "string",
                        "pattern": "^(?:19|20)[0-9]{2}$",
                        "description": "用户明确给出的四位年份。",
                    },
                    "region": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 24,
                        "description": "用户明确给出的地区或剧集产地，例如美国、欧美、日本。",
                    },
                    "genre": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 24,
                        "description": "用户明确给出的题材，例如科幻、悬疑、喜剧。",
                    },
                },
                "additionalProperties": False,
            },
            handler=recommend_discovery,
            validator=discovery_recommend_arguments,
            examples=(
                "最近想看点科幻",
                "最近有什么推荐的国漫",
                "推荐几部电影",
                "推荐今年中国大陆的新动画剧集",
                "今年或指定年份有哪些新剧",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="bangumi.calendar",
            description="读取 Bangumi 本周或指定星期的放送日历，不返回图片地址、收藏状态或 Provider 配置。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "weekday": {"type": "integer", "minimum": 1, "maximum": 7},
                    "page": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 1,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 10,
                    },
                },
                "additionalProperties": False,
            },
            handler=bangumi_calendar,
            validator=bangumi_calendar_arguments,
            examples=(
                "看看本周追番日历",
                "今天有哪些动画更新",
            ),
        )
    )
