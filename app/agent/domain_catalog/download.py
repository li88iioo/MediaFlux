"""download 领域的 Agent 原子工具声明。"""

from __future__ import annotations

from app.agent.download_actions import (
    diagnose_download_queue,
    download_diagnosis_arguments,
    download_request_summaries_arguments,
    summarize_download_requests,
)
from app.agent.download_retry_actions import (
    download_retry_submission_arguments,
    prepare_retry_download_submission,
    retry_download_submission_confirmed,
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
            name="downloads.diagnose_queue",
            description="只读诊断 qBittorrent 当前队列、传输状态与疑似停滞任务，不返回 hash、路径或凭据。此诊断不含客户端版本；版本与其他原生信息通过provider.capabilities发现后用provider.query读取。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=diagnose_download_queue,
            validator=download_diagnosis_arguments,
            related_tools=("provider.capabilities",),
            examples=(
                "检查下载队列有没有异常",
                "qBittorrent 里有没有卡住的任务",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="downloads.request_summaries",
            description="只读列出当前 MediaFlux 实例本地 SQLite 已记录的下载请求在 qB、光鸭、整理与 STRM 各阶段的安全状态摘要；不查询光鸭实时全账户离线任务队列，也不代表其完整状态；不返回链接、路径、哈希或云端任务标识。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["active", "attention", "recent"],
                        "default": "active",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 30,
                        "default": 12,
                    },
                },
                "additionalProperties": False,
            },
            handler=summarize_download_requests,
            validator=download_request_summaries_arguments,
            domains=("downloads", "jobs"),
            source_kind="system_state",
            freshness="live",
            examples=("列出最近的本地下载请求", "哪些本地下载请求需要处理"),
        )
    )
    registry.register(
        ToolSpec(
            name="downloads.retry_submission",
            description=(
                "预检并在用户确认后，将一条明确编号的下载待处理记录重新提交到 "
                "qBittorrent、光鸭或两者；不返回资源链接、种子、路径、任务标识或凭据。"
            ),
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "required": ["request_id", "target"],
                "properties": {
                    "request_id": {"type": "integer", "minimum": 1},
                    "target": {"type": "string", "enum": ["qb", "guangya", "both"]},
                },
                "additionalProperties": False,
            },
            validator=download_retry_submission_arguments,
            requires_confirmation=True,
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                retry_download_submission_confirmed
            ),
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                prepare_retry_download_submission
            ),
            examples=(
                "重新提交下载待处理记录 3 到光鸭",
                "把下载请求 2 重新提交到 qB 和光鸭",
            ),
        )
    )
