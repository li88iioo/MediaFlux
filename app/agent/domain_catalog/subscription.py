"""subscription 领域的 Agent 原子工具声明。"""

from __future__ import annotations

from app.agent.media_consumption_actions import (
    clear_preferences_confirmed,
    continue_watching_arguments,
    get_continue_watching,
    get_preferences,
    get_recently_added,
    get_recently_played,
    get_subscription_notification_rule,
    get_today_summary,
    notification_rule_arguments,
    notification_rule_update_arguments,
    preferences_update_arguments,
    prepare_clear_preferences,
    prepare_reset_subscription_notification_rule,
    prepare_set_preferences,
    prepare_set_subscription_notification_rule,
    recently_added_arguments,
    recently_played_arguments,
    reset_subscription_notification_rule_confirmed,
    set_preferences_confirmed,
    set_subscription_notification_rule_confirmed,
)
from app.agent.media_consumption_actions import (
    empty_arguments as media_consumption_empty_arguments,
)
from app.agent.media_subscription_actions import (
    create_media_subscription_confirmed,
    delete_media_subscription_confirmed,
    get_media_subscription_policy,
    get_media_subscription_summary,
    inspect_media_subscription_updates,
    list_media_subscription_summaries,
    media_subscription_create_arguments,
    media_subscription_delete_arguments,
    media_subscription_enabled_arguments,
    media_subscription_policy_arguments,
    media_subscription_policy_update_arguments,
    media_subscription_summaries_arguments,
    media_subscription_summary_arguments,
    media_subscription_updates_arguments,
    prepare_create_media_subscription,
    prepare_delete_media_subscription,
    prepare_set_media_subscription_enabled,
    prepare_set_media_subscription_policy,
    set_media_subscription_enabled_confirmed,
    set_media_subscription_policy_confirmed,
)
from app.agent.models import (
    RiskLevel,
    ToolSpec,
)
from app.agent.rss_actions import (
    diagnose_rss,
    get_rss_recent_activity,
    get_rss_subscription_summary,
    list_rss_subscription_summaries,
    rss_diagnosis_arguments,
    rss_subscription_summaries_arguments,
    rss_subscription_summary_arguments,
)
from app.agent.rss_download_actions import (
    prepare_rss_pending_download,
    rss_pending_download_arguments,
    submit_pending_rss_to_qb_confirmed,
)
from app.agent.rss_entry_actions import (
    list_rss_entry_summaries,
    mark_rss_entries_confirmed,
    prepare_mark_rss_entries,
    prepare_submit_rss_entries,
    rss_entry_summaries_arguments,
    rss_mark_entries_arguments,
    rss_submit_entries_arguments,
    submit_rss_entries_confirmed,
)
from app.agent.rss_refresh_actions import (
    prepare_rss_subscription_refresh,
    prepare_rss_subscriptions_refresh,
    refresh_rss_subscription_confirmed,
    refresh_rss_subscriptions_confirmed,
    rss_refresh_subscription_arguments,
    rss_refresh_subscriptions_arguments,
)
from app.agent.rss_retry_actions import (
    prepare_rss_failure_retry,
    retry_failed_rss_to_qb_confirmed,
    rss_failure_retry_arguments,
)
from app.agent.rss_subscription_control_actions import (
    create_rss_subscription_confirmed,
    delete_rss_subscription_confirmed,
    prepare_create_rss_subscription,
    prepare_delete_rss_subscription,
    prepare_update_rss_subscription,
    rss_create_subscription_arguments,
    rss_delete_subscription_arguments,
    rss_update_subscription_arguments,
    update_rss_subscription_confirmed,
)


