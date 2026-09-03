"""strm 领域的 Agent 原子工具声明。"""

from __future__ import annotations

from .support import (
    RiskLevel,
    ToolSpec,
    _no_arguments,
    diagnose_strm,
    get_strm_run_history,
    prepare_strm_failure_retry,
    prepare_strm_run_once,
    prepare_strm_schedule_policy_confirmation,
    retry_strm_failure_records_confirmed,
    run_strm_once_confirmed,
    set_strm_schedule_policy_confirmed,
    strm_failure_retry_arguments,
    strm_failure_triage_arguments,
    strm_run_arguments,
    strm_run_history_arguments,
    strm_runtime_status,
    strm_schedule_policy_arguments,
    strm_schedule_policy_summary_arguments,
    summarize_strm_schedule_policy,
    triage_strm_failures,
)


def register_specs(
    registry, *, resource_store, active_ingest_store, ingest_actions
) -> None:
    registry.register(
        ToolSpec(
            name="strm.diagnose",
            description="检查 STRM 索引、缺失文件、失败记录和最近同步状态，不执行修复。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=diagnose_strm,
            validator=_no_arguments,
            examples=(
                "STRM 最近同步正常吗",
                "检查 STRM 缺失和失败",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="strm.run_history",
            description="读取最近 STRM 运行的安全历史、固定统计、失败聚合和队列计数；不返回运行 ID、来源、路径、文件名、对象标识或错误正文。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 8,
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "all",
                            "running",
                            "success",
                            "partial",
                            "failed",
                            "skipped",
                        ],
                        "default": "all",
                    },
                },
                "additionalProperties": False,
            },
            handler=get_strm_run_history,
            validator=strm_run_history_arguments,
            examples=(
                "看看 STRM 最近运行历史",
                "STRM 最近为什么失败",
                "查看 STRM 队列和失败上下文",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="strm.triage_failures",
            description="只读汇总 STRM 失败账本的状态与动作类别，不返回路径、文件名、来源、对象标识或错误正文。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=triage_strm_failures,
            validator=strm_failure_triage_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="strm.retry_failures",
            description="预检并在用户确认后重试当前 STRM 失败项，仅返回聚合计数，不暴露失败明细。",
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["all", "generate", "metadata"],
                        "default": "all",
                    },
                },
                "additionalProperties": False,
            },
            validator=strm_failure_retry_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_strm_failure_retry
            ),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                retry_strm_failure_records_confirmed
            ),
            examples=(
                "重试 STRM 失败项",
                "只重试 STRM 元数据失败项",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="strm.run_once",
            description=(
                "预检并在用户确认后同步全部或指定的已配置 STRM 来源；"
                "source_names 只能使用设置中名称唯一的来源，不接受目录或来源 ID。"
            ),
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "properties": {
                    "source_names": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 16,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 80},
                    },
                },
                "additionalProperties": False,
            },
            validator=strm_run_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_strm_run_once
            ),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                run_strm_once_confirmed
            ),
            domains=("strm",),
            source_kind="system_state",
            examples=(
                "执行一次 STRM 完整同步",
                "现在同步 STRM",
                "只同步整理这个 STRM 来源",
                "同步整理和 NSFW，不同步其他来源",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="strm.status",
            description=(
                "查看 STRM 当前运行进度、调度状态、最近结果和可选择的来源显示名称；"
                "不返回目录、来源 ID 或错误正文。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=strm_runtime_status,
            validator=_no_arguments,
            examples=(
                "查看 STRM 当前同步状态",
                "有哪些 STRM 来源可以同步",
                "STRM 同步到哪里了",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="strm.schedule_policy",
            description="读取 STRM 定时同步的启用状态、五段 cron 和任务通知开关，不返回目录、地址或凭据。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=summarize_strm_schedule_policy,
            validator=strm_schedule_policy_summary_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="strm.set_schedule_policy",
            description="预检并在用户确认后修改 STRM 定时同步的三项白名单策略，不立即运行同步。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "minProperties": 1,
                "properties": {
                    "enabled": {"type": "boolean"},
                    "cron": {"type": "string", "minLength": 1, "maxLength": 128},
                    "notify_enabled": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            validator=strm_schedule_policy_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                set_strm_schedule_policy_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_strm_schedule_policy_confirmation
            ),
        )
    )
