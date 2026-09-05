"""活动时间线与可逆操作：声明原子能力，保持唯一 Kernel/EffectPlan。"""

from app.agent.action_undo import (
    execute_undo,
    inspect_undo,
    prepare_undo,
    receipt_arguments,
)
from app.agent.activity_actions import (
    get_activity_timeline,
    search_activities,
    search_arguments,
    selection_arguments,
)
from app.agent.activity_follow_actions import (
    follow_arguments,
    follow_confirmed,
    list_arguments,
    list_follows,
    prepare_follow,
    prepare_stop,
    stop_arguments,
    stop_confirmed,
)
from app.agent.models import RiskLevel, ToolSpec

_SELECTION = {
    "type": "object",
    "required": ["activity_selection_ref"],
    "properties": {
        "activity_selection_ref": {
            "type": "string",
            "pattern": "^ref_[A-Za-z0-9_-]{16,100}$",
        },
        "position": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1},
    },
    "additionalProperties": False,
}


def register_specs(registry, **_kwargs):
    registry.register(
        ToolSpec(
            name="activity.search",
            description="按标题查找下载、光鸭整理和本地整理活动，并返回可续查的会话引用。标题相同不代表同一任务；查询为空列出最近活动。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": 120},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
            validator=search_arguments,
            context_handler=search_activities,
            domains=("activity", "download", "cloud", "local_media"),
            related_tools=("activity.timeline", "action.undo.inspect"),
            examples=(
                "昨天绿灯军团下载整理的任务在哪里",
                "查找最近的整理操作",
                "刚才任务到哪里了",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="activity.timeline",
            description="通过安全活动引用读取下载→整理→STRM→媒体库复核的持久化阶段及故障原因，只按真实请求/运行标识关联。不把已提交当作已入库，不拿标题拼接因果。",
            risk=RiskLevel.READ,
            parameters=_SELECTION,
            validator=selection_arguments,
            context_handler=get_activity_timeline,
            domains=("activity", "download", "automation"),
            related_tools=("activity.search", "action.undo.inspect"),
            examples=(
                "刚才下载到哪了，为什么没有入库",
                "这个任务为什么只成功了两个",
                "解释这个任务失败在哪一步",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="action.undo.inspect",
            description="核对指定光鸭整理日志是否有可逆移动/改名的完整操作快照。只生成回退凭证，不执行。下载提交、本地文件缺乏反向快照、永久删除均不能猜测撤销；设置修改可直接使用其结果返回的 undo_receipt_ref。",
            risk=RiskLevel.READ,
            parameters=_SELECTION,
            validator=selection_arguments,
            context_handler=inspect_undo,
            domains=("activity", "cloud"),
            related_tools=("action.undo.execute", "activity.search"),
            examples=("撤销刚才的整理", "看看这个改名能不能回退", "把刚才移动的目录移回去"),
        )
    )
    registry.register(
        ToolSpec(
            name="action.undo.execute",
            description="使用服务端回退凭证生成冻结恢复计划；用户确认后才调用原领域执行器。只恢复有事务写后凭证的媒体偏好或可逆移动改名，校验中间更改，凭证一次性消费。不能撤回已下载/已发送通知。",
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "required": ["undo_receipt_ref"],
                "properties": {
                    "undo_receipt_ref": {
                        "type": "string",
                        "pattern": "^ref_[A-Za-z0-9_-]{16,100}$",
                    }
                },
                "additionalProperties": False,
            },
            validator=receipt_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_undo,
            context_confirmed_handler=execute_undo,
            domains=("activity", "cloud", "config"),
            related_tools=("action.undo.inspect",),
            examples=("恢复刚才改动之前的设置", "确认撤销这个移动改名"),
        )
    )

    follow_schema = {
        **_SELECTION,
        "properties": {
            **_SELECTION["properties"],
            "hours": {"type": "integer", "minimum": 1, "maximum": 168, "default": 24},
        },
    }
    registry.register(
        ToolSpec(
            name="activity.follow",
            description="持续跟踪选定任务，异常、已有阶段结束或到期时发送一次 Telegram 通知；只观察、不替任务重试。需要确认开启，复用既有通知开关。",
            risk=RiskLevel.WRITE,
            parameters=follow_schema,
            validator=follow_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_follow,
            context_confirmed_handler=follow_confirmed,
            domains=("activity", "automation", "download"),
            related_tools=("activity.search", "activity.follows"),
            examples=(
                "这个任务结束后告诉我",
                "帮我盯住刚才的下载",
                "下载入库情况有问题通知我",
            ),
        )
    )
    registry.register(
        ToolSpec(
            name="activity.follows",
            description="查看我持久保存的任务跟踪规则及启用状态和到期时间。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=list_arguments,
            context_handler=list_follows,
            domains=("activity", "automation"),
            related_tools=("activity.unfollow",),
            examples=("正在帮我跟踪哪些任务",),
        )
    )
    registry.register(
        ToolSpec(
            name="activity.unfollow",
            description="停用我的活动跟踪通知，不停止或删除下载/整理任务。",
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "required": ["rule_id"],
                "properties": {"rule_id": {"type": "string", "maxLength": 100}},
                "additionalProperties": False,
            },
            validator=stop_arguments,
            requires_confirmation=True,
            context_confirmation_preparer=prepare_stop,
            context_confirmed_handler=stop_confirmed,
            domains=("activity", "automation"),
            related_tools=("activity.follows",),
            examples=("不用再跟踪这个任务了",),
        )
    )
