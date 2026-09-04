"""workspace 领域的 Agent 原子工具声明。"""

from __future__ import annotations

from app.agent.media_health_actions import (
    diagnose_workspace_health,
    workspace_health_arguments,
)
from app.agent.models import (
    RiskLevel,
    ToolSpec,
)
from app.agent.workspace_actions import (
    search_workspace,
    workspace_search_arguments,
)
from app.agent.workspace_briefing_actions import (
    summarize_workspace_briefing,
    workspace_briefing_arguments,
)
from app.agent.workspace_next_actions import (
    summarize_workspace_next_actions,
    workspace_next_actions_arguments,
)
from app.agent.workspace_todo_actions import (
    summarize_workspace_todo,
    workspace_todo_arguments,
)


def register_specs(
    registry, *, resource_store, active_ingest_store, ingest_actions
) -> None:
    registry.register(
        ToolSpec(
            name="workspace.briefing",
            description="生成本地系统简报，汇总工作区待办、下载后核验、媒体库巡检、索引器就绪与媒体服务器配置完整性；不访问网络，也不扫描媒体或云盘内容目录。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=summarize_workspace_briefing,
            validator=workspace_briefing_arguments,
            examples=(
                "给我一份系统简报",
                "现在有哪些事情需要处理",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="workspace.health",
            description="执行媒体系统健康总检，聚合本地工作区、关键配置与媒体服务器连通性；不扫描内容目录、不搜索资源且不执行写操作。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=diagnose_workspace_health,
            validator=workspace_health_arguments,
            examples=(
                "检查整个媒体系统是否健康",
                "排查配置和媒体服务器连通性",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="workspace.todo",
            description="只读汇总下载、RSS、整理、STRM、本地媒体、下载后核验与媒体库巡检的工作区待办计数；不返回标题、路径、URL、凭据、哈希、业务标识或错误正文。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=summarize_workspace_todo,
            validator=workspace_todo_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="workspace.next_actions",
            description="从本地安全待办快照生成按固定优先级排列的只读下一步行动卡；不执行诊断、预检或写操作，不返回标题、路径、URL、凭据、哈希、业务标识或错误正文。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=summarize_workspace_next_actions,
            validator=workspace_next_actions_arguments,
        )
    )
    registry.register(
        ToolSpec(
            name="workspace.search",
            description="按标题搜索媒体库、RSS、下载、整理与本地媒体工作流；不返回路径、URL、凭据、哈希、业务标识或错误正文。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 120},
                    "sections": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 5,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "enum": [
                                "library",
                                "rss",
                                "downloads",
                                "organize",
                                "local_media",
                            ],
                        },
                    },
                },
                "additionalProperties": False,
            },
            handler=search_workspace,
            validator=workspace_search_arguments,
        )
    )
