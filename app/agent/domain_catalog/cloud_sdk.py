"""光鸭 SDK 增量能力的原子 ToolSpec 声明。"""

from __future__ import annotations

from app.agent.guangya_account_actions import (
    get_guangya_account_status,
    guangya_account_status_arguments,
)
from app.agent.guangya_recycle_actions import (
    execute_clear_guangya_recycle,
    execute_restore_guangya_recycle,
    guangya_recycle_clear_arguments,
    guangya_recycle_list_arguments,
    guangya_recycle_restore_arguments,
    guangya_task_status_arguments,
    list_guangya_recycle,
    prepare_clear_guangya_recycle,
    prepare_restore_guangya_recycle,
    query_guangya_task_status,
)
from app.agent.guangya_share_actions import (
    execute_create_guangya_share,
    execute_revoke_guangya_shares,
    guangya_share_create_arguments,
    guangya_share_list_arguments,
    guangya_share_revoke_arguments,
    list_guangya_user_shares,
    prepare_create_guangya_share,
    prepare_revoke_guangya_shares,
)
from app.agent.models import RiskLevel, ToolSpec


def register_specs(
    registry, *, resource_store, active_ingest_store, ingest_actions
) -> None:
    del resource_store, active_ingest_store, ingest_actions

    registry.register(
        ToolSpec(
            name="guangya.account.status",
            description=(
                "读取当前光鸭账号连接状态与服务端可用的容量摘要。只返回掩码身份和总量/已用/可用字节，"
                "不返回用户 ID、手机号明文、Token 或原始 SDK 响应。"
            ),
            risk=RiskLevel.READ,
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            validator=guangya_account_status_arguments,
            handler=get_guangya_account_status,
            domains=("cloud_account", "cloud_files"),
            source_kind="guangya_account",
            freshness="live",
            examples=("我的光鸭还剩多少空间", "查看光鸭账号容量和连接状态"),
        )
    )
    registry.register(
        ToolSpec(
            name="guangya.recycle.list",
            description=(
                "分页读取光鸭回收站，返回名称、类型、体积和会话绑定的 guangya_recycle_items_ref；"
                "不返回 Provider 文件 ID。恢复时必须引用本次列表。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "minimum": 1, "maximum": 400, "default": 1},
                    "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                },
                "additionalProperties": False,
            },
            validator=guangya_recycle_list_arguments,
            context_handler=list_guangya_recycle,
            related_tools=("guangya.recycle.restore", "guangya.recycle.clear"),
            domains=("cloud_files", "storage_hygiene"),
            source_kind="guangya_recycle_snapshot",
            freshness="live",
            examples=("看看光鸭回收站里有什么", "列出刚删除的光鸭文件"),
        )
    )
    registry.register(
        ToolSpec(
            name="guangya.recycle.restore",
            description=(
                "对 guangya.recycle.list 返回的回收站引用按 index 冻结恢复计划。模型调用只生成预览，"
                "用户确认后才恢复；确认时重新核对凭据和对象快照。"
            ),
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "required": ["guangya_recycle_items_ref", "indices"],
                "properties": {
                    "guangya_recycle_items_ref": {"type": "string", "pattern": "^ref_[A-Za-z0-9_-]{8,190}$"},
                    "indices": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 200,
                        "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 1, "maximum": 20000},
                    },
                },
                "additionalProperties": False,
            },
            validator=guangya_recycle_restore_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_restore_guangya_recycle,
            context_confirmed_handler=execute_restore_guangya_recycle,
            related_tools=("guangya.recycle.list", "guangya.operation.status"),
            domains=("cloud_files", "storage_hygiene"),
            source_kind="guangya_recycle_snapshot",
            freshness="live",
            examples=("恢复回收站列表中的第 1 项", "把刚才回收站中的前两项恢复"),
        )
    )
    registry.register(
        ToolSpec(
            name="guangya.recycle.clear",
            description=(
                "完整读取并冻结当前光鸭回收站后生成不可逆清空计划。任一对象发生变化都会使确认失效；"
                "确认后永久删除全部回收站内容。"
            ),
            risk=RiskLevel.DANGER,
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            validator=guangya_recycle_clear_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_clear_guangya_recycle,
            context_confirmed_handler=execute_clear_guangya_recycle,
            related_tools=("guangya.recycle.list", "guangya.operation.status"),
            domains=("cloud_files", "storage_hygiene"),
            source_kind="guangya_recycle_snapshot",
            freshness="live",
            examples=("清空光鸭回收站", "永久删除光鸭回收站全部内容"),
        )
    )
    registry.register(
        ToolSpec(
            name="guangya.operation.status",
            description=(
                "使用回收站恢复、清空或其他 SDK 异步操作返回的 guangya_task_ref 查询 Provider 状态。"
                "不接受或公开原始任务 ID。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["guangya_task_ref"],
                "properties": {
                    "guangya_task_ref": {"type": "string", "pattern": "^ref_[A-Za-z0-9_-]{8,190}$"},
                },
                "additionalProperties": False,
            },
            validator=guangya_task_status_arguments,
            context_handler=query_guangya_task_status,
            domains=("cloud_files", "cloud_tasks"),
            source_kind="guangya_provider_task",
            freshness="live",
            examples=("查询刚才的光鸭恢复任务", "刚才清空回收站完成了吗"),
        )
    )
    registry.register(
        ToolSpec(
            name="guangya.share.list",
            description=(
                "分页读取当前账号自己创建的光鸭分享，返回标题、状态、时间摘要和会话绑定引用；"
                "不返回分享 ID、访问码或底层响应。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "minimum": 1, "maximum": 200, "default": 1},
                    "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                },
                "additionalProperties": False,
            },
            validator=guangya_share_list_arguments,
            context_handler=list_guangya_user_shares,
            related_tools=("guangya.share.revoke",),
            domains=("cloud_files", "cloud_sharing"),
            source_kind="guangya_share_snapshot",
            freshness="live",
            examples=("我创建了哪些光鸭分享", "列出光鸭分享链接状态"),
        )
    )
    registry.register(
        ToolSpec(
            name="guangya.share.create",
            description=(
                "为 guangya.fs.query 最近观察中的 1–100 个 object_ref 创建分享。可设置标题、有效天数、"
                "自动或自定义访问码、最大转存次数和下载权限；模型调用只生成冻结预览，确认后创建。"
            ),
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "required": ["object_refs"],
                "properties": {
                    "observation_ref": {"type": "string", "pattern": "^OBS[0-9A-Fa-f]{32}$"},
                    "object_refs": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 100,
                        "uniqueItems": True,
                        "items": {"type": "string", "pattern": "^OBJ[0-9A-Fa-f]{24}$"},
                    },
                    "title": {"type": "string", "maxLength": 180},
                    "expires_days": {"type": "integer", "minimum": 0, "maximum": 3650, "default": 0},
                    "access_code": {"type": "string", "pattern": "^[A-Za-z0-9]{4,16}$"},
                    "auto_access_code": {"type": "boolean", "default": True},
                    "max_restore_count": {"type": "integer", "minimum": 0, "maximum": 1000000, "default": 0},
                    "allow_download": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            },
            validator=guangya_share_create_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_create_guangya_share,
            context_confirmed_handler=execute_create_guangya_share,
            # 创建分享只依赖目录观察；分享列表属于后续管理能力，不能在
            # “创建目录并规整文件”这类高重合语句中挤掉 FS 执行工具。
            related_tools=("guangya.fs.query",),
            domains=("cloud_files", "cloud_sharing"),
            source_kind="guangya_share_plan",
            freshness="live",
            examples=("把刚才选中的目录创建一个 7 天光鸭分享", "给这几个文件创建无访问码分享"),
        )
    )
    registry.register(
        ToolSpec(
            name="guangya.share.revoke",
            description=(
                "按 guangya.share.list 返回的分享引用和 index 冻结撤销计划；确认后仅撤销分享链接，"
                "不会删除或移动原始云盘文件。"
            ),
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "required": ["guangya_shares_ref", "indices"],
                "properties": {
                    "guangya_shares_ref": {"type": "string", "pattern": "^ref_[A-Za-z0-9_-]{8,190}$"},
                    "indices": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 100,
                        "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 1, "maximum": 2000},
                    },
                },
                "additionalProperties": False,
            },
            validator=guangya_share_revoke_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_revoke_guangya_shares,
            context_confirmed_handler=execute_revoke_guangya_shares,
            related_tools=("guangya.share.list",),
            domains=("cloud_files", "cloud_sharing"),
            source_kind="guangya_share_snapshot",
            freshness="live",
            examples=("撤销刚才列表中的第 2 个分享", "关闭这两个光鸭分享链接"),
        )
    )