def register_specs(
    registry, *, resource_store, active_ingest_store, ingest_actions
) -> None:
    registry.register(
        ToolSpec(
            name="rss.diagnose",
            description="只读诊断 RSS 订阅、待处理、失败与长期提交中条目，不访问订阅源且不返回 URL、GUID、payload 或路径。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=diagnose_rss,
            validator=rss_diagnosis_arguments,
            examples=(
                "RSS 订阅为什么没有更新",
                "检查 RSS 待处理和失败项目",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="rss.subscription_summaries",
            description=(
                "只读列出 RSS 规则订阅（不是媒体追更订阅）的安全摘要，仅含编号、名称、"
                "启用/调度状态和条目计数，不返回 URL、过滤词、正文或路径。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=list_rss_subscription_summaries,
            validator=rss_subscription_summaries_arguments,
            examples=(
                "我有哪些 RSS 订阅",
                "我订阅了哪些 RSS",
                "我订阅了那些 RSS",
                "列出 RSS 订阅和启用状态",
                "查看 RSS 规则",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="rss.get_subscription_summary",
            description="按精确订阅编号读取 RSS 安全摘要，仅含名称、启用/调度状态和条目计数，不返回 URL、过滤词、正文或路径。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["subscription_id"],
                "properties": {"subscription_id": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
            handler=get_rss_subscription_summary,
            validator=rss_subscription_summary_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="rss.create_subscription",
            description="预检并在用户确认后创建一个 RSS 订阅；支持订阅地址、过滤、刷新、下载目标和媒体去重配置，不接受任意下载路径或云端目录标识。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["name", "urls"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 160},
                    "urls": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                    },
                    "exclude_keywords": {"type": "string", "maxLength": 1000},
                    "action": {"type": "string", "enum": ["subscribe", "download"]},
                    "enabled": {"type": "boolean"},
                    "refresh_interval_minutes": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 10080,
                    },
                    "download_method": {
                        "type": "string",
                        "enum": ["", "qb", "guangya"],
                    },
                    "media_tmdb_id": {"type": "string", "maxLength": 10},
                    "media_default_season": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                    },
                    "skip_existing_episodes": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            validator=rss_create_subscription_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                create_rss_subscription_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_create_rss_subscription
            ),
            examples=(
                "新增一个 RSS 订阅",
                "订阅这个 RSS 地址并用 qB 下载",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="rss.update_subscription",
            description="预检并在用户确认后更新一个指定 RSS 订阅的名称、地址、过滤、刷新、下载目标或媒体去重配置；不接受任意路径。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["subscription_id"],
                "properties": {
                    "subscription_id": {"type": "integer", "minimum": 1},
                    "name": {"type": "string", "minLength": 1, "maxLength": 160},
                    "urls": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                    },
                    "exclude_keywords": {"type": "string", "maxLength": 1000},
                    "action": {"type": "string", "enum": ["subscribe", "download"]},
                    "enabled": {"type": "boolean"},
                    "refresh_interval_minutes": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 10080,
                    },
                    "download_method": {
                        "type": "string",
                        "enum": ["", "qb", "guangya"],
                    },
                    "media_tmdb_id": {"type": "string", "maxLength": 10},
                    "media_default_season": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                    },
                    "skip_existing_episodes": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            validator=rss_update_subscription_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                update_rss_subscription_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_update_rss_subscription
            ),
            examples=(
                "修改 RSS 订阅 2 的过滤词",
                "把 RSS 订阅 3 改为每 30 分钟刷新",
                "更新这个 RSS 订阅的地址",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="rss.delete_subscription",
            description="预检并在用户确认后永久删除一个指定 RSS 订阅及其本地条目记录；不删除下载任务或已下载文件。",
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "required": ["subscription_id"],
                "properties": {
                    "subscription_id": {"type": "integer", "minimum": 1},
                },
                "additionalProperties": False,
            },
            validator=rss_delete_subscription_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                delete_rss_subscription_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_delete_rss_subscription
            ),
            examples=(
                "删除 RSS 订阅 2",
                "移除编号 3 的 RSS 订阅",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="media.subscription_summaries",
            description=(
                "只读列出影视/动画媒体追更订阅（不是 RSS 规则订阅）摘要，仅含编号、"
                "标题、媒体类型、启用状态和缺失数量。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=list_media_subscription_summaries,
            validator=media_subscription_summaries_arguments,
            examples=(
                "我订阅了哪些媒体",
                "列出当前的追更订阅",
                "看看我的媒体订阅",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="media.subscription_updates",
            description=(
                "实时检查全部媒体追更订阅：逐条比较 TMDB 已播清单与 Jellyfin/Emby 本地库存，"
                "并对确认缺失项执行有界多站资源搜索；只返回下载建议，不提交 qBittorrent 或光鸭。"
            ),
            risk=RiskLevel.READ,
            domains=("subscriptions", "resource_search"),
            source_kind="system_state",
            freshness="live",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=inspect_media_subscription_updates,
            validator=media_subscription_updates_arguments,
            examples=(
                "我订阅的媒体又更新吗",
                "检查追更订阅有没有新集",
                "看看订阅缺哪些集并搜索资源",
                "查看我的追更和 RSS 更新情况",
                "检查订阅更新",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="media.get_subscription_summary",
            description="按精确订阅编号读取一条媒体追更订阅的安全摘要。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["subscription_id"],
                "properties": {"subscription_id": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
            handler=get_media_subscription_summary,
            validator=media_subscription_summary_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="media.get_subscription_policy",
            description="按精确订阅编号读取追更范围、动作模式、下载目标和检查周期；不返回站点明细或凭据。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["subscription_id"],
                "properties": {"subscription_id": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
            handler=get_media_subscription_policy,
            validator=media_subscription_policy_arguments,
            examples=(
                "查看媒体订阅 1 的追更策略",
                "订阅 1 多久检查一次，下载到哪里",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="media.set_subscription_policy",
            description="预检并在用户确认后修改一个媒体追更订阅的追更范围、动作模式、下载目标或检查周期；不会立即检查或下载。",
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "required": ["subscription_id"],
                "properties": {
                    "subscription_id": {"type": "integer", "minimum": 1},
                    "monitor_mode": {
                        "type": "string",
                        "enum": ["missing", "future", "selected"],
                    },
                    "seasons": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "include_specials": {"type": "boolean"},
                    "action": {"type": "string", "enum": ["notify", "confirm", "auto"]},
                    "download_target": {
                        "type": "string",
                        "enum": ["qb", "guangya", "both"],
                    },
                    "check_interval_minutes": {
                        "type": "integer",
                        "minimum": 5,
                        "maximum": 10080,
                    },
                },
                "additionalProperties": False,
            },
            validator=media_subscription_policy_update_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                set_media_subscription_policy_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_set_media_subscription_policy
            ),
            examples=(
                "把媒体订阅 1 的下载目标改为两边",
                "媒体订阅 1 每 2 小时检查一次",
                "媒体订阅 1 改成只通知不要自动下载",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="media.create_subscription",
            description="预检并在用户确认后，为一个精确影视条目创建媒体追更订阅；不会立即搜索或下载资源。",
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
                    "season": {"type": "integer", "minimum": 1, "maximum": 100},
                    "check_interval_minutes": {
                        "type": "integer",
                        "enum": [4320, 10080],
                        "description": "检查周期：4320 为每 3 天，10080 为每 7 天。",
                    },
                },
                "additionalProperties": False,
            },
            validator=media_subscription_create_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                create_media_subscription_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_create_media_subscription
            ),
            examples=(
                "订阅这个 TMDB 剧集",
                "为这部剧创建媒体追更",
                "只追更这部剧第 2 季",
                "为光阴之外创建一个每周检查的追更订阅",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="media.delete_subscription",
            description="预检并在用户明确确认后软删除一个精确编号的媒体追更订阅；不会删除已提交下载任务或媒体文件。",
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "required": ["subscription_id"],
                "properties": {
                    "subscription_id": {"type": "integer", "minimum": 1},
                },
                "additionalProperties": False,
            },
            validator=media_subscription_delete_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                delete_media_subscription_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_delete_media_subscription
            ),
            examples=(
                "删除媒体追更订阅 2",
                "移除编号 4 的媒体订阅",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="media.set_subscription_enabled",
            description="预检并在用户确认后暂停或恢复一个指定媒体追更订阅；不会操作已提交下载任务或媒体文件。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["subscription_id", "enabled"],
                "properties": {
                    "subscription_id": {"type": "integer", "minimum": 1},
                    "enabled": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            validator=media_subscription_enabled_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                set_media_subscription_enabled_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_set_media_subscription_enabled
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="media.recently_played",
            description=(
                "读取媒体服务器用户的真实最近播放历史（播放事件/DatePlayed）；优先使用"
                "明确配置的用户，未配置时沿用服务器默认用户选择。不是继续观看 Resume 列表，"
                "可作为个性化片单推荐的事实依据。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "server": {"type": "string", "enum": ["auto", "jellyfin", "emby"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
            context_handler=get_recently_played,
            validator=recently_played_arguments,
            related_tools=("discovery.recommend",),
            examples=(
                "我最近看了什么",
                "查看 Jellyfin 最近播放历史",
                "根据我最近播放的内容推荐片单",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="media.recently_added",
            description="读取 Jellyfin 或 Emby 最近入库的内容；连续单集会按作品去重，避免刷屏。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "server": {"type": "string", "enum": ["auto", "jellyfin", "emby"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
            context_handler=get_recently_added,
            validator=recently_added_arguments,
            examples=("最近入库了什么", "查看 Jellyfin 最新添加内容"),
        )
    )
    registry.register(
        ToolSpec(
            name="media.continue_watching",
            description="读取媒体服务器用户的继续观看列表；优先使用明确配置用户，未配置时沿用服务器默认用户选择，不返回用户 ID、媒体内部 ID、URL、路径或凭据。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "server": {"type": "string", "enum": ["auto", "jellyfin", "emby"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
            context_handler=get_continue_watching,
            validator=continue_watching_arguments,
            examples=("继续观看", "查看 Jellyfin 继续观看", "Emby 还有哪些没看完"),
        )
    )
    registry.register(
        ToolSpec(
            name="media.preferences",
            description="读取当前会话显式保存的媒体服务器与下载目标偏好；不从聊天摘要或模型记忆推断。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            context_handler=get_preferences,
            validator=media_consumption_empty_arguments,
            examples=("查看我的媒体偏好", "下载默认到哪里", "我偏好哪个媒体服务器"),
        )
    )
    registry.register(
        ToolSpec(
            name="media.set_preferences",
            description="预检并在用户确认后保存当前会话的显式媒体偏好；偏好按会话身份隔离，不修改系统全局配置。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "preferred_server": {
                        "type": "string",
                        "enum": ["any", "jellyfin", "emby"],
                    },
                    "preferred_download_target": {
                        "type": "string",
                        "enum": ["qb", "guangya", "both"],
                    },
                },
                "additionalProperties": False,
            },
            validator=preferences_update_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_set_preferences,
            context_confirmed_handler=set_preferences_confirmed,
            examples=("以后默认下载到光鸭", "优先用 Jellyfin", "默认下载目标改为两边"),
        )
    )
    registry.register(
        ToolSpec(
            name="media.clear_preferences",
            description="预检并在用户确认后清除当前会话保存的显式媒体偏好，恢复产品默认值。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=media_consumption_empty_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_clear_preferences,
            context_confirmed_handler=clear_preferences_confirmed,
        )
    )
    registry.register(
        ToolSpec(
            name="media.today_summary",
            description="按本机今天的日期汇总全局管理员范围内的追更检查、整理入库、RSS 与下载内容事件；不返回路径、磁力、凭据或错误正文。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            context_handler=get_today_summary,
            validator=media_consumption_empty_arguments,
            examples=("今天媒体有什么更新", "今日内容摘要", "今天下载和入库了什么"),
        )
    )
    registry.register(
        ToolSpec(
            name="media.subscription_notification_rule",
            description="读取指定全局媒体追更订阅的通知规则；只返回公开订阅编号、标题和布尔开关。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["subscription_number"],
                "properties": {
                    "subscription_number": {"type": "integer", "minimum": 1}
                },
                "additionalProperties": False,
            },
            context_handler=get_subscription_notification_rule,
            validator=notification_rule_arguments,
            examples=("查看媒体订阅 1 的通知规则",),
        )
    )
    registry.register(
        ToolSpec(
            name="media.set_subscription_notification_rule",
            description="预检并在用户确认后修改指定全局媒体追更订阅的缺集、满足或错误通知开关；不会改变订阅巡检策略。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["subscription_number"],
                "properties": {
                    "subscription_number": {"type": "integer", "minimum": 1},
                    "enabled": {"type": "boolean"},
                    "notify_on_missing": {"type": "boolean"},
                    "notify_on_satisfied": {"type": "boolean"},
                    "notify_on_error": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            validator=notification_rule_update_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_set_subscription_notification_rule,
            context_confirmed_handler=set_subscription_notification_rule_confirmed,
            examples=("开启媒体订阅 1 的缺集通知", "关闭媒体订阅 2 的错误通知"),
        )
    )
    registry.register(
        ToolSpec(
            name="media.reset_subscription_notification_rule",
            description="预检并在用户确认后删除指定全局媒体追更订阅的显式通知规则，恢复默认关闭状态。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["subscription_number"],
                "properties": {
                    "subscription_number": {"type": "integer", "minimum": 1}
                },
                "additionalProperties": False,
            },
            validator=notification_rule_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_reset_subscription_notification_rule,
            context_confirmed_handler=reset_subscription_notification_rule_confirmed,
        )
    )
    registry.register(
        ToolSpec(
            name="rss.recent_activity",
            description="统计最近 24 小时 RSS 成功下载次数，并按订阅名称汇总；不返回 URL、条目正文或路径。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=get_rss_recent_activity,
            validator=rss_subscription_summaries_arguments,
            examples=(
                "RSS 最近有下载新内容吗",
                "查看最近 24 小时 RSS 更新",
                "查看我的追更和 RSS 更新情况",
                "检查订阅更新",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="rss.entry_summaries",
            description="安全列出 RSS 条目的公开编号、标题、状态、季集线索和固定失败分类；不返回 GUID、payload、下载 URL、路径或凭据。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "subscription_number": {"type": "integer", "minimum": 1},
                    "status": {
                        "type": "string",
                        "enum": [
                            "all",
                            "pending",
                            "submitting",
                            "downloaded",
                            "failed",
                            "skipped",
                        ],
                        "default": "pending",
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
            handler=list_rss_entry_summaries,
            validator=rss_entry_summaries_arguments,
            examples=(
                "列出 RSS 待处理条目",
                "看看 RSS 失败条目",
                "RSS 订阅 1 最近有哪些条目",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="rss.mark_entries",
            description="预检并在用户确认后把精确 RSS 条目编号标记为已处理或未处理；不会覆盖正在提交或已下载的条目。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["entry_numbers", "processed"],
                "properties": {
                    "entry_numbers": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 50,
                        "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "processed": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            validator=rss_mark_entries_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                mark_rss_entries_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_mark_rss_entries
            ),
            examples=(
                "把 RSS 条目 12 标记为已处理",
                "把 RSS 条目 12 和 13 恢复为未处理",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="rss.submit_entries_to_qb",
            description="预检并在用户确认后把精确的 pending RSS 条目集合提交到 qBittorrent；集合与配置会在确认时重新核对。",
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "required": ["entry_numbers"],
                "properties": {
                    "entry_numbers": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 1},
                    },
                },
                "additionalProperties": False,
            },
            validator=rss_submit_entries_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                submit_rss_entries_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_submit_rss_entries
            ),
            examples=(
                "下载 RSS 条目 12 到 qB",
                "把 RSS 条目 12 和 13 提交到 qBittorrent",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="rss.refresh_subscription",
            description="预检并在用户确认后刷新一个指定 RSS 订阅；不自动下载且不返回 URL、过滤词、条目正文或凭据。",
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "required": ["subscription_id"],
                "properties": {
                    "subscription_id": {"type": "integer", "minimum": 1},
                },
                "additionalProperties": False,
            },
            validator=rss_refresh_subscription_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_rss_subscription_refresh
            ),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                refresh_rss_subscription_confirmed
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="rss.refresh_subscriptions",
            description=(
                "预检并在用户确认后依次刷新一组 RSS 订阅，单次最多 32 个；"
                "不自动下载且不返回 URL、过滤词、条目正文或凭据。"
            ),
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "subscription_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 32,
                        "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["all_configured"],
                        "description": "all_configured 手动刷新全部已配置订阅（含停用项）。",
                    },
                },
                "oneOf": [
                    {"required": ["subscription_ids"]},
                    {"required": ["scope"]},
                ],
                "additionalProperties": False,
            },
            validator=rss_refresh_subscriptions_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                refresh_rss_subscriptions_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_rss_subscriptions_refresh
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="rss.submit_pending_to_qb",
            description="预检并在用户确认后，将最新的待处理 RSS 条目有界提交到 qBittorrent；不返回条目、URL、路径或凭据。",
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 10,
                    },
                },
                "additionalProperties": False,
            },
            validator=rss_pending_download_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_rss_pending_download
            ),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                submit_pending_rss_to_qb_confirmed
            ),
            examples=(
                "把最近 10 条 RSS 待处理内容提交到 qB",
                "提交 RSS 待处理条目",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="rss.retry_failed_to_qb",
            description="预检并在用户确认后，有界重试已明确分类为可安全重试的 qBittorrent RSS 失败条目；不返回条目、URL、路径、失败原文或凭据。",
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 10,
                    },
                },
                "additionalProperties": False,
            },
            validator=rss_failure_retry_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_rss_failure_retry
            ),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                retry_failed_rss_to_qb_confirmed
            ),
            examples=(
                "重试 RSS 失败条目",
                "重试最近 5 条可安全重试的 RSS 失败项",
            ),
        )
    )
