"""确定性跨域订阅与主动摘要；只有确认入口可持久化授权。"""

from __future__ import annotations

from app.agent.automation_rule_actions import (
    create_media_rule_arguments,
    create_media_rule_confirmed,
    digest_list_arguments,
    digest_set_arguments,
    list_digest_rules,
    prepare_create_media_rule,
    prepare_set_digest,
    set_digest_confirmed,
)
from app.agent.models import RiskLevel, ToolSpec


def register_specs(registry, **_dependencies) -> None:
    registry.register(
        ToolSpec(
            name="automation.create_media_rule",
            description="创建持续运行的媒体追更规则：既有调度器按每3天或7天检查缺集、搜索指定站点、按提醒/确认/自动策略提交光鸭或qB。不会额外启动LLM定时器。后处理只复用已有配置。不支持固定星期/时间或独立4K字幕硬过滤，请勿把这些约束宣称已保存。",
            risk=RiskLevel.WRITE,
            requires_confirmation=True,
            parameters={
                "type": "object",
                "properties": {
                    "tmdb_id": {"type": "string", "pattern": "^[1-9][0-9]{0,9}$"},
                    "media_type": {"type": "string", "enum": ["movie", "tv"]},
                    "season": {"type": "integer", "minimum": 1, "maximum": 100},
                    "check_interval_minutes": {
                        "type": "integer",
                        "enum": [4320, 10080],
                    },
                    "action": {"type": "string", "enum": ["notify", "confirm", "auto"]},
                    "download_target": {
                        "type": "string",
                        "enum": ["qb", "guangya", "both"],
                    },
                    "sites": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 6,
                    },
                    "enabled": {"type": "boolean"},
                },
                "required": ["tmdb_id", "media_type"],
                "additionalProperties": False,
            },
            validator=create_media_rule_arguments,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_create_media_rule
            ),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                create_media_rule_confirmed
            ),
            domains=("automation", "subscription", "download"),
            related_tools=(
                "media.subscription_summaries",
                "media.set_subscription_policy",
                "media.delete_subscription",
            ),
            examples=(
                "每周检查光阴之外缺集，自动提交光鸭下载",
                "创建每3天检查一次的媒体自动化，只提醒不下载",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="automation.digest_rules",
            description="读取当前身份保存的每日媒体动态/异常摘要规则，默认没有开启。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=digest_list_arguments,
            context_handler=list_digest_rules,
            domains=("automation", "notification"),
            related_tools=("automation.set_digest",),
            examples=("我配置了哪些每日摘要", "每日通知什么时候发送"),
        )
    )
    registry.register(
        ToolSpec(
            name="automation.set_digest",
            description="确认后新增或修改每日摘要规则；按本机时区每天指定时刻汇总媒体动态，或只汇总失败和需关注项。复用既有Telegram通知总开关/队列，不调用LLM、不另建通知链路。enabled=false停用；修改必须提供读取到的rule_id。",
            risk=RiskLevel.WRITE,
            requires_confirmation=True,
            parameters={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string", "maxLength": 100},
                    "enabled": {"type": "boolean"},
                    "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                    "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                    "errors_only": {"type": "boolean"},
                    "send_empty": {"type": "boolean"},
                },
                "required": ["enabled", "hour"],
                "additionalProperties": False,
            },
            validator=digest_set_arguments,
            context_confirmation_preparer=prepare_set_digest,
            context_confirmed_handler=set_digest_confirmed,
            domains=("automation", "notification"),
            related_tools=("automation.digest_rules", "media.today_summary"),
            examples=(
                "每天晚上9点汇总今天的媒体动态",
                "每天只在下载或整理异常时发一份摘要",
                "停用每日主动摘要",
            ),
        )
    )
