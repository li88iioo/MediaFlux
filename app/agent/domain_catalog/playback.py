"""playback 领域的 Agent 原子工具声明。"""

from __future__ import annotations

from app.agent.media_proxy_actions import (
    media_proxy_enabled_arguments,
    media_proxy_failure_summary_arguments,
    media_proxy_restart_arguments,
    media_proxy_status_arguments,
    media_proxy_test_arguments,
    prepare_restart_media_proxy_instance,
    prepare_set_media_proxy_instance_enabled,
    restart_media_proxy_instance_confirmed,
    set_media_proxy_instance_enabled_confirmed,
    summarize_media_proxy_playback_failures,
    summarize_media_proxy_status,
    test_media_proxy_instance,
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
            name="media_proxy.status_summary",
            description="安全汇总媒体反代实例的数量、类型、启用状态与运行状态，不返回地址、端口、路径、实例 ID 或凭据。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=summarize_media_proxy_status,
            validator=media_proxy_status_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="media_proxy.playback_failure_summary",
            description="按固定时间窗聚合已记录的媒体反代播放请求、失败阶段、路由类别、缓存命中与平均时延；不返回媒体名、用户、会话、URL、路径或错误正文。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["hours"],
                "properties": {
                    "hours": {"type": "integer", "enum": [1, 6, 24, 72]},
                    "instance_number": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10000,
                    },
                },
                "additionalProperties": False,
            },
            handler=summarize_media_proxy_playback_failures,
            validator=media_proxy_failure_summary_arguments,
            examples=(
                "查看最近 24 小时播放失败摘要",
                "媒体反代最近 6 小时哪里失败最多",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="media_proxy.test_instance",
            description="按公开序号测试一个已保存媒体反代实例的上游连通性，不返回地址、端口、路径、实例 ID、凭据或原始错误。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["instance_number"],
                "properties": {
                    "instance_number": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10000,
                    },
                },
                "additionalProperties": False,
            },
            handler=test_media_proxy_instance,
            validator=media_proxy_test_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="media_proxy.set_instance_enabled",
            description="预检并在用户确认后按公开序号启用或停用一个媒体反代实例；不会修改地址、监听、路径或凭据。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["instance_number", "enabled"],
                "properties": {
                    "instance_number": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10000,
                    },
                    "enabled": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            validator=media_proxy_enabled_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_set_media_proxy_instance_enabled
            ),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                set_media_proxy_instance_enabled_confirmed
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="media_proxy.restart_instance",
            description="预检并确认后按公开序号强制重建一个已启用媒体反代实例的运行时；不修改实例配置。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["instance_number"],
                "properties": {
                    "instance_number": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10000,
                    },
                },
                "additionalProperties": False,
            },
            validator=media_proxy_restart_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_restart_media_proxy_instance
            ),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                restart_media_proxy_instance_confirmed
            ),
            domains=("playback",),
            source_kind="system_state",
            examples=("重启媒体反代实例 1", "重启 Jellyfin 反代实例 2"),
        )
    )
