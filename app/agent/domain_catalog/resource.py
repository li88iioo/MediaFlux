"""resource 领域的 Agent 原子工具声明。"""

from __future__ import annotations

from app.agent.indexer_actions import (
    search_arguments as indexer_search_arguments,
)
from app.agent.indexer_actions import (
    search_resources,
)
from app.agent.indexer_readiness_actions import (
    diagnose_indexer_readiness,
    indexer_readiness_arguments,
)
from app.agent.ingest_actions import (
    ingest_inspect_arguments,
    ingest_status_arguments,
    ingest_submit_arguments,
)
from app.agent.models import (
    RiskLevel,
    ToolSpec,
)


def register_specs(
    registry, *, resource_store, active_ingest_store, ingest_actions
) -> None:
    registry.register(
        ToolSpec(
            name="indexer.diagnose_readiness",
            description="只读检查多站资源索引器的本地开关、启用站点与能力声明；不访问资源站、网络或文件系统，也不返回 URL、Cookie 或凭据。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=diagnose_indexer_readiness,
            validator=indexer_readiness_arguments,
            examples=(
                "为什么没搜到资源",
                "资源站连不上怎么办",
                "检查多站资源搜索状态",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="indexer.search_resources",
            description=(
                "在已启用的多站索引中搜索短期资源结果，只返回 opaque result_id 与公开元数据。"
                "sites 可把本次读取严格限制到指定站点且不会修改站点配置；例如 "
                'sites=["sukebei"] 只查询 Sukebei。需要近期结果时使用 '
                "sort_mode=published_desc。"
                "可提交的候选会同时返回 owner/session 绑定的 resource_candidates_ref，后续资源检查或"
                "提交必须原样使用该引用。"
                "可用于交叉核对连载资源跟进到哪一集，但资源标题只能作为旁证，不能证明官方播出进度。"
            ),
            risk=RiskLevel.READ,
            domains=("resource_search", "official_progress"),
            source_kind="resource_index",
            freshness="realtime",
            parameters={
                "type": "object",
                "required": ["title"],
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 120},
                    "original_title": {"type": "string", "maxLength": 120},
                    "english_title": {"type": "string", "maxLength": 120},
                    "aliases": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {"type": "string", "minLength": 1, "maxLength": 120},
                    },
                    "year": {"type": "integer", "minimum": 1800, "maximum": 2200},
                    "media_type": {
                        "type": "string",
                        "enum": ["", "movie", "tv", "anime"],
                    },
                    "page": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 1,
                    },
                    "sort_mode": {
                        "type": "string",
                        "enum": [
                            "published_desc",
                            "relevance_desc",
                            "episode_desc",
                            "seeders_desc",
                            "size_desc",
                            "size_asc",
                        ],
                        "default": "relevance_desc",
                    },
                    "sites": {
                        "type": "array",
                        "maxItems": 16,
                        "items": {"type": "string", "pattern": "^[a-z0-9_-]{1,32}$"},
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
            handler=search_resources,
            validator=indexer_search_arguments,
            related_tools=("ingest.inspect", "ingest.submit"),
            examples=(
                "搜索《某片》的下载资源",
                "找种子或磁力资源",
                "帮我下载《某片》",
                "下载某部电视剧",
                "核对某部连载动画的资源索引跟进到第几集",
                "检查订阅更新并在需要时搜索资源",
                '最近有什么新出的步兵资源，仅用 sites=["sukebei"] 并按发布时间倒序搜索 uncensored',
                '查看 Sukebei 最近发布的无码资源，不查询其他索引站',
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="ingest.inspect",
            description=(
                "统一只读检查资源接入来源：可识别光鸭官方分享、Magnet、ED2K、明确 HTTP(S) 下载直链，"
                "或读取当前会话最近资源搜索候选。原始链接、分享令牌、file_id 与索引 result_id 只保存在"
                "owner 绑定的短期服务端快照中；普通网页链接不会创建下载任务。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "source_type": {
                        "type": "string",
                        "enum": [
                            "auto",
                            "direct_url",
                            "guangya_share",
                            "resource_candidates",
                        ],
                        "default": "auto",
                    },
                    "input": {"type": "string", "maxLength": 8192},
                    "resource_candidates_ref": {
                        "type": "string",
                        "pattern": "^ref_[A-Za-z0-9_-]{16,160}$",
                    },
                },
                "additionalProperties": False,
            },
            context_handler=ingest_actions.inspect,
            validator=ingest_inspect_arguments,
            domains=("downloads", "resource_search", "cloud_files"),
            source_kind="ingest_snapshot",
            freshness="live",
            workflow="ingest_submit",
            workflow_stage=10,
            related_tools=("ingest.submit",),
            examples=(
                "解析这个光鸭分享链接",
                "检查这个磁力链接能否下载",
                "查看刚才搜索到的资源候选",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="ingest.submit",
            description=(
                "在用户确认后统一提交最近检查的直链或光鸭分享，或按资源搜索候选序号提交。"
                "资源搜索完成后应把返回的 resource_candidates_ref 原样传入，确保同轮与后续对话均绑定"
                "同一份候选快照；直链或分享检查完成后应把 ingest_snapshot_ref 原样传入。"
                "直链和资源候选可选 qB、光鸭或两边；省略 target 或使用 preferred 时，在预检中采用用户保存的默认下载目标，没有偏好则使用光鸭；目标随计划冻结，确认期间不随偏好变化。光鸭分享始终只转存到光鸭。确认参数不包含链接、"
                "访问令牌、云端 file_id、内部 result_id 或后端任务标识。"
            ),
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "required": ["source_type"],
                "properties": {
                    "source_type": {
                        "type": "string",
                        "enum": ["direct_url", "guangya_share", "resource_candidates"],
                    },
                    "target": {
                        "type": "string", "enum": ["preferred", "qb", "guangya", "both"],
                        "description": "省略或 preferred 使用保存的默认目标；显式 qb/guangya/both 优先。",
                    },
                    "positions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 200,
                        "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    "resource_candidates_ref": {
                        "type": "string",
                        "pattern": "^ref_[A-Za-z0-9_-]{16,160}$",
                    },
                    "ingest_snapshot_ref": {
                        "type": "string",
                        "pattern": "^ref_[A-Za-z0-9_-]{16,160}$",
                    },
                },
                "additionalProperties": False,
            },
            validator=ingest_submit_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=ingest_actions.prepare_submit,
            context_confirmed_handler=ingest_actions.execute_submit,
            related_tools=("media.preferences",),
            domains=("downloads", "resource_search", "cloud_files"),
            source_kind="ingest_snapshot",
            freshness="live",
            workflow="ingest_submit",
            workflow_stage=20,
            examples=(
                "按我的默认下载目标提交刚才的资源",
                "把刚才的磁力提交到 qB",
                "把这个光鸭分享全部转存",
                "把刚才第 1、3 个资源提交到两边，并使用搜索结果返回的 resource_candidates_ref",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="ingest.status",
            description=(
                "按公开请求编号读取统一资源接入状态，覆盖 qB、光鸭、整理与 STRM 阶段；"
                "不返回链接、路径、哈希或后端任务标识。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["request_number"],
                "properties": {"request_number": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
            context_handler=ingest_actions.status,
            validator=ingest_status_arguments,
            domains=("downloads", "organize", "strm"),
            source_kind="download_request",
            freshness="live",
            examples=("查询资源请求 12 的状态", "刚才提交的资源到哪一步了"),
        )
    )
