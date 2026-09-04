"""system 领域的 Agent 原子工具声明。"""

from __future__ import annotations

from typing import Any

from app import config
from app.agent.action_history import (
    action_history_arguments,
    list_action_history,
)
from app.agent.automation_actions import (
    automation_pipeline_arguments,
    diagnose_automation_pipeline,
)
from app.agent.config_actions import (
    media_server_arguments,
    test_media_server,
)
from app.agent.config_diagnosis_actions import diagnose_config
from app.agent.config_explain_actions import (
    config_component_arguments,
    explain_config_component,
)
from app.agent.durable_job_actions import (
    agent_job_status_arguments,
    cancel_agent_job_arguments,
    cancel_agent_job_confirmed,
    get_agent_job_status,
    prepare_cancel_agent_job,
)
from app.agent.feature_actions import (
    feature_state_arguments,
    feature_summary_arguments,
    prepare_feature_state_confirmation,
    set_feature_state_confirmed,
    summarize_feature_states,
    verify_feature_state_write,
)
from app.agent.feature_gate import is_agent_enabled
from app.agent.indexer_config_actions import (
    indexer_sites_arguments,
    indexer_sites_summary_arguments,
    prepare_indexer_sites_confirmation,
    set_indexer_sites_confirmed,
    summarize_indexer_sites,
    verify_indexer_sites_write,
)
from app.agent.media_server_actions import (
    diagnose_media_servers,
    media_server_diagnosis_arguments,
)
from app.agent.models import (
    Evidence,
    RiskLevel,
    ToolResult,
    ToolSpec,
)
from app.agent.provider_actions import (
    execute_provider_change_confirmed,
    list_provider_capabilities,
    prepare_provider_change_execution,
    preview_provider_change,
    provider_capabilities_arguments,
    provider_change_status,
    provider_plan_arguments,
    provider_plan_ref_arguments,
    provider_query_arguments,
    query_provider,
)
from app.agent.recognition_toggle_actions import (
    prepare_set_recognition_rule_enabled,
    recognition_rule_enabled_arguments,
    set_recognition_rule_enabled_confirmed,
)
from app.agent.safe_policy_actions import (
    SAFE_POLICY_IDS,
    prepare_safe_policy_confirmation,
    safe_policy_arguments,
    safe_policy_summary_arguments,
    set_safe_policy_confirmed,
    summarize_safe_policies,
)
from app.agent.telegram_test_actions import (
    prepare_telegram_test_notification,
    send_telegram_test_notification_confirmed,
    telegram_test_arguments,
)
from app.indexers.config import INDEXER_SITE_ORDER

from .shared import (
    _no_arguments,
    _now,
)


