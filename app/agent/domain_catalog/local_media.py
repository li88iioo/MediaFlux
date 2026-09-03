"""local_media 领域的 Agent 原子工具声明。"""

from __future__ import annotations

from .support import (
    LOCAL_TASK_STATUSES,
    RiskLevel,
    ToolSpec,
    diagnose_local_media,
    get_local_media_source_summary,
    inspect_local_media_task,
    list_local_media_source_summaries,
    list_local_media_task_summaries,
    local_media_diagnosis_arguments,
    local_media_history_arguments,
    local_media_inspection_arguments,
    local_media_review_queue_arguments,
    local_media_scan_arguments,
    local_media_source_summaries_arguments,
    local_media_source_summary_arguments,
    local_media_source_trigger_arguments,
    local_media_task_number_arguments,
    local_media_task_summaries_arguments,
    prepare_refresh_local_media_task_library,
    prepare_retry_local_media_task,
    prepare_scan_local_media_sources,
    prepare_set_local_media_source_trigger_enabled,
    preview_local_media_task,
    refresh_local_media_task_library_confirmed,
    retry_local_media_task_confirmed,
    scan_local_media_sources_confirmed,
    set_local_media_source_trigger_enabled_confirmed,
    summarize_local_media_history,
    summarize_local_media_review_queue,
    verify_local_media_task_library_visibility,
)


def register_specs(
    registry, *, resource_store, active_ingest_store, ingest_actions
) -> None:
    registry.register(
        ToolSpec(
            name="local_media.diagnose",
            description="只读汇总本地媒体来源、整理任务与调度器状态，不扫描文件系统、不访问外部服务且不返回路径或业务标识。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=diagnose_local_media,
            validator=local_media_diagnosis_arguments,
            examples=(
                "本地媒体来源和整理调度正常吗",
                "检查本地媒体自动化状态",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="local_media.source_summaries",
            description="只读列出本地媒体来源的公开序号、触发状态和安全配置摘要，不返回名称、路径、媒体库标识或凭据。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=list_local_media_source_summaries,
            validator=local_media_source_summaries_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="local_media.get_source_summary",
            description="只读查看一个公开序号对应的本地媒体来源触发状态与安全摘要，不返回名称、路径、媒体库标识或凭据。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["source_number"],
                "properties": {"source_number": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
            handler=get_local_media_source_summary,
            validator=local_media_source_summary_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="local_media.set_source_trigger_enabled",
            description="确认后精确启停一个本地媒体来源的 qB 下载完成自动接管；不修改目录、规则、目标或凭据。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["source_number", "trigger", "enabled"],
                "properties": {
                    "source_number": {"type": "integer", "minimum": 1},
                    "trigger": {"type": "string", "enum": ["qb_completed"]},
                    "enabled": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            validator=local_media_source_trigger_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                set_local_media_source_trigger_enabled_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_set_local_media_source_trigger_enabled
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="local_media.scan_sources",
            description="预检并确认后扫描全部或指定公开序号的已配置本地媒体来源，把发现的媒体加入整理队列；不接受任意路径。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "source_numbers": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1, "maximum": 10000},
                        "maxItems": 20,
                    },
                    "query": {"type": "string", "maxLength": 120},
                },
                "additionalProperties": False,
            },
            validator=local_media_scan_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_scan_local_media_sources
            ),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                scan_local_media_sources_confirmed
            ),
            domains=("local_media", "organize"),
            source_kind="system_state",
            examples=("扫描全部本地媒体来源", "扫描本地媒体来源 2"),
        )
    )
    registry.register(
        ToolSpec(
            name="local_media.review_queue_summary",
            description="只读汇总本地媒体待人工确认队列的数量、触发来源和等待时长，不返回标题、路径、任务标识或错误正文。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=summarize_local_media_review_queue,
            validator=local_media_review_queue_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="local_media.task_summaries",
            description="只读列出本地媒体任务的 owner 绑定短期公开序号、媒体标题、阶段和可用动作，不返回路径、哈希、数据库 ID 或错误正文。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": [
                            "all",
                            "attention",
                            "active",
                            "history",
                            *sorted(LOCAL_TASK_STATUSES),
                        ],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
            context_handler=list_local_media_task_summaries,
            validator=local_media_task_summaries_arguments,
            examples=("列出本地媒体任务", "查看失败的本地整理任务"),
        )
    )
    registry.register(
        ToolSpec(
            name="local_media.inspect_task",
            description="只读检查一个短期公开序号对应的待人工确认任务，生成 owner 绑定检查序号；不返回路径、错误正文或内部句柄。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["task_number"],
                "properties": {"task_number": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
            context_handler=inspect_local_media_task,
            validator=local_media_task_number_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="local_media.preview_task",
            description="基于 owner 绑定短期检查序号生成本地整理匹配预览；只读且不返回路径、TMDB ID、规则快照或内部检查 ID。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["inspection_number"],
                "properties": {"inspection_number": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
            context_handler=preview_local_media_task,
            validator=local_media_inspection_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="local_media.retry_task",
            description="预检并确认后仅重试 failed 或 requires_manual 的本地媒体任务；使用版本条件原子重新排队，不直接移动文件。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["task_number"],
                "properties": {"task_number": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
            validator=local_media_task_number_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_retry_local_media_task,
            context_confirmed_handler=retry_local_media_task_confirmed,
        )
    )
    registry.register(
        ToolSpec(
            name="local_media.refresh_task_library",
            description="预检并确认后，仅对已完成任务重新解析出的唯一绑定媒体服务器与媒体库执行精准路径刷新；不接受 URL、路径或内部 ID。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["task_number"],
                "properties": {"task_number": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
            validator=local_media_task_number_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_refresh_local_media_task_library,
            context_confirmed_handler=refresh_local_media_task_library_confirmed,
        )
    )
    registry.register(
        ToolSpec(
            name="local_media.verify_task_library_visibility",
            description="只读核验已完成任务的媒体是否已在唯一绑定媒体库中索引，并明确标记未执行真实播放探测。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["task_number"],
                "properties": {"task_number": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
            context_handler=verify_local_media_task_library_visibility,
            validator=local_media_task_number_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="local_media.history_summary",
            description="只读汇总本地媒体已完成与失败历史的数量、触发来源和时间分布，不返回标题、路径、任务标识或错误正文。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=summarize_local_media_history,
            validator=local_media_history_arguments,
        )
    )