def register_specs(
    registry, *, resource_store, active_ingest_store, ingest_actions
) -> None:
    registry.register(
        ToolSpec(
            name="agent.runtime_status",
            description="只读返回 Media Agent 总开关、Telegram 接入和模型路由的当前启用状态，不返回令牌、密钥或供应商配置值。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=lambda _arguments: ToolResult(
                ok=True,
                status="success",
                summary="Media Agent 运行状态已读取",
                data={
                    "agent_enabled": is_agent_enabled(),
                    "telegram_enabled": config.get_bool("TG_AGENT_ENABLED", False),
                    "model_routing_enabled": config.get_bool("AGENT_LLM_ENABLED"),
                },
                evidence=[
                    Evidence(
                        "agent_runtime",
                        "读取当前进程可见的非敏感 Agent 功能开关。",
                        _now(),
                    )
                ],
                suggestions=["如需调整 Telegram Agent，请发送 /agent。"],
            ),
            validator=_no_arguments,
            domains=("agent", "system"),
            source_kind="system_state",
            freshness="live",
            examples=("Agent 现在开启了吗", "查看智能助手状态"),
        )
    )
    registry.register(
        ToolSpec(
            name="provider.capabilities",
            description=(
                "列出媒体服务器与 qBittorrent 当前已开放的原生语义操作、参数和非敏感 profile；"
                "不会连接上游，也不会返回地址或凭据。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["media", "qbittorrent"],
                    },
                    "intent": {"type": "string", "maxLength": 160},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 24,
                        "default": 12,
                    },
                },
                "additionalProperties": False,
            },
            handler=list_provider_capabilities,
            validator=provider_capabilities_arguments,
            model_name="mf_provider_capabilities",
            related_tools=("provider.query",),
            domains=("media_library", "downloads", "system"),
            source_kind="provider_catalog",
            freshness="snapshot",
            workflow="provider_change",
            workflow_stage=10,
            examples=(
                "查看 Jellyfin 可以读取哪些信息",
                "统计媒体库中电影、剧集和单集总数",
                "查看动漫媒体库有多少部剧集",
                "查看 qBittorrent 可用能力",
                "我能让你检查哪些媒体服务器内容",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="provider.query",
            description=(
                "调用静态目录中已登记的 Jellyfin、Emby 或 qBittorrent 只读操作。"
                "可读取全库统计，也可先列出媒体库后按指定媒体库统计。"
                "profile 和 operation 必须先从 Provider 能力清单获取；禁止任意 URL、HTTP 方法、header 或凭据。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["profile_ref", "operation", "arguments"],
                "properties": {
                    "profile_ref": {"type": "string", "minLength": 1, "maxLength": 80},
                    "operation": {"type": "string", "minLength": 3, "maxLength": 96},
                    "arguments": {"type": "object", "additionalProperties": True},
                },
                "additionalProperties": False,
            },
            context_handler=query_provider,
            validator=provider_query_arguments,
            model_name="mf_provider_query",
            related_tools=(
                "provider.capabilities",
                "provider.change.preview",
            ),
            domains=("media_library", "downloads", "episodes", "system"),
            source_kind="provider_api",
            freshness="live",
            workflow="provider_change",
            workflow_stage=20,
            examples=(
                "读取 Jellyfin 媒体库",
                "统计媒体库中有多少电影、剧集和单集",
                "统计动漫媒体库中有多少部剧集",
                "在媒体服务器中搜索一部剧",
                "读取 qBittorrent 下载任务",
                "查看刚才下载任务的文件",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="provider.change.preview",
            description=(
                "为静态目录中已开放的媒体服务器或 qBittorrent 写操作执行实时预检并冻结短期计划。"
                "只能使用先前只读查询返回的对象引用；不会在预检阶段修改 Provider。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["profile_ref", "operation", "arguments"],
                "properties": {
                    "profile_ref": {"type": "string", "minLength": 1, "maxLength": 80},
                    "operation": {"type": "string", "minLength": 3, "maxLength": 96},
                    "arguments": {"type": "object", "additionalProperties": True},
                },
                "additionalProperties": False,
            },
            context_handler=preview_provider_change,
            validator=provider_plan_arguments,
            model_name="mf_provider_change_preview",
            related_tools=("provider.change.execute",),
            domains=("media_library", "downloads"),
            source_kind="provider_change_plan",
            freshness="live",
            workflow="provider_change",
            workflow_stage=30,
            examples=(
                "预览刷新刚才选中的媒体库",
                "预览暂停刚才选中的 qB 下载任务",
                "预览移除 qB 任务但保留文件",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="provider.change.execute",
            description=(
                "在用户确认后执行一个 owner/session 绑定的冻结 Provider 写计划。"
                "不能接收新的目标或写参数；计划只能原子认领并执行一次。"
            ),
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "required": ["plan_ref"],
                "properties": {
                    "plan_ref": {"type": "string", "pattern": "^PP-[0-9A-Fa-f]{24}$"},
                },
                "additionalProperties": False,
            },
            validator=provider_plan_ref_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_provider_change_execution,
            context_confirmed_handler=execute_provider_change_confirmed,
            model_name="mf_provider_change_execute",
            domains=("media_library", "downloads"),
            source_kind="provider_change_plan",
            freshness="live",
            workflow="provider_change",
            workflow_stage=40,
            examples=("确认执行刚才的 Provider 写计划",),
        )
    )
    registry.register(
        ToolSpec(
            name="provider.job.status",
            description="读取当前会话中指定 Provider 写计划的持久状态、公开目标摘要与写后核验结果。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["plan_ref"],
                "properties": {
                    "plan_ref": {"type": "string", "pattern": "^PP-[0-9A-Fa-f]{24}$"},
                },
                "additionalProperties": False,
            },
            context_handler=provider_change_status,
            validator=provider_plan_ref_arguments,
            model_name="mf_provider_job_status",
            domains=("media_library", "downloads", "agent"),
            source_kind="provider_change_plan",
            freshness="live",
            examples=("查看刚才 Provider 操作是否完成",),
        )
    )
    registry.register(
        ToolSpec(
            name="config.diagnose",
            description="检查媒体服务器、TMDB、下载器、STRM 与 AI 回退配置是否完整，不返回配置值。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=diagnose_config,
            validator=_no_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="config.explain_component",
            description="解释一个白名单配置组件的状态、必要字段标签、受影响能力与安全下一步，不返回配置键或配置值。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["component"],
                "properties": {
                    "component": {
                        "type": "string",
                        "enum": [
                            "jellyfin",
                            "emby",
                            "tmdb",
                            "qbittorrent",
                            "strm",
                            "ai_recognition",
                            "discovery",
                            "douban",
                            "resource_results",
                            "indexer_search",
                        ],
                    },
                },
                "additionalProperties": False,
            },
            handler=explain_config_component,
            validator=config_component_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="config.feature_summary",
            description="只读汇总媒体探索、资源检索与联网搜索的启用状态和依赖可用性，不返回配置值或供应商凭据。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=summarize_feature_states,
            validator=feature_summary_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="automation.diagnose_pipeline",
            description="只读汇总下载、RSS、光鸭整理与 STRM 的本地自动化状态，不访问外部服务且不返回路径、凭据或业务标识。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=diagnose_automation_pipeline,
            validator=automation_pipeline_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="config.diagnose_media_servers",
            description="使用服务端当前生效配置汇总诊断 Jellyfin 12 与 Emby / Jellyfin 10.x 节点的连通性、产品版本和兼容槽位，不返回地址、服务器名称或凭据。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=diagnose_media_servers,
            validator=media_server_diagnosis_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="config.test_media_server",
            description="使用服务端当前生效配置测试 Jellyfin 或 Emby / Jellyfin 10.x 的连通性与鉴权，不返回地址或凭据。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["server_type"],
                "properties": {
                    "server_type": {"type": "string", "enum": ["jellyfin", "emby"]},
                },
                "additionalProperties": False,
            },
            handler=test_media_server,
            validator=media_server_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="recognition.set_rule_enabled",
            description="预检并在用户确认后，按明确规则类型和编号启用或停用一条识别规则；不会修改规则内容、映射、别名或优先级。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["rule_type", "rule_id", "enabled"],
                "properties": {
                    "rule_type": {
                        "type": "string",
                        "enum": [
                            "preprocess_rule",
                            "tmdb_regex_rule",
                            "knowledge_entry",
                        ],
                    },
                    "rule_id": {"type": "integer", "minimum": 1},
                    "enabled": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            validator=recognition_rule_enabled_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_set_recognition_rule_enabled
            ),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                set_recognition_rule_enabled_confirmed
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="config.indexer_sites_summary",
            description="读取当前固定白名单资源站点的选择，仅返回站点 ID、展示名和数量。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=summarize_indexer_sites,
            validator=indexer_sites_summary_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="config.set_indexer_sites",
            description="预检并在用户确认后更新固定白名单资源站点，不接受配置键、URL、凭据、Cookie 或路径。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["site_ids"],
                "properties": {
                    "site_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": len(INDEXER_SITE_ORDER),
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "enum": list(INDEXER_SITE_ORDER),
                        },
                    },
                    "enable_search": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            validator=indexer_sites_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_indexer_sites_confirmation
            ),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                set_indexer_sites_confirmed
            ),
            post_write_verifier=verify_indexer_sites_write,
        )
    )
    registry.register(
        ToolSpec(
            name="telegram.send_test_notification",
            description="预检并在用户确认后向当前已配置会话发送一条固定 Telegram 连接测试消息；不接受消息、凭据或会话参数。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=telegram_test_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                send_telegram_test_notification_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_telegram_test_notification
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="config.safe_policy_summary",
            description="读取 Agent 可安全管理的固定白名单策略，只返回公开值和环境托管状态。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=summarize_safe_policies,
            validator=safe_policy_summary_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="config.set_safe_policy",
            description="预检并在用户确认后修改一项固定白名单非敏感策略，不接受任意配置键、凭据、URL 或路径。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["policy", "value"],
                "properties": {
                    "policy": {"type": "string", "enum": list(SAFE_POLICY_IDS)},
                    "value": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "integer"},
                        ],
                    },
                },
                "additionalProperties": False,
            },
            validator=safe_policy_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                set_safe_policy_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_safe_policy_confirmation
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="config.set_feature_state",
            description="预检并在用户确认后开启或关闭一个非敏感白名单功能，不接受配置键或任意配置值。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["feature", "enabled"],
                "properties": {
                    "feature": {
                        "type": "string",
                        "enum": [
                            "discovery",
                            "douban",
                            "resource_results",
                            "indexer_search",
                            "web_search",
                            "offline_magnet",
                            "offline_ed2k",
                            "offline_http",
                            "strm_metadata",
                            "download_verification_notify",
                        ],
                    },
                    "enabled": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            validator=feature_state_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_feature_state_confirmation
            ),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                set_feature_state_confirmed
            ),
            post_write_verifier=verify_feature_state_write,
        )
    )
    registry.register(
        ToolSpec(
            name="agent.job_status",
            description="查询当前登录会话发起的后台全库检查进度与安全结果；不会启动或修改任务。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "pattern": "^job_[A-Za-z0-9_-]{16,80}$",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                    },
                },
                "additionalProperties": False,
            },
            validator=agent_job_status_arguments,
            context_handler=get_agent_job_status,
        )
    )
    registry.register(
        ToolSpec(
            name="agent.cancel_job",
            description="预检并在用户确认后安全取消当前会话的后台全库检查。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "pattern": "^job_[A-Za-z0-9_-]{16,80}$",
                    },
                },
                "additionalProperties": False,
            },
            validator=cancel_agent_job_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_cancel_agent_job,
            context_confirmed_handler=cancel_agent_job_confirmed,
        )
    )

    def capabilities(_arguments: dict[str, Any]) -> ToolResult:
        tools = registry.capabilities()
        groups = (
            (
                "媒体与媒体库",
                {"library", "provider", "playback"},
                "查询 Jellyfin/Emby、媒体总量、单部收录、缺集与播放状态",
            ),
            (
                "资源发现",
                {"discovery", "indexer", "resource", "web"},
                "查询 TMDB/Bangumi、推荐公开内容、搜索资源站并读取公开网页",
            ),
            (
                "下载管理",
                {"download", "downloads", "ingest", "qb"},
                "查询 qBittorrent 与项目下载任务，并在确认后提交、暂停或清理",
            ),
            (
                "光鸭云盘",
                {"cloud", "guangya"},
                "浏览和检索云盘；预览后创建目录、改名、移动、整理或离线转存",
            ),
            (
                "订阅与自动化",
                {"rss", "media", "automation"},
                "管理 RSS 规则和媒体追更订阅，检查自动化执行状态",
            ),
            (
                "STRM 与本地整理",
                {"strm", "local_media", "organize"},
                "检查或触发 STRM、本地媒体识别、整理和入库复核",
            ),
            (
                "项目运维",
                {"agent", "workspace", "config", "feature", "telegram"},
                "查看系统健康、配置、任务与审计；配置变更均需人工确认",
            ),
        )
        prefixes = [str(item.get("name") or "").partition(".")[0] for item in tools]
        capability_groups: list[dict[str, Any]] = []
        assigned: set[str] = set()
        for title, members, description in groups:
            count = sum(1 for prefix in prefixes if prefix in members)
            if not count:
                continue
            assigned.update(members)
            capability_groups.append(
                {"title": title, "description": description, "tool_count": count}
            )
        remaining = sum(1 for prefix in prefixes if prefix and prefix not in assigned)
        if remaining:
            capability_groups.append(
                {
                    "title": "其他项目能力",
                    "description": "当前注册表中的其他受控读取与确认后执行能力",
                    "tool_count": remaining,
                }
            )
        return ToolResult(
            ok=True,
            status="success",
            summary=f"当前提供 {len(tools)} 个受控项目能力，写操作统一经过人工确认",
            data={
                "total_tools": len(tools),
                "groups": capability_groups,
                "write_policy": "只读能力可直接执行；写入和危险操作只生成冻结计划，确认后由确定性代码执行。",
            },
            evidence=[
                Evidence("agent_registry", "能力来自服务端显式工具注册表。", _now())
            ],
            suggestions=[
                "可以问：查看系统简报、检查项目配置、诊断下载队列、诊断 RSS 订阅、测试 Jellyfin 或 Emby 连接、关闭媒体探索（需确认）、预览光鸭整理计划、立即整理光鸭云盘、查看 STRM 同步进度、在媒体库找一部影片、从外部影视源搜索一部影片、推荐几部电影或电视剧、查看今天有什么番剧、搜索某部影片的资源、核对某部剧是否缺集、检查某部剧是否有更新。"
            ],
        )

    registry.register(
        ToolSpec(
            name="agent.action_history",
            description="查看最近经确认执行的 Agent 动作审计，仅返回脱敏状态、聚合计数与耗时。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 20,
                    },
                    "outcome": {
                        "type": "string",
                        "enum": ["all", "success", "failed"],
                        "default": "all",
                    },
                },
                "additionalProperties": False,
            },
            validator=action_history_arguments,
            context_handler=list_action_history,
        )
    )
    registry.register(
        ToolSpec(
            name="agent.capabilities",
            description=(
                "回答 MediaFlux Media Agent 是谁、能做什么，并列出当前可以读取或经确认执行的项目能力；"
                "这是全局能力说明，不是仅限光鸭、下载或某个单一领域的能力列表。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=capabilities,
            validator=_no_arguments,
            domains=("agent", "system"),
            source_kind="agent_capability_catalog",
            freshness="derived",
            examples=(
                "你是谁？你能做什么？",
                "MediaFlux Agent 支持哪些项目能力",
                "列出你可以读取和经确认执行的功能",
            ),
        )
    )
